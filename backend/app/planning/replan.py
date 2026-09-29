"""Перепланирование после события (PLAN 6.12).

Отслеживания исполнения у нас нет: состояние дня на момент события `T` **моделируется по
текущему плану**. Всё, к чему бригада уже выехала, закрепляется и остаётся как есть;
пересчитывается только остаток дня. Планы неизменяемы, поэтому событие создаёт новую
версию с `parentPlanId`, а не правит прежний документ.

Здесь только подготовка состояния: сборку плана из него делает `service.build_plan` —
одна точка сборки на первичный расчёт и на перепланирование.
"""

from dataclasses import dataclass

from ..config import settings
from ..dictionaries import Outcome, WorkPriority
from ..models import (
    AddRequestEvent,
    CloseRequestEvent,
    Closure,
    Engineer,
    Event,
    Office,
    Plan,
    ReplanMode,
    Request,
    Stop,
)
from ..routing.matrices import Travel, point_id
from ..timeutil import to_clock, to_seconds
from .explain import (
    close_before_departure,
    close_not_started,
    close_over_closed,
    close_refusal,
    edit_moves_committed,
    edit_refusal,
)
from .schedule import (
    Position,
    build_schedule,
    day_start,
    first_violation,
    free_from,
    start_positions,
    units,
)


class ReplanError(Exception):
    """Правку дня нельзя применить. `status` уходит HTTP-кодом (PLAN 5.4).

    Одна на событие (PLAN 6.12) и на ручное переназначение (PLAN 6.16): отказ у них
    устроен одинаково — код и готовая фраза диспетчеру, — и обработчик в `api/plans.py`
    у обоих один.
    """

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class State:
    """День на момент события: что уже закреплено и откуда бригады продолжают."""

    parent: Plan
    # Нового события может и не быть: ручная правка (PLAN 6.16) описывает тем же состоянием
    # день, каким его оставило **прошлое** событие, а у первичного плана его нет вовсе.
    event: Event | None
    mode: ReplanMode | None
    time: int  # T, секунды от полуночи
    requests: list[Request]  # все заявки дня после события — это и есть `plan.input`
    engineers: list[Engineer]  # бригады дня: у родителя плюс вышедшая по событию
    committed: dict[str, list[Request]]  # закреплённый префикс маршрута по бригадам
    positions: dict[str, Position]  # точка продолжения и `availableFrom`
    deferred: dict[str, str]  # заявка → время переноса на следующий день (PLAN 6.19)
    closed: dict[str, Closure]  # заявка → факт от диспетчера (PLAN 6.19)

    def free(self) -> list[Request]:
        """Заявки, которые планируются заново: всё, что ещё не закреплено и не перенесено."""
        fixed = {
            request.id for requests in self.committed.values() for request in requests
        }
        return [
            request
            for request in self.requests
            if request.id not in fixed and request.id not in self.deferred
        ]


ADDING = ("add_request", "urgent_request")


def default_mode(event: Event) -> ReplanMode:
    """Режим пересчёта, если диспетчер его не выбрал (PLAN 6.12, ответы организаторов).

    Обычная заявка, пришедшая днём, встраивается в свободное время и уже сформированный
    план не перестраивает. Авария — другое дело: ради неё можно перестроить остаток дня.
    Так же и заявка, которую диспетчер пометил срочной: срочность при перепланировании
    выше (ТЗ, п. 2.4.1), а во встраивании ей не за счёт чего пройти вперёд других.
    Остальные события меняют сам день и пересчитывают остаток.
    """
    if event.type in ADDING and not _urgent(event):
        return ReplanMode.INSERT
    return ReplanMode.FROM_EVENT


def _urgent(event: AddRequestEvent) -> bool:
    """Срочна ли заявка события (PLAN 6.12).

    Явный флаг события главнее всего; без него — флаг в самой заявке, авария или прежнее
    имя события `urgent_request`, которое само означает срочность.
    """
    if event.urgent is not None:
        return event.urgent
    return (
        event.request.urgent
        or event.request.work_priority is WorkPriority.EMERGENCY
        or event.type == "urgent_request"
    )


def prepare(parent: Plan, event: Event, mode: ReplanMode, travel: Travel) -> State:
    """Фиксация, точки продолжения и применённое событие (PLAN 6.12, шаги 1–3)."""
    if mode is ReplanMode.INSERT and event.type not in ADDING:
        # Встроить можно только новую заявку: отмена, выбытие или правка меняют сам день,
        # и оставить остаток «как был» у них значило бы отдать план, который им противоречит.
        raise ReplanError(
            400, "Встроить в план без пересчёта можно только новую заявку"
        )
    time = to_seconds(event.time)
    _check_order(parent, time)
    known = {request.id: request for request in parent.input.requests}
    names = {engineer.id: engineer.name for engineer in parent.input.engineers}

    requests = list(parent.input.requests)
    engineers = list(parent.input.engineers)
    # Перенос и факт наследуются цепочкой: заявку, которую диспетчер отложил или закрыл,
    # день не возвращает.
    deferred = dict(parent.deferred)
    closed = dict(parent.closed)
    # Факт разбирается **до** фиксации: закрытый стоп закрепляется независимо от того, выехала
    # ли к нему бригада до `T`, и в режиме `full` тоже (PLAN 6.19), а фиксация обязана это знать.
    if isinstance(event, CloseRequestEvent):
        closed[event.request_id] = _closure(
            parent, event, known, deferred, closed, time
        )

    committed, positions = _fixation(parent, known, time, mode, travel, closed)
    match event.type:
        case "add_request" | "urgent_request":
            requests.append(_added(event, known, _not_before(time, mode)))
        case "add_engineer":
            engineer = _joined(event.engineer, parent, time, mode)
            engineers.append(engineer)
            positions[engineer.id] = free_from(parent.input.office, engineer)
        case "update_request":
            _check_not_started(
                event.request.id, known, _started(parent, time), names, closed
            )
            requests = [
                event.request if item.id == event.request.id else item
                for item in requests
            ]
        case "update_engineer":
            engineers = _edited(
                engineers, event.engineer, committed, travel, parent, time, mode
            )
            # Точка продолжения пересчитывается по правленой бригаде: у неё могли
            # сдвинуться времена закреплённой части, переехать дом или смениться
            # транспорт — со старой позицией остаток дня разошёлся бы с началом.
            # Режим здесь не развилка, а граница `_not_before`: в `full` закреплённым
            # остаётся только закрытое фактом, и считать его надо тем же `continuation`,
            # иначе голова и хвост маршрута поедут от одного и того же начала дня.
            edited = _by_id(engineers, event.engineer.id)
            positions[event.engineer.id] = continuation(
                parent.input.office,
                edited,
                committed.get(event.engineer.id, []),
                travel,
                _not_before(time, mode),
            )
        case "defer_request":
            _check_all_not_started(
                event.request_ids, known, parent, time, names, closed
            )
            for request_id in event.request_ids:
                deferred[request_id] = event.time
        case "cancel_request":
            _check_all_not_started(
                event.request_ids, known, parent, time, names, closed
            )
            gone = set(event.request_ids)
            requests = [item for item in requests if item.id not in gone]
            # Отменённой заявки в дне больше нет, и перенос на завтра к ней уже
            # не относится: иначе `deferred` копил бы идентификаторы, которых нет во входе.
            for request_id in gone:
                deferred.pop(request_id, None)
        case "close_request":
            closure = closed[event.request_id]
            if closure.outcome is Outcome.FAILED:
                # Заявка не сделана: она уходит из маршрута в перенос на следующий день,
                # а бригада продолжает день от её адреса (PLAN 6.19).
                _release(
                    parent,
                    event.request_id,
                    closure,
                    committed,
                    positions,
                    travel,
                    time,
                    mode,
                )
                deferred[event.request_id] = closure.time
        case "engineer_unavailable":
            engineers = _unavailable(engineers, event.engineer_id, time)
            positions[event.engineer_id] = Position(
                positions[event.engineer_id].point_id,
                to_seconds(_by_id(engineers, event.engineer_id).shift_end),
                equipment_used=positions[event.engineer_id].equipment_used,
            )

    return State(
        parent,
        event,
        mode,
        time,
        requests,
        engineers,
        committed,
        positions,
        deferred,
        closed,
    )


def _check_order(parent: Plan, time: int) -> None:
    """Событие не может быть раньше предыдущего (PLAN 20.1).

    Проверка формально из блока 20, но без неё не держится обещание блока 11: фиксация
    считается по моменту события, поэтому время назад превратило бы в свободные те стопы,
    которые прошлая версия объявила начатыми, — и такой план прошёл бы проверку, ведь `T`
    она берёт из него самого.
    """
    floor = earliest(parent)
    if time < floor:
        before = (
            f"последнего факта или переноса ({to_clock(floor)})"
            if parent.replan_mode is ReplanMode.FULL
            else f"предыдущего ({to_clock(floor)})"
        )
        raise ReplanError(
            400,
            f"Событие в {to_clock(time)} раньше {before}: "
            "время в цепочке событий идёт только вперёд",
        )


def earliest(plan: Plan) -> int:
    """Раньше какого момента нельзя событие поверх этой версии (PLAN 20.1, блок 33).

    Обычно — момент её события. Версия «весь день заново» считается известной с утра и
    стоит на шкале дня с 00:00 (блок 33): закреплено в ней только записанное фактом, и
    событие раньше самого пересчёта ничего начатого не размораживает. Раньше факта
    нельзя — диспетчер записал, что уже случилось. Ручная правка наследует событие
    и режим родителя, поэтому граница у неё та же.
    """
    if plan.event is None:
        return 0
    if plan.replan_mode is not ReplanMode.FULL:
        return to_seconds(plan.event.time)
    facts = [closure.time for closure in plan.closed.values()]
    return max(map(to_seconds, [*facts, *plan.deferred.values()]), default=0)


def _started(parent: Plan, time: int) -> dict[str, str]:
    """Заявка → бригада для всего, к чему бригада выехала не позже `T`.

    Считается независимо от режима: отменить начатую работу нельзя и в режиме `full` —
    иначе сравнительный расчёт делал бы вид, что её не было.
    """
    return {
        stop.request_id: route.engineer_id
        for route in parent.routes
        for stop in route.stops
        if to_seconds(stop.departure) <= time
    }


def _fixation(
    parent: Plan,
    known: dict[str, Request],
    time: int,
    mode: ReplanMode,
    travel: Travel,
    closed: dict[str, Closure],
) -> tuple[dict[str, list[Request]], dict[str, Position]]:
    """Стоп закреплён, если выезд к нему ≤ `T`: работа выполнена, идёт или бригада уже в пути.

    Выезды по маршруту возрастают, поэтому закреплённые стопы — всегда его начало.
    В режиме `full` правило «выезд ≤ `T`» не действует: день считается заново от начала смен.
    Единственное, что закрепляется в обоих режимах, — закрытое фактом (PLAN 6.19): диспетчер
    записал, чем кончилась работа, и пересматривать её не может уже никакой пересчёт.

    Время продолжения берётся из **пересчёта** закреплённой части, а не из напечатанных
    в плане часов: наружу время уходит с точностью до минуты (PLAN 2.6), и секунды в нём
    уже потеряны. От усечённого окончания остаток дня поехал бы на секунды раньше, чем
    может, и независимая проверка это ловит — она-то считает секунды.
    """
    engineers = {engineer.id: engineer for engineer in parent.input.engineers}
    committed: dict[str, list[Request]] = {}
    # Бригада без собственного маршрута в плане невозможна, но словарь позиций обязан быть
    # полным: дальше по коду позиция берётся по ключу, а не «если найдётся».
    positions: dict[str, Position] = start_positions(parent.input)
    for route in parent.routes:
        engineer = engineers.get(route.engineer_id)
        if engineer is None:  # чужой маршрут: такой план не прошёл бы проверку
            continue
        requests = [
            known[stop.request_id] for stop in _fixed(route.stops, time, mode, closed)
        ]
        if not requests and mode is ReplanMode.FULL:
            continue  # день этой бригады считается с начала смены, позиция уже стоит
        # Закреплённая часть пересчитывается от **начала дня** бригады: у вышедшей в
        # течение дня это время её выхода, а не начало смены. Иначе на следующем событии
        # цепочки её утренние часы «освободились» бы, и день оказался бы занят дважды.
        committed[route.engineer_id] = requests
        positions[route.engineer_id] = continuation(
            parent.input.office, engineer, requests, travel, _not_before(time, mode)
        )
    return committed, positions


def _fixed(
    stops: list[Stop], time: int, mode: ReplanMode, closed: dict[str, Closure]
) -> list[Stop]:
    """Стопы, которые в этой версии не пересматриваются: начатые и закрытые фактом.

    Берётся **набор**, а не префикс до последнего подходящего. Соседа закреплённого стопа
    закреплять не за что: в режиме `full` у него нет ни выезда до `T` (правило там не
    действует), ни факта, и независимая проверка справедливо ругается «закреплена, хотя
    план считался не с момента события». Порядок от этого не страдает: новая версия
    собирается как «закреплённое плюс остаток» (`service._full`), поэтому закреплённые
    стопы всегда идут первыми, даже если в прежнем маршруте между ними что-то стояло.
    Их времена пересчитываются подряд от начала дня — как и всё сделанное.
    """
    return [
        stop
        for stop in stops
        if (mode is not ReplanMode.FULL and to_seconds(stop.departure) <= time)
        or stop.request_id in closed
    ]


def _not_before(time: int, mode: ReplanMode) -> int:
    """Раньше какого времени остаток дня не начинается.

    В режимах `from_event` и `insert` это момент события, в `full` — ничего: день там
    считается заново, и прижимать его к `T` значило бы сделать сравнительный расчёт
    неполноценным.
    """
    return 0 if mode is ReplanMode.FULL else time


def continuation(
    office: Office,
    engineer: Engineer,
    committed: list[Request],
    travel: Travel,
    not_before: int,
) -> Position:
    """Откуда и когда бригада продолжает день после закреплённой части (PLAN 6.12).

    Считается по той бригаде, которая в дне сейчас: правка смены или транспорта меняет
    времена закреплённой части, и позиция обязана меняться вместе с ней — иначе остаток
    дня поедет от чужого времени, а независимая проверка это поймает (PLAN 6.19).

    `not_before` — момент события в режиме `from_event` и ноль в `full` (`_not_before`).
    """
    began = day_start(office, engineer)
    visits = build_schedule(engineer, committed, travel, began)
    free = free_from(office, engineer)
    if engineer.unavailable_from is not None:
        # Выбывшая бригада новых работ не берёт и после следующего события тоже:
        # её состояние записано в самой бригаде (PLAN 6.12), а не только здесь.
        when = free.available_from
    elif visits:
        when = max(not_before, visits[-1].end)
    else:
        # Бригада ничего не успела. День продолжается с её начала — начала смены или
        # времени выхода, — но не раньше события. Начало дня здесь обязательно: в режиме
        # `full` границы события нет вовсе, и без него вышедшая в 12:00 бригада получила
        # бы утренние часы, которых у неё не было.
        when = max(not_before, began.available_from or 0)
    # Оборудование закреплённой части уже унесено, и остатку дня достаётся только то, что
    # осталось от дневной вместимости (PLAN 6.2).
    return Position(
        point_id(committed[-1].id) if committed else began.point_id,
        when,
        equipment_used=units(committed),
    )


def _added(event: AddRequestEvent, known: dict[str, Request], time: int) -> Request:
    """Заявка, поступившая в течение дня, сохраняет свой приоритет: он определяется типом работы.

    Срочность — `_urgent` (PLAN 6.12). Начало окна прижимается к моменту события: раньше,
    чем заявка поступила, её не начать (ответ 5). У аварии конец окна — не позже срока
    реакции от этого начала: на сетевую аварию закладывается 1–2 часа (ответ 19). Считается
    от начала окна, а не от события: аварию, которую назначили на вечер, срок реакции не
    должен сделать невыполнимой.

    `time` — граница `_not_before`: в режиме `full` ноль. Там заявка известна с утра
    (блок 33), и прижимать её окно к моменту события значило бы считать день наполовину
    «с утра», наполовину «с момента события».
    """
    request = event.request
    if request.id in known:
        raise ReplanError(400, f"Заявка №{request.id} уже есть в плане")
    start = max(to_seconds(request.window_start), time)
    end = to_seconds(request.window_end)
    if start >= end:
        raise ReplanError(
            400,
            f"Окно заявки №{request.id} ({request.window_start}–{request.window_end}) "
            f"закончилось до момента события {to_clock(time)}",
        )
    if request.work_priority is WorkPriority.EMERGENCY:
        end = min(end, start + settings.emergency_response_min * 60)
    return Request.model_validate(
        request.model_dump()
        | {
            "urgent": _urgent(event),
            "window_start": to_clock(start),
            "window_end": to_clock(end),
        }
    )


def _joined(engineer: Engineer, parent: Plan, time: int, mode: ReplanMode) -> Engineer:
    """Бригада, вышедшая в течение дня (PLAN 6.19). Проверки — те же, что у бригад набора.

    Время выхода записывается в саму бригаду (`availableFrom`) и уезжает в `plan.input`:
    её день начинается не с начала смены, и следующее событие цепочки обязано это видеть,
    иначе закреплённые стопы пересчитаются с утра и день окажется занят дважды.
    В режиме `full` день считается так, будто бригада была с утра: это сравнительный
    расчёт, и ограничение по времени выхода в нём не действует (PLAN 6.12).
    """
    if any(item.id == engineer.id for item in parent.input.engineers):
        raise ReplanError(400, f"Инженер {engineer.id} уже есть в плане")
    if mode is ReplanMode.FULL:
        return engineer
    if to_seconds(engineer.shift_end) <= time:
        raise ReplanError(
            400,
            f"Смена инженера {engineer.name} ({engineer.shift_start}–{engineer.shift_end}) "
            f"закончилась до момента события {to_clock(time)}",
        )
    return engineer.model_copy(update={"available_from": to_clock(time)})


def _edited(
    engineers: list[Engineer],
    new: Engineer,
    committed: dict[str, list[Request]],
    travel: Travel,
    parent: Plan,
    time: int,
    mode: ReplanMode,
) -> list[Engineer]:
    """Правка бригады в течение дня (PLAN 6.19).

    Состояние дня (`availableFrom`, `unavailableFrom`) остаётся прежним: его задают события,
    а не форма правки. Закреплённые стопы не пересматриваются, поэтому правка, из-за которой
    уже начатая работа перестаёт помещаться или начинается позже момента события — сократили
    смену, убрали навык, сменили транспорт, переехал дом, — отклоняется целиком: план с
    нарушением или с ложным закреплением отдавать нельзя. Правку, упирающуюся во **время**,
    принимает режим `full`: правило «выезд ≤ `T`» там не действует. Правку, ломающую
    ограничение (навык, транспорт, оборудование, смена), не принимает и он: закрытая фактом
    заявка закреплена в обоих режимах, и план с нарушением по ней остался бы нарушением.
    """
    if not any(item.id == new.id for item in engineers):
        raise ReplanError(400, f"Инженера {new.id} нет в плане")
    old = _by_id(engineers, new.id)
    updated = new.model_copy(
        update={
            "available_from": old.available_from,
            "unavailable_from": old.unavailable_from,
        }
    )
    fixed = committed.get(new.id, [])
    if fixed:
        began = day_start(parent.input.office, updated)
        violation = first_violation(updated, fixed, travel, began)
        if violation is not None:
            raise ReplanError(409, edit_refusal(updated, fixed, violation))
        # Закрепление держится на том, что выезд был не позже `T`. Правка смены,
        # транспорта или дома двигает времена закреплённой части, и выезд может уехать
        # за момент события — тогда «работа уже начата» перестаёт быть правдой, и план
        # противоречил бы сам себе (это ловит и независимая проверка, отвечая 500).
        # В режиме `full` этого правила нет: там закреплено только закрытое фактом, и
        # держится оно на факте, а не на часах.
        if mode is not ReplanMode.FULL:
            for request, visit in zip(
                fixed, build_schedule(updated, fixed, travel, began), strict=True
            ):
                if visit.departure > time:
                    raise ReplanError(
                        409,
                        edit_moves_committed(updated, request, visit.departure, time),
                    )
    return [updated if item.id == new.id else item for item in engineers]


def _check_all_not_started(
    request_ids: list[str],
    known: dict[str, Request],
    parent: Plan,
    time: int,
    names: dict[str, str],
    closed: dict[str, Closure],
) -> None:
    """То же для списка: событие применяется целиком, поэтому проверяются все заявки до правки.

    Отказ называет **первую** неподходящую заявку — те же слова, что у события над одной, а
    после исправления списка диспетчер получит следующую, если она есть.
    """
    started = _started(parent, time)
    for request_id in request_ids:
        _check_not_started(request_id, known, started, names, closed)


def _check_not_started(
    request_id: str,
    known: dict[str, Request],
    started: dict[str, str],
    names: dict[str, str],
    closed: dict[str, Closure],
) -> None:
    """Заявка есть в плане, не закрыта фактом и работа по ней ещё не началась (PLAN 6.12, шаг 3).

    Одна проверка на отмену, правку и перенос: начатую работу ни одно из трёх событий не
    трогает — закрыть её можно только фиксацией факта (PLAN 6.19).

    Закрытая проверяется отдельно и первой. Заявка с исходом «не выполнена» из маршрутов
    ушла, поэтому «работа уже началась» её больше не ловит, а отменить её значило бы стереть
    записанный факт: сама заявка выпала бы из входа плана, а запись о ней осталась — такой
    план не прошёл бы независимую проверку.
    """
    if request_id not in known:
        raise ReplanError(400, f"Заявки №{request_id} нет в плане")
    if request_id in closed:
        raise ReplanError(409, close_refusal(request_id, closed[request_id]))
    engineer_id = started.get(request_id)
    if engineer_id is not None:
        raise ReplanError(
            409,
            f"Работа уже началась: заявку №{request_id} "
            f"выполняет {names.get(engineer_id, engineer_id)}",
        )


def _closure(
    parent: Plan,
    event: CloseRequestEvent,
    known: dict[str, Request],
    deferred: dict[str, str],
    closed: dict[str, Closure],
    time: int,
) -> Closure:
    """Факт от диспетчера: что именно закрываем и можно ли (PLAN 6.19, ответ 1).

    Закрыть можно только работу, которая уже началась: к ней бригада выехала не позже `T`.
    Закрытие будущего — не факт, а выдумка, и следующее перепланирование о ней спорить не
    сможет: закрытый стоп закрепляется навсегда. По той же причине `actualEnd` не может
    быть позже события: диспетчер сообщает, чем работа кончилась, а не чем кончится.
    """
    request = known.get(event.request_id)
    if request is None:
        raise ReplanError(400, f"Заявки №{event.request_id} нет в плане")
    if to_seconds(event.at()) > time:
        raise ReplanError(
            409,
            f"Заявка №{request.id}: фактическое окончание {event.at()} позже события "
            f"{to_clock(time)} — факт сообщают о том, что уже случилось",
        )
    if event.request_id in closed:
        raise ReplanError(409, close_refusal(request.id, closed[event.request_id]))
    if event.request_id in deferred:
        raise ReplanError(
            409,
            f"Заявка №{request.id} перенесена на следующий день "
            f"({deferred[event.request_id]}): закрывать фактом нечего",
        )

    names = {engineer.id: engineer.name for engineer in parent.input.engineers}
    for route in parent.routes:
        for index, stop in enumerate(route.stops):
            if stop.request_id != event.request_id:
                continue
            if to_seconds(stop.departure) > time:
                raise ReplanError(
                    409,
                    close_not_started(
                        request,
                        names.get(route.engineer_id, route.engineer_id),
                        stop.departure,
                        time,
                    ),
                )
            if to_seconds(event.at()) < to_seconds(stop.departure):
                # Нижняя граница факта — выезд к заявке: кончиться раньше, чем бригада
                # к ней поехала, работа не могла. Верхняя (момент события) проверена выше;
                # без нижней в записи и в переносе стояло бы время до начала дня.
                raise ReplanError(
                    409,
                    close_before_departure(request, event.at(), stop.departure),
                )
            if event.outcome is Outcome.FAILED:
                # Невыполненная заявка уводит бригаду из маршрута обратно к себе, а всё,
                # что стояло после неё, планируется заново. Закрытое так не пересмотреть:
                # факт по нему уже записан, и отдавать его в пересчёт нельзя.
                later = next(
                    (
                        item.request_id
                        for item in route.stops[index + 1 :]
                        if item.request_id in closed
                    ),
                    None,
                )
                if later is not None:
                    raise ReplanError(409, close_over_closed(request, later))
            return Closure(
                outcome=event.outcome,
                time=event.at(),
                engineer_id=route.engineer_id,
            )
    raise ReplanError(
        409, f"Заявка №{request.id} никому не назначена: закрывать фактом нечего"
    )


def _release(
    parent: Plan,
    request_id: str,
    closure: Closure,
    committed: dict[str, list[Request]],
    positions: dict[str, Position],
    travel: Travel,
    time: int,
    mode: ReplanMode,
) -> None:
    """Исход `failed`: заявка уходит из дня, бригада освобождается (PLAN 6.19).

    Всё, что стояло в закреплённой части после невыполненной заявки, тоже возвращается в
    расчёт: до этого бригада не доехала — её день изменился с того момента, как работа
    сорвалась.

    **Крюк к невыполненной заявке в плане не остаётся.** День описывает работу, которую
    планировали, а независимая проверка пересчитывает его по самим маршрутам (PLAN 6.7);
    невидимый заезд, которого в маршруте нет, ей пришлось бы принять на веру. Поэтому
    заявка исчезает из дня целиком — вместе со своими километрами, — а от факта остаётся
    время: `actualEnd` не позже события (`_closure`), и остаток дня всё равно не начинается
    раньше `T`. Так пересчёт бригады и остаётся тем же, что у любой другой.
    """
    engineer_id = closure.engineer_id
    fixed = committed.get(engineer_id, [])
    index = next(
        (number for number, item in enumerate(fixed) if item.id == request_id),
        len(fixed),
    )
    committed[engineer_id] = fixed[:index]
    engineer = _by_id(parent.input.engineers, engineer_id)
    positions[engineer_id] = continuation(
        parent.input.office,
        engineer,
        committed[engineer_id],
        travel,
        _not_before(time, mode),
    )


def _unavailable(
    engineers: list[Engineer], engineer_id: str, time: int
) -> list[Engineer]:
    """Недоступная бригада освобождается только к концу смены.

    Отдельной ветки «бригада выбыла» в алгоритме нет: `unavailableFrom` превращается в
    `availableFrom` конца смены (`schedule.free_from`), и тогда ни один новый стоп в эту
    смену уже не помещается — это одинаково видят и проверка маршрута, и модель OR-Tools.
    Закреплённые стопы при этом остаются за ней, а остальные её заявки уходят в общий
    список сами собой.

    Признак живёт **в самой бригаде** и уезжает в `plan.input`: иначе следующее событие
    цепочки о выбытии не узнало бы и вернуло бы бригаде работу, которую диспетчер отменил.
    """
    if not any(item.id == engineer_id for item in engineers):
        raise ReplanError(400, f"Инженера {engineer_id} нет в плане")
    return [
        item.model_copy(update={"unavailable_from": to_clock(time)})
        if item.id == engineer_id
        else item
        for item in engineers
    ]


def _by_id(engineers: list[Engineer], engineer_id: str) -> Engineer:
    return next(item for item in engineers if item.id == engineer_id)
