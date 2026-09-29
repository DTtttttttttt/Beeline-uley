"""Ручное переназначение заявки (PLAN 6.16).

Диспетчер выбирает бригаду и позицию, сервер проверяет ограничения и либо отдаёт новую
версию плана, либо объясняет, что нарушится. Правка делается **поверх прожитого дня**:
закреплённая часть маршрутов остаётся как есть, пересматривается только остаток.

Своей проверки ограничений здесь нет — только `schedule.first_violation` и
`schedule.best_insertion`; план из готовых маршрутов собирает `service.build_plan`.
"""

from dataclasses import dataclass

from ..models import Engineer, Plan, ReplanMode, Request, Stop
from ..routing.matrices import Travel
from ..timeutil import to_seconds
from .explain import close_refusal, manual_breaks_route, manual_refusal
from .replan import ReplanError, State, continuation
from .schedule import (
    Position,
    Visit,
    best_insertion,
    first_violation,
    run,
    start_positions,
)


@dataclass(frozen=True)
class Manual:
    """Результат ручной правки: остаток дня по бригадам и закрепления (PLAN 6.16)."""

    routes: dict[str, list[Request]]
    pinned: dict[str, str]


def allows(pinned: dict[str, str], request_id: str, engineer_id: str) -> bool:
    """Закреплённая вручную заявка разрешена только своей бригаде (PLAN 6.16, п. 6).

    Одно правило на все пути назначения: отсюда его берут отбор кандидатов (а через него
    модель OR-Tools), базовый вариант и досчёт вставкой. Если закреплённая бригада заявку
    уже не берёт — выбыла, не хватает времени, — она просто остаётся без неё, и закрепление
    снимается само: в план попадают только выполненные закрепления (`service._pins`).
    """
    return pinned.get(request_id, engineer_id) == engineer_id


def day(parent: Plan, travel: Travel) -> State:
    """День таким, каким его оставило последнее событие: что закреплено и откуда продолжают.

    Фиксация не пересчитывается по времени заново, а читается из самих стопов
    (`stop.committed`): их уже сверила независимая проверка (PLAN 6.7), а повторный расчёт
    по `T` захватил бы стоп, выехавший ровно в момент события.

    Событие и режим наследуются от родителя, а не обнуляются: без них проверка отвергла бы
    закреплённые стопы («закреплена, хотя план считался не с момента события»), а остаток
    дня посчитался бы от начала смены вместо `T`. Ручная правка не отменяет прожитый день,
    она его правит.

    Момент события берётся по тому же условию, что и в независимой проверке: при
    `from_event` и `insert`. В режиме `full` день считается заново от начала смен — взять оттуда `T`
    значило бы сдвинуть на него все двенадцать маршрутов, а проверка считала бы их от
    начала смены и не сошлась бы.

    Точка продолжения при этом считается **по закреплённой части, а не по режиму**: в
    режиме `full` закрепляется закрытое фактом (PLAN 6.19), и у бригады с закрытой заявкой
    остаток дня обязан начинаться после неё. Со стартовой точкой начала дня голова и хвост
    маршрута поехали бы от одного и того же часа.
    """
    known = {request.id: request for request in parent.input.requests}
    engineers = {engineer.id: engineer for engineer in parent.input.engineers}
    time = (
        to_seconds(parent.event.time)
        if parent.event and parent.replan_mode is not ReplanMode.FULL
        else None
    )

    committed: dict[str, list[Request]] = {}
    positions: dict[str, Position] = start_positions(parent.input)
    for route in parent.routes:
        engineer = engineers.get(route.engineer_id)
        if engineer is None:  # чужой маршрут: такой план не прошёл бы проверку
            continue
        committed[route.engineer_id] = [
            known[stop.request_id] for stop in route.stops if stop.committed
        ]
        if time is not None or committed[route.engineer_id]:
            positions[route.engineer_id] = continuation(
                parent.input.office,
                engineer,
                committed[route.engineer_id],
                travel,
                time or 0,
            )
    return State(
        parent,
        parent.event,
        parent.replan_mode,
        time or 0,
        list(parent.input.requests),
        list(parent.input.engineers),
        committed,
        positions,
        dict(parent.deferred),
        dict(parent.closed),
    )


def move(
    state: State,
    travel: Travel,
    request_id: str,
    engineer_id: str | None,
    position: int | None,
) -> Manual:
    """Заявка уходит к выбранной бригаде (PLAN 6.16, шаги 1–4). `engineerId = null` — снять.

    `position` — куда вставить в маршрут выбранной бригады: индекс с нуля, считая
    закреплённые стопы и **без** самой переносимой заявки, то есть ровно то, что диспетчер
    видит в таблице плана. `None` — лучшая позиция по приросту пробега.
    """
    parent = state.parent
    requests = {request.id: request for request in state.requests}
    engineers = {engineer.id: engineer for engineer in state.engineers}
    if request_id not in requests:
        raise ReplanError(400, f"Заявки №{request_id} нет в плане")
    if engineer_id is not None and engineer_id not in engineers:
        raise ReplanError(400, f"Инженера {engineer_id} нет в плане")

    request = requests[request_id]
    was = parent.assignments.get(request_id)
    _check_free(state, request_id, was, engineers)
    if engineer_id is None and was is None:
        raise ReplanError(400, f"Заявка №{request_id} и так не назначена")

    # Остаток дня по всем бригадам: закреплённое сюда не входит — его не пересматривают.
    routes = {
        engineer.id: [
            requests[stop.request_id]
            for stop in _stops(parent, engineer.id)
            if not stop.committed and stop.request_id != request_id
        ]
        for engineer in state.engineers
    }
    if was is not None and was != engineer_id:
        _check_left(engineers[was], routes[was], request, travel, state.positions[was])

    if engineer_id is None:
        return Manual(routes, _without(parent.pinned, request_id))

    engineer, at = engineers[engineer_id], state.positions[engineer_id]
    if engineer.unavailable_from is not None:
        # Иначе отказ соврал бы: у выбывшей бригады день начинается концом смены, и любая
        # вставка упирается в окно или в смену — причина называлась бы не та (PLAN 6.12).
        raise ReplanError(
            409,
            f"Нельзя: {engineer.name} — с {engineer.unavailable_from} не работает, "
            "новых заявок не берёт",
        )
    route = routes[engineer_id]
    index = _index(state, engineer, route, request, travel, position)
    attempt = route[:index] + [request] + route[index:]
    violation = first_violation(engineer, attempt, travel, at)
    if violation is not None:
        culprit, visit = _culprit(engineer, attempt, travel, at)
        raise ReplanError(
            409,
            manual_refusal(
                engineer,
                culprit,
                violation,
                visit,
                route,
                _spots(engineer, route, request, travel, at),
            ),
        )

    routes[engineer_id] = attempt
    return Manual(routes, parent.pinned | {request_id: engineer_id})


def _check_free(
    state: State,
    request_id: str,
    was: str | None,
    engineers: dict[str, Engineer],
) -> None:
    """Закрытую, закреплённую и перенесённую заявку ручная правка не трогает (PLAN 6.16, шаг 1).

    Перенесённая на следующий день отклоняется здесь, а не молча ставится в маршрут: в
    расчёт остатка дня она не входит (PLAN 6.19), и план с ней в маршруте не прошёл бы
    независимую проверку — диспетчер увидел бы 500 вместо причины. Закрытая проверяется
    первой: она попадает и в закреплённые (исход «выполнена»), и в перенесённые («не
    смогли»), но отказ ей нужен свой — про факт, а не про момент события.
    """
    if request_id in state.closed:
        # Факт записан: и «выполнена», и «не смогли» — одинаково не предмет для правки.
        raise ReplanError(409, close_refusal(request_id, state.closed[request_id]))
    fixed = {
        request.id for requests in state.committed.values() for request in requests
    }
    if request_id in fixed:
        name = engineers[was].name if was in engineers else was
        raise ReplanError(
            409,
            f"Заявку №{request_id} менять нельзя: её выполняет {name} — "
            "на момент события работа уже шла или инженер был в пути",
        )
    if request_id in state.deferred:
        raise ReplanError(
            409,
            f"Заявку №{request_id} менять нельзя: она перенесена на следующий день "
            f"({state.deferred[request_id]})",
        )


def _check_left(
    engineer: Engineer,
    route: list[Request],
    moved: Request,
    travel: Travel,
    at: Position,
) -> None:
    """Маршрут прежней бригады без снятой заявки (PLAN 6.16, шаг 3).

    Снятие среднего стопа меняет плечо «предыдущий → следующий», и его может не быть
    (PLAN 3.4): такой маршрут не строится вовсе. Времена от снятия только уменьшаются,
    поэтому окно и смена сломаться не могут, но проверяем тем же вызовом — своей проверки
    ограничений в проекте нет.
    """
    violation = first_violation(engineer, route, travel, at)
    if violation is None:
        return
    culprit, visit = _culprit(engineer, route, travel, at)
    raise ReplanError(
        409, manual_breaks_route(engineer, moved, culprit, violation, visit)
    )


def _index(
    state: State,
    engineer: Engineer,
    route: list[Request],
    request: Request,
    travel: Travel,
    position: int | None,
) -> int:
    """Позиция диспетчера — в индекс остатка дня. Закреплённый префикс не раздвигается."""
    if position is None:
        spot = best_insertion(
            engineer, route, request, travel, state.positions[engineer.id]
        )
        # Не встаёт никуда: место всё равно нужно, чтобы вызвать отказ с причиной.
        return len(route) if spot is None else spot[0]

    offset = len(state.committed.get(engineer.id, []))
    if position < offset:
        raise ReplanError(
            409,
            f"{engineer.name}: первые {offset} стопов закреплены, "
            "перед ними заявку поставить нельзя",
        )
    if position - offset > len(route):
        raise ReplanError(400, f"{engineer.name}: в маршруте нет такой позиции")
    return position - offset


def _spots(
    engineer: Engineer,
    route: list[Request],
    request: Request,
    travel: Travel,
    at: Position,
) -> list[int]:
    """Все места остатка дня, куда заявка встаёт. Нужны только тексту отказа."""
    return [
        index
        for index in range(len(route) + 1)
        if first_violation(
            engineer, route[:index] + [request] + route[index:], travel, at
        )
        is None
    ]


def _culprit(
    engineer: Engineer, route: list[Request], travel: Travel, at: Position
) -> tuple[Request, Visit | None]:
    """Из-за какой заявки маршрут недопустим и что с ней по расписанию.

    `first_violation` отдаёт **первое** нарушение в порядке маршрута, поэтому самый
    короткий префикс с нарушением кончается ровно на виноватой заявке. Виноватой может
    оказаться и соседняя: вставка сдвигает всё, что стоит после неё.
    """
    for size in range(1, len(route) + 1):
        visits, violation = run(engineer, route[:size], travel, at)
        if violation is None:
            continue
        # Список короче префикса — до этого стопа нет пути, и времён у него не будет.
        # Судить об этом по коду нарушения нельзя: непроходимый переезд обрывает маршрут,
        # а наружу может уйти более ранний отказ по ресурсу (PLAN 6.2).
        return route[size - 1], visits[-1] if len(visits) == size else None
    raise ValueError(f"{engineer.id}: допустимый маршрут отказом не бывает")


def _stops(plan: Plan, engineer_id: str) -> list[Stop]:
    """Стопы бригады в родительском плане; маршрут есть у каждой, даже пустой (PLAN 5.2)."""
    return next(
        (route.stops for route in plan.routes if route.engineer_id == engineer_id), []
    )


def _without(pinned: dict[str, str], request_id: str) -> dict[str, str]:
    """Снятая с бригады заявка закрепления не несёт: закреплять её не за кем."""
    return {key: value for key, value in pinned.items() if key != request_id}
