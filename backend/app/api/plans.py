"""Расчёт и чтение планов (PLAN 5.4)."""

from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Response
from pydantic import Field

from ..data import seed
from ..models import (
    Algorithm,
    Clock,
    Dataset,
    DaySummary,
    Event,
    History,
    Input,
    Model,
    Plan,
    Preview,
    ReplanMode,
    Variant,
)
from ..planning import export, replan, scenarios, service

router = APIRouter(prefix="/api/plans", tags=["plans"])


class PlanRequest(Model):
    """Тело расчёта.

    Базовый вариант ТЗ или оптимизация OR-Tools; `manual` отклоняет сама модель, а не
    спрятанная ветка в коде. Значения перечисления в `Literal` не ставим: pydantic показал
    бы в ошибке `<Algorithm.BASELINE: 'baseline'>` вместо читаемого «Input should be
    'baseline' or 'optimized'».
    """

    algorithm: Literal["baseline", "optimized"] = "baseline"


class CompareRequest(Model):
    """Тело сравнения вариантов (PLAN 5.4, 6.14).

    По умолчанию считаются все варианты в порядке справочника; список можно сократить —
    каждый вариант стоит своего лимита поиска. Неизвестное значение отклоняет модель.
    """

    variants: list[Variant] = Field(default_factory=lambda: list(Variant))


class ManualRequest(Model):
    """Тело ручного переназначения (PLAN 5.4, 6.16).

    `engineerId: null` — снять назначение. `position` — куда вставить в маршрут выбранной
    бригады: индекс с нуля, считая закреплённые стопы и без самой переносимой заявки, то
    есть ровно то, что диспетчер видит в таблице плана. `null` — лучшая позиция.
    """

    request_id: str
    engineer_id: str | None = None
    position: int | None = Field(default=None, ge=0)


class EventRequest(Model):
    """Тело перепланирования (PLAN 5.4).

    Без `mode` режим выбирается по событию (`replan.default_mode`): обычная заявка днём
    встраивается в свободное время (`insert`), всё остальное пересчитывает остаток дня с
    момента события (`from_event`). `full` считает день заново целиком и служит для
    сравнения (PLAN 6.12).
    """

    event: Event
    mode: ReplanMode | None = None


@router.post("", response_model=Plan)
def create_plan(body: PlanRequest) -> Plan:
    dataset = seed.current()
    if dataset is None:
        raise HTTPException(404, "Набор данных не загружен")

    return _checked(service.create_plan(dataset, Algorithm(body.algorithm)))


@router.post("/compare", response_model=list[Plan])
def compare_plans(body: CompareRequest) -> list[Plan]:
    """Один день несколькими настройками расчёта (PLAN 6.14).

    Путь объявлен раньше `/{plan_id}`: иначе «compare» сошло бы за идентификатор плана.
    Ответ — полные планы, а не сводка: диспетчер открывает любой вариант на карте и в
    таблицах, не считая его заново.
    """
    dataset = seed.current()
    if dataset is None:
        raise HTTPException(404, "Набор данных не загружен")

    return [
        _checked(plan, f"Вариант «{plan.variant}»: ")
        for plan in service.compare(dataset, body.variants)
    ]


@router.post("/{plan_id}/manual", response_model=Plan)
def manual_assign(plan_id: str, body: ManualRequest) -> Plan:
    """Ручное переназначение создаёт новую версию плана с `diff` и `pinned` (PLAN 6.16)."""
    parent, dataset = _parent(plan_id)
    try:
        return _checked(
            service.manual_plan(
                parent,
                dataset,
                body.request_id,
                body.engineer_id,
                body.position,
            )
        )
    except replan.ReplanError as error:
        raise HTTPException(error.status, str(error)) from error


@router.post("/{plan_id}/events", response_model=Plan)
def apply_event(plan_id: str, body: EventRequest) -> Plan:
    """Событие создаёт новую версию плана с `parentPlanId` и `diff` (PLAN 6.12, 6.13)."""
    parent, dataset = _parent(plan_id)
    _not_clock(body.event)
    try:
        mode = body.mode or replan.default_mode(body.event)
        return _checked(service.replan_plan(parent, dataset, body.event, mode))
    except replan.ReplanError as error:
        raise HTTPException(error.status, str(error)) from error


class PreviewRequest(Model):
    """Тело предпросмотра события (PLAN 5.4, 6.12): режим выбирает не диспетчер, а карточки."""

    event: Event


@router.post("/{plan_id}/events/preview", response_model=Preview)
def preview_event(plan_id: str, body: PreviewRequest) -> Preview:
    """Варианты реакции на событие черновиками; версия появится только после `commit`."""
    parent, dataset = _parent(plan_id)
    _not_clock(body.event)
    try:
        preview = service.preview(parent, dataset, body.event)
    except replan.ReplanError as error:
        raise HTTPException(error.status, str(error)) from error
    for option in preview.options:
        _checked(option.plan, f"Вариант «{option.mode}»: ")
    return preview


@router.post("/{plan_id}/commit", response_model=Plan)
def commit_plan(plan_id: str) -> Plan:
    """Применить вариант предпросмотра: он становится версией цепочки (PLAN 6.12)."""
    plan = service.commit(plan_id)
    if plan is None:
        raise HTTPException(404, "Варианты устарели — посчитайте заново")
    return plan


class ClockRequest(Model):
    time: Clock


@router.post(
    "/{plan_id}/clock",
    response_model=list[Plan],
    responses={204: {"description": "ничего нового"}},
)
def day_clock(plan_id: str, body: ClockRequest) -> list[Plan] | Response:
    """Часы дня (блоки 68, 82): закрыть работы, чья скрытая длительность истекла, и
    починить остаток при затянувшейся — всё до `time`, по порядку. Новые версии от первой к
    последней, каждая — потомок предыдущей; ничего нового — 204."""
    parent, dataset = _parent(plan_id)
    try:
        versions = service.clock(parent, dataset, body.time)
    except replan.ReplanError as error:
        raise HTTPException(error.status, str(error)) from error
    if not versions:
        return Response(status_code=204)
    return [_checked(version) for version in versions]


@router.get("/{plan_id}/day-summary", response_model=DaySummary)
def day_summary(plan_id: str) -> DaySummary:
    """«Итоги дня» (блок 68): работа план / факт и скорость инженеров, несделанное."""
    plan = service.get_plan(plan_id)
    if plan is None:
        raise HTTPException(404, "План не найден")
    return service.day_summary(plan)


@router.get("/{plan_id}/history", response_model=History)
def plan_history(plan_id: str) -> History:
    """Цепочка версий плана с событиями, режимами и метриками (PLAN 6.17).

    Плюс `diff` первой версии к запрошенной — «Сравнить с исходным». Версии приходят
    короткими записями: полный план берётся `GET /api/plans/{id}`.
    """
    history = scenarios.history(plan_id)
    if history is None:
        raise HTTPException(404, "План не найден")
    return history


@router.get("/{plan_id}/export.csv")
def export_plan(plan_id: str) -> Response:
    """План версии файлом для Excel (PLAN 5.4, блок 56).

    Набор называется по плану, а не по текущему: текущий мог смениться импортом. Пропал
    набор — в имени его идентификатор, сам план от набора не зависит.
    """
    plan = service.get_plan(plan_id)
    if plan is None:
        raise HTTPException(404, "План не найден")
    dataset = seed.by_id(plan.dataset_id)
    name = dataset.name if dataset else plan.dataset_id
    # Заголовок — latin-1: кириллица имени едет в `filename*`, ASCII-запасное — без неё.
    disposition = (
        f'attachment; filename="plan.csv"; '
        f"filename*=UTF-8''{quote(export.filename(plan, name))}"
    )
    return Response(
        export.plan_csv(plan, name, _before_event(plan)).encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": disposition},
    )


def _before_event(plan: Plan) -> Input | None:
    """Вход дня до события версии: в нём ещё есть отменённые заявки.

    Ручная правка наследует событие родителя (PLAN 6.16), поэтому подниматься надо до
    версии, где событие появилось, и брать вход её родителя. Цепочки короткие.
    """
    version = plan
    while plan.event and version.parent_plan_id:
        parent = service.get_plan(version.parent_plan_id)
        if parent is None:
            return None
        if parent.event != plan.event:
            return parent.input
        version = parent
    return None


def _not_clock(event: Event) -> None:
    """Событие часов собирает сервер по скрытым фактам (`POST /clock`), а не клиент."""
    if event.type == "clock":
        raise HTTPException(400, "Часы дня — только через POST /api/plans/{id}/clock")


def _parent(plan_id: str) -> tuple[Plan, Dataset]:
    """План-родитель и набор, по которому он считался (PLAN 6.12).

    Набор берём тот, по которому считался родитель, а не текущий: текущий мог смениться
    импортом, и новая версия поехала бы по чужим точкам.
    """
    parent = service.get_plan(plan_id)
    if parent is None:
        raise HTTPException(404, "План не найден")
    # Черновик предпросмотра — ещё не версия дня (PLAN 6.12, блок 53): событие от него
    # повисло бы на плане, который через час удалит TTL.
    if parent.draft:
        raise HTTPException(409, "Вариант ещё не применён — сначала «Применить»")
    dataset = seed.by_id(parent.dataset_id)
    if dataset is None:
        raise HTTPException(404, "Набор данных плана не найден")
    return parent, dataset


def _checked(plan: Plan, prefix: str = "") -> Plan:
    if not plan.validation.ok:
        # Независимая проверка не сошлась — отдавать такой план нельзя (PLAN 6.7).
        raise HTTPException(
            500,
            prefix + "План не прошёл проверку: " + "; ".join(plan.validation.errors),
        )
    return plan


@router.get("/{plan_id}", response_model=Plan)
def get_plan(plan_id: str) -> Plan:
    plan = service.get_plan(plan_id)
    if plan is None:
        raise HTTPException(404, "План не найден")
    return plan
