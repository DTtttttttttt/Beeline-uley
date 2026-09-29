"""Расчёт и чтение планов (PLAN 5.4)."""

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import Field

from ..data import seed
from ..models import (
    Algorithm,
    Dataset,
    Event,
    History,
    Model,
    Plan,
    ReplanMode,
    Variant,
)
from ..planning import replan, scenarios, service

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
                parent, dataset, body.request_id, body.engineer_id, body.position
            )
        )
    except replan.ReplanError as error:
        raise HTTPException(error.status, str(error)) from error


@router.post("/{plan_id}/events", response_model=Plan)
def apply_event(plan_id: str, body: EventRequest) -> Plan:
    """Событие создаёт новую версию плана с `parentPlanId` и `diff` (PLAN 6.12, 6.13)."""
    parent, dataset = _parent(plan_id)
    try:
        mode = body.mode or replan.default_mode(body.event)
        return _checked(service.replan_plan(parent, dataset, body.event, mode))
    except replan.ReplanError as error:
        raise HTTPException(error.status, str(error)) from error


@router.post("/{plan_id}/scenarios/{scenario_id}", response_model=list[Plan])
def run_scenario(plan_id: str, scenario_id: str) -> list[Plan]:
    """Готовая цепочка событий поверх плана: все версии одним запросом (PLAN 6.17).

    Ответ — полные планы, как у сравнения вариантов (PLAN 6.14): диспетчер открывает любую
    версию на карте и в таблицах, не считая её заново. Цена — лимит перепланирования на
    каждое событие, то есть десятки секунд на всю цепочку.
    """
    parent, dataset = _parent(plan_id)
    scenario = seed.scenario_by_id(scenario_id)
    if scenario is None:
        raise HTTPException(404, "Сценарий не найден")
    if scenario.dataset_id != parent.dataset_id:
        # Без этой проверки сценарий упал бы в середине с «заявки №… нет», и диспетчер
        # чинил бы не ту причину.
        raise HTTPException(
            400,
            f"Сценарий «{scenario.name}» составлен для другого набора данных: "
            f"{_name(scenario.dataset_id)} вместо {_name(parent.dataset_id)}",
        )

    try:
        return [
            _checked(version) for version in scenarios.run(parent, dataset, scenario)
        ]
    except replan.ReplanError as error:
        raise HTTPException(error.status, str(error)) from error


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


def _name(dataset_id: str) -> str:
    dataset = seed.by_id(dataset_id)
    return f"«{dataset.name}»" if dataset else dataset_id


def _parent(plan_id: str) -> tuple[Plan, Dataset]:
    """План-родитель и набор, по которому он считался (PLAN 6.12).

    Набор берём тот, по которому считался родитель, а не текущий: текущий мог смениться
    импортом, и новая версия поехала бы по чужим точкам.
    """
    parent = service.get_plan(plan_id)
    if parent is None:
        raise HTTPException(404, "План не найден")
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
