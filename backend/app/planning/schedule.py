"""Расписание маршрута, проверка ограничений и лучшая вставка (PLAN 6.2).

Единственное место, где проверяются ограничения. Базовый вариант, отбор кандидатов, досчёт,
ручное переназначение и оценка риска зовут отсюда, а не пишут свою проверку: разъехавшиеся
копии — самая дорогая ошибка в этом проекте.

Внутри — целые секунды от полуночи и целые метры; перевод наружу делают мапперы.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum

from ..config import settings
from ..dictionaries import LUNCH_MIN, DurationMode
from ..models import Closure, Engineer, Input, Office, Request
from ..routing.matrices import Travel, point_id, start_id, turn_id
from ..timeutil import to_clock, to_meters, to_seconds
from .duration import seconds

# Короче этого свободный промежуток диспетчеру не показываем (PLAN 6.9).
MIN_IDLE_SEC = 10 * 60
LUNCH_SEC = LUNCH_MIN * 60


class Violation(StrEnum):
    """Почему маршрут недопустим. Первое нарушение в порядке маршрута (PLAN 6.2, 6.8)."""

    NO_SKILL = "NO_SKILL"
    NO_TRANSPORT = "NO_TRANSPORT"
    NO_EQUIPMENT = "NO_EQUIPMENT"
    # Оборудование есть, но столько за день бригада не унесёт (PLAN 6.2): расход — свойство
    # маршрута, а не пары «заявка — бригада», поэтому это не NO_EQUIPMENT.
    NO_CAPACITY = "NO_CAPACITY"
    # Valhalla вернула `null`: пути между точками для этого транспорта нет (PLAN 3.4).
    NO_ROUTE = "NO_ROUTE"
    WINDOW_END = "WINDOW_END"
    SHIFT_END = "SHIFT_END"


@dataclass(frozen=True)
class Finish:
    """Что известно о конце уже начатой работы (блок 68).

    `exact` — факт закрытия: работа кончилась ровно в `end`, раньше или позже плана. Иначе —
    «работа ещё шла в момент `end`»: затянулась сверх плана, и раньше этого она не кончится.
    """

    end: int
    exact: bool


def finishes(
    closed: Mapping[str, Closure], running: Mapping[str, str]
) -> dict[str, Finish]:
    """Факты дня для расписания: закрытия точные, «ещё идёт» — нижняя граница конца."""
    facts = {
        request_id: Finish(to_seconds(time), False)
        for request_id, time in running.items()
    }
    facts |= {
        request_id: Finish(to_seconds(closure.time), True)
        for request_id, closure in closed.items()
    }
    return facts


@dataclass(frozen=True)
class Turn:
    """Выезд на аварию (блок 74): не раньше `time`; при развороте в `time` бригада стояла в
    `point_id`, проехав `meters` по брошенному плечу. Без точки — «доделает и поедет»."""

    point_id: str | None
    time: int
    meters: int


@dataclass(frozen=True)
class Position:
    """Откуда и когда бригада продолжает день.

    При первичном расчёте — её стартовая точка и начало смены, при перепланировании — точка
    продолжения и время из PLAN 6.12. Проверка всегда идёт от текущего состояния бригады
    (PLAN 6.3).

    Умолчаний у полей нет намеренно: пока все стартовали из офиса, забытый аргумент был
    незаметен, а с домами бригад (PLAN 2.4) он молча отправлял бы маршрут не оттуда.
    """

    point_id: str
    available_from: int | None = None  # секунды; None — начало смены
    # Сколько единиц оборудования уже ушло на закреплённую часть дня (PLAN 6.2, 6.12):
    # пополнения нет, и остаток дня берёт только то, что осталось. Умолчания нет, как и у
    # точки: забытый аргумент молча вернул бы бригаде полный рюкзак.
    equipment_used: int = field(kw_only=True)
    # Как считается длительность работы (PLAN 9.1): режим дня из `Input`. Едет в позиции,
    # потому что позиция есть у каждого прохода по маршруту; умолчания нет по той же причине,
    # что у точки, — забытый режим молча считал бы день по нормативу.
    duration_mode: DurationMode = field(kw_only=True)
    # Факты по начатым работам (блок 68): конец такой работы — факт, а не план. Нужны только
    # началу дня (`day_start`), с которого пересчитывается сделанное; у точки продолжения их
    # нет — остаток дня начатых работ не содержит. Умолчания нет: забытый аргумент молча
    # вернул бы плановый конец работы, кончившейся раньше.
    finished: Mapping[str, Finish] = field(kw_only=True)
    # Развороты на аварию (блок 74) по аварии-якорю: перед её стопом бригада оказывается в
    # точке разворота. Как и факты — только у начала дня: разворот случился в сделанной части.
    # Умолчания нет: забытый аргумент молча вернул бы бригаду на брошенное плечо.
    turns: Mapping[str, Turn] = field(kw_only=True)
    # Обед уже был до этой позиции (блок 79): у начала дня — нет, у точки продолжения — если
    # он случился в закреплённой части или в простое после неё. Умолчания нет: забытый
    # аргумент молча дал бы бригаде второй обед или не дал бы ни одного.
    lunch_done: bool = field(kw_only=True)
    # Бригада стоит в точке законченной работы (блок 82): обедают только после работы, и у
    # начала дня обеда перед первым выездом нет, а у точки продолжения после работы — есть.
    # Умолчания нет по той же причине, что у `lunch_done`.
    after_work: bool = field(kw_only=True)


@dataclass(frozen=True)
class Visit:
    """Один стоп маршрута во внутренних единицах."""

    request_id: str
    departure: int
    arrival: int
    start: int
    end: int
    travel_sec: int
    travel_m: int
    work_sec: int  # расчётная работа пары; у работы с фактом `end − start` — уже факт
    # Обед перед выездом к этой работе, в точке, где бригада была (блок 79): начало и конец.
    lunch: tuple[int, int] | None = None


def day_start(
    office: Office,
    engineer: Engineer,
    mode: DurationMode,
    finished: Mapping[str, Finish],
) -> Position:
    """Где и когда у бригады **начался день** (PLAN 6.2).

    Стартовая точка — офис или её дом; время — начало смены, а у вышедшей в течение дня
    бригады время выхода. По этой позиции пересчитывается уже сделанное: закреплённый
    префикс маршрута и независимая проверка. `finished` — факты по начатым работам (блок 68).
    """
    return Position(
        start_id(office, engineer),
        None
        if engineer.available_from is None
        else to_seconds(engineer.available_from),
        equipment_used=0,  # день только начался: рюкзак собран, ничего не отдано
        duration_mode=mode,
        finished=finished,
        lunch_done=False,
        after_work=False,  # день только начался: ни одной работы позади
        turns={
            turn.emergency_id: Turn(
                None if turn.lat is None else turn_id(turn.lat, turn.lon),
                to_seconds(turn.time),
                to_meters(turn.km or 0),
            )
            for turn in engineer.dispatched
        },
    )


def free_from(office: Office, engineer: Engineer, mode: DurationMode) -> Position:
    """Откуда и когда бригада берёт **новую** работу.

    Отличается от `day_start` только у выбывшей бригады: ей новых заявок не достаётся,
    и выражено это концом смены — то же самое одинаково видят проверка маршрута и
    модель OR-Tools (PLAN 6.12).
    """
    if engineer.unavailable_from is not None:
        return Position(
            start_id(office, engineer),
            to_seconds(engineer.shift_end),
            equipment_used=0,
            duration_mode=mode,
            finished={},
            turns={},
            lunch_done=True,  # день бригады кончился: обедать в нём уже некогда
            after_work=False,
        )
    # Новая работа фактов и разворотов не знает: они только у начатой, а её здесь нет.
    return replace(day_start(office, engineer, mode, {}), turns={})


def start_positions(data: Input) -> dict[str, Position]:
    """Состояние бригад на начало дня: каждая — от своей стартовой точки (PLAN 6.2).

    Словарь заполняется на все бригады входа, поэтому дальше по коду позиция берётся по
    ключу, а не «по умолчанию, если не нашлось».
    """
    return {
        engineer.id: free_from(data.office, engineer, data.duration_mode)
        for engineer in data.engineers
    }


def begins(engineer: Engineer, position: Position) -> int:
    """С какой секунды бригада распоряжается днём: начало смены или точка продолжения.

    Одна формула на проход по маршруту и свободные окна: разойдись они — окна кандидата
    разошлись бы с расчётом.
    """
    when = to_seconds(engineer.shift_start)
    if position.available_from is not None:
        when = max(when, position.available_from)
    return when


def lunch_window(engineer: Engineer) -> tuple[int, int]:
    return to_seconds(engineer.lunch_start), to_seconds(engineer.lunch_end)


def lunch_due(engineer: Engineer, begin: int) -> bool:
    """Обязан ли обед (блок 79): бригада работает с начала, когда час ещё помещается в окно,
    и смена его накрывает. Вышедшая к концу окна или ушедшая до него — без обеда."""
    lunch_start, lunch_end = lunch_window(engineer)
    return (
        begin <= lunch_end - LUNCH_SEC
        and to_seconds(engineer.shift_end) >= lunch_start + LUNCH_SEC
    )


def day_end(engineer: Engineer) -> int:
    """Конец дня бригады: конец смены, у выбывшей — момент выбытия."""
    end = to_seconds(engineer.shift_end)
    if engineer.unavailable_from is not None:
        end = min(end, to_seconds(engineer.unavailable_from))
    return end


def lunch_slot(visits: list[Visit]) -> tuple[int, int] | None:
    """Когда бригада обедает в этих работах: обед, поставленный проходом (блоки 79, 82)."""
    return next((visit.lunch for visit in visits if visit.lunch is not None), None)


def lunch_after(engineer: Engineer, visits: list[Visit]) -> tuple[int, int] | None:
    """Час обеда сразу после последней из этих работ, если он помещается в окно (блок 82).

    Нужен точке продолжения: событие застало бригаду в простое после закреплённой части, и
    обед, начатый в этом простое, был — событие его не обрывает. До первой работы обеда нет.
    """
    if not visits:
        return None
    lunch_start, lunch_end = lunch_window(engineer)
    at = max(visits[-1].end, lunch_start)
    if at + LUNCH_SEC > min(lunch_end, day_end(engineer)):
        return None
    return at, at + LUNCH_SEC


def resource_checks(request: Request, engineer: Engineer) -> dict[Violation, bool]:
    """Навык, транспорт и оборудование по отдельности (PLAN 6.2, пп. 1–2).

    Объяснению нужны три ✓/✗ сразу (PLAN 6.9), а проверке маршрута — первое нарушение.
    Правила при этом остаются в одном экземпляре: `resource_violation` читает этот же словарь,
    и порядок ключей задаёт порядок нарушений.

    Инструменты входят в «оборудование»: это тот же ресурс «есть у бригады или нет», только
    без расхода (PLAN 5.1). Отдельный код нарушения размножил бы ✓/✗ кандидата и ветки
    объяснений ради разницы, которую и так называет текст причины.
    """
    return {
        Violation.NO_SKILL: request.skill in engineer.skills,
        Violation.NO_TRANSPORT: request.required_transport
        in (None, engineer.transport),
        Violation.NO_EQUIPMENT: set(request.required_equipment)
        <= set(engineer.equipment)
        and set(request.required_tools) <= set(engineer.tools),
    }


def capacity(engineer: Engineer) -> int | None:
    """Сколько единиц оборудования бригада берёт на день; `None` — без ограничения (PLAN 6.2)."""
    return settings.equipment_capacity.get(engineer.transport)


def units(requests: list[Request]) -> int:
    """Сколько единиц оборудования расходуют заявки: по одной каждого требуемого вида."""
    return sum(len(request.required_equipment) for request in requests)


def over_capacity(engineer: Engineer, used: int) -> bool:
    """Больше ли `used` единиц, чем бригада унесёт за день (PLAN 6.2).

    Единственное место правила: проход по маршруту, объяснения и чек-лист набора зовут
    отсюда, а не сравнивают с вместимостью сами.
    """
    limit = capacity(engineer)
    return limit is not None and used > limit


def resource_violation(request: Request, engineer: Engineer) -> Violation | None:
    """Первое нарушение по ресурсу; `None` — бригада подходит. От времени и маршрута не зависит."""
    checks = resource_checks(request, engineer)
    return next((violation for violation, ok in checks.items() if not ok), None)


def build_schedule(
    engineer: Engineer,
    requests: list[Request],
    travel: Travel,
    position: Position,
) -> list[Visit]:
    """Времена маршрута по формулам PLAN 6.2.

    Считаются и у недопустимого маршрута — они нужны объяснениям и причинам неназначения.
    Исключение — `NO_ROUTE`: продолжать не от чего, поэтому `ValueError`. Вызывать после
    `first_violation`; кому нужны обе половины ответа сразу — `run`.
    """
    visits, _ = run(engineer, requests, travel, position)
    # Короче маршрута список бывает только после непроходимого переезда: дальше ехать не от чего.
    # Проверять по коду нарушения нельзя — его мог занять более ранний отказ по ресурсу.
    if len(visits) != len(requests):
        raise ValueError(f"{engineer.id}: в маршруте есть непроходимый переезд")
    return visits


def first_violation(
    engineer: Engineer,
    requests: list[Request],
    travel: Travel,
    position: Position,
) -> Violation | None:
    """Первое нарушение в порядке маршрута; `None` — маршрут допустим."""
    return run(engineer, requests, travel, position)[1]


def culprit(
    engineer: Engineer, route: list[Request], travel: Travel, at: Position
) -> tuple[Request, Visit | None]:
    """Из-за какой заявки маршрут недопустим и что с ней по расписанию (PLAN 6.12, 6.16).

    Нужна ручной правке — назвать причину отказа — и перепланированию: подрезать
    стартовое решение до допустимого, сняв только виноватую заявку.

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


def meters(visits: list[Visit]) -> int:
    return sum(visit.travel_m for visit in visits)


def idle_slots(
    engineer: Engineer, visits: list[Visit], position: Position
) -> list[tuple[int, int]]:
    """Когда бригада свободна: промежутки дня, где она не работает и не едет (PLAN 6.9).

    Считается по готовым временам маршрута, своей проверки ограничений здесь нет. Окно
    кончается **за время переезда** до следующей работы: по расписанию бригада выезжает
    сразу и ждёт у адреса (PLAN 6.2), но распоряжаться она может только тем временем,
    после которого ещё успевает доехать. Окна короче `MIN_IDLE_SEC` не отдаются: на такое
    диспетчер всё равно ничего не поставит, а список кандидатов они удлиняют. У выбывшей
    бригады окон не остаётся — её день по `free_from` начинается концом смены.
    """
    shift_end = to_seconds(engineer.shift_end)
    when = begins(engineer, position)

    slots = []
    for visit in visits:
        # Обед перед выездом (блок 79) — тоже занятое время: окно до него и после.
        if visit.lunch is not None:
            if visit.lunch[0] - when >= MIN_IDLE_SEC:
                slots.append((when, visit.lunch[0]))
            when = visit.lunch[1]
        free_until = visit.start - visit.travel_sec
        if free_until - when >= MIN_IDLE_SEC:
            slots.append((when, free_until))
        when = visit.end
    if shift_end - when >= MIN_IDLE_SEC:
        slots.append((when, shift_end))
    return slots


def best_insertion(
    engineer: Engineer,
    requests: list[Request],
    new_request: Request,
    travel: Travel,
    position: Position,
) -> tuple[int, int] | None:
    """Куда вставить заявку в маршрут: позиция и прирост метров.

    `None` — не встаёт никуда. При равном приросте выбирается более ранняя позиция:
    два запуска на одних данных обязаны дать один результат. Маршрут, который бригада не
    проезжает (копия бригады на другом транспорте в «Что поможет»), тоже `None`: вставлять
    некуда, а не ошибка.
    """
    if resource_violation(new_request, engineer) is not None:
        return None

    visits, _ = run(engineer, requests, travel, position)
    if len(visits) != len(requests):
        return None
    base = meters(visits)
    best: tuple[int, int] | None = None
    for index in range(len(requests) + 1):
        route = requests[:index] + [new_request] + requests[index:]
        visits, violation = run(engineer, route, travel, position)
        if violation is not None:
            continue
        extra = meters(visits) - base
        if best is None or extra < best[1]:
            best = (index, extra)
    return best


def run(
    engineer: Engineer,
    requests: list[Request],
    travel: Travel,
    position: Position,
) -> tuple[list[Visit], Violation | None]:
    """Проход по маршруту: времена всех стопов и первое нарушение.

    Одна реализация формул на весь модуль — расписание и проверка не могут разойтись;
    `build_schedule` и `first_violation` берут отсюда разные половины ответа.

    Отсюда же их берёт тот, кому нужны обе сразу (PLAN 6.16): по коду нарушения о временах
    судить нельзя — непроходимый переезд обрывает маршрут, но наружу может уйти более
    ранний отказ по ресурсу. Признак обрыва один: список короче маршрута.
    """
    shift_end = to_seconds(engineer.shift_end)
    where = position.point_id
    when = begins(engineer, position)

    # Оборудование берётся в офисе на весь день, пополнения нет (ответ 4): счёт идёт от того,
    # что уже унесла закреплённая часть.
    used = position.equipment_used
    # Обед (блоки 79, 82) — один раз, между работами: после законченной работы, перед
    # выездом к следующей. До первой работы дня его нет (`worked`).
    hungry = not position.lunch_done and lunch_due(engineer, when)
    worked = position.after_work
    lunch_start, lunch_end = lunch_window(engineer)
    # Обед кончается в окне и в дне бригады: смена или выбытие внутри окна сужают его.
    lunch_last = min(lunch_end, day_end(engineer))

    visits: list[Visit] = []
    violation: Violation | None = None
    for request in requests:
        if violation is None:
            violation = resource_violation(request, engineer)
        taken = len(request.required_equipment)
        used += taken
        # Нарушает только заявка, которая сама что-то забирает: если закреплённая часть уже
        # вышла за вместимость (план посчитан при другой настройке), работа без оборудования
        # по-прежнему допустима, а начатое пересмотру не подлежит.
        if violation is None and taken and over_capacity(engineer, used):
            violation = Violation.NO_CAPACITY

        departure = when
        # Выезд на аварию (блок 74) — не раньше, чем её получили. Развёрнутая бригада выехала
        # по прежнему плечу, в момент разворота стояла в его точке и оттуда поехала к аварии:
        # проеханный кусок — часть этого переезда.
        lead_sec = lead_m = 0
        lunch = None
        turn = position.turns.get(request.id)
        if turn is not None and turn.point_id is None:
            # Свободная бригада стояла до аварии после работы: час окна в этом простое — её обед.
            at = max(when, lunch_start)
            if hungry and worked and at + LUNCH_SEC <= min(lunch_last, turn.time):
                lunch, hungry = (at, at + LUNCH_SEC), False
            departure = when = max(when, turn.time)
        elif turn is not None:
            lead_sec, lead_m = max(turn.time - when, 0), turn.meters
            where, when = turn.point_id, max(turn.time, when)
        leg = travel.travel(engineer.transport, where, point_id(request.id))
        if leg is None:
            # Пути нет: следующий выезд не от чего считать, дальше маршрута не существует.
            return visits, violation or Violation.NO_ROUTE
        travel_sec, travel_m = leg[0] + lead_sec, leg[1] + lead_m
        opens = to_seconds(request.window_start)
        work = seconds(request, engineer, position.duration_mode)
        # Обед — после работы, в её точке, перед выездом к этой (блоки 79, 82). Сейчас, если
        # он не сдвигает начало этой работы (уходит в ожидание окна) или если после неё час в
        # окно уже не встанет; иначе — позже, а если эта работа последняя, обеда в плане нет:
        # бригада свободна и поест после неё. Работа кончилась ближе часа к концу окна —
        # обеда нет. У выезда на аварию обеда перед выездом нет.
        if hungry and worked and turn is None:
            at = max(when, lunch_start)
            if at + LUNCH_SEC > lunch_last:
                hungry = False  # окно ушло: обедать некогда
            else:
                start0 = max(when + leg[0], opens)
                start1 = max(at + LUNCH_SEC + leg[0], opens)
                if (
                    start1 == start0
                    or max(start0 + work, lunch_start) + LUNCH_SEC > lunch_last
                ):
                    lunch, hungry = (at, at + LUNCH_SEC), False
                    departure = when = at + LUNCH_SEC
        arrival = when + leg[0]
        start = max(arrival, opens)
        end = start + work
        # Начатая работа кончается по факту (блок 68): закрытая — когда закрыли (не раньше
        # начала), затянувшаяся — не раньше момента, когда она ещё шла.
        fact = position.finished.get(request.id)
        if fact is not None:
            end = max(start, fact.end) if fact.exact else max(end, fact.end)
        visits.append(
            Visit(
                request.id,
                departure,
                arrival,
                start,
                end,
                travel_sec,
                travel_m,
                work,
                lunch,
            )
        )

        # Окно и смену у работы с фактом не проверяем: она уже случилась.
        if violation is None and fact is None:
            violation = _time_violation(
                request, engineer, position.duration_mode, start, end, shift_end
            )
        where, when, worked = point_id(request.id), end, True

    return visits, violation


def latest_start(request: Request, engineer: Engineer, mode: DurationMode) -> int:
    """Самое позднее допустимое начало работы этой бригадой (PLAN 6.2).

    Правило окна читается из конфига здесь — единственное место на весь проект. Отсюда же
    его берёт модель OR-Tools: верхняя граница `CumulVar` заявки — это оно же (PLAN 6.5).
    При `fit_in_window` до конца окна помещается работа, а её длительность зависит от
    бригады (PLAN 9.1): быстрая может начать позже медленной.
    """
    latest = to_seconds(request.window_end)
    if settings.window_rule == "fit_in_window":
        latest -= seconds(request, engineer, mode)
    return latest


def capped(
    request: Request, latest: int, engineers: list[Engineer], mode: DurationMode
) -> Request:
    """Копия заявки, которую нельзя начать позже `latest` (PLAN 6.5, аварии вперёд).

    Срок аварии на этапе 2 выражен самим окном, а не своей проверкой: его читают и модель
    OR-Tools, и досчёт, и `first_violation` — правило окна остаётся здесь одно. При
    `fit_in_window` к концу окна прибавляется длительность, чтобы `latest_start` копии было
    ровно `latest`. Окно только сужается; `latest` — целая минута, как и время окна.

    Длительность зависит от бригады (PLAN 9.1), а окно у копии одно. Оно берётся по самой
    быстрой из бригад, которым заявка подходит по ресурсу: у неё `latest_start` копии ровно
    `latest`, у остальных — не позже, и срок не нарушает никто. Правило окна при этом не
    повторяется: запас «конец окна − крайнее начало» даёт сам `latest_start`.
    """
    fitting = [
        engineer
        for engineer in engineers
        if resource_violation(request, engineer) is None
    ] or engineers
    allowed = max(latest_start(request, engineer, mode) for engineer in fitting)
    if latest >= allowed:
        return request
    end = latest + to_seconds(request.window_end) - allowed
    return request.model_copy(update={"window_end": to_clock(end)})


def _time_violation(
    request: Request,
    engineer: Engineer,
    mode: DurationMode,
    start: int,
    end: int,
    shift_end: int,
) -> Violation | None:
    """Окно и смена (PLAN 6.2, п. 3).

    Нижняя граница окна выполняется по построению: раньше начала окна работу не начинаем, ждём.
    """
    if start > latest_start(request, engineer, mode):
        return Violation.WINDOW_END
    if end > shift_end:
        return Violation.SHIFT_END
    return None
