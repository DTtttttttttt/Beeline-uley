"""Расписание маршрута, проверка ограничений и лучшая вставка (PLAN 6.2).

Единственное место, где проверяются ограничения. Базовый вариант, отбор кандидатов, досчёт,
ручное переназначение и оценка риска зовут отсюда, а не пишут свою проверку: разъехавшиеся
копии — самая дорогая ошибка в этом проекте.

Внутри — целые секунды от полуночи и целые метры; перевод наружу делают мапперы.
"""

from dataclasses import dataclass, field
from enum import StrEnum

from ..config import settings
from ..models import Engineer, Input, Office, Request
from ..routing.matrices import Travel, point_id, start_id
from ..timeutil import to_seconds

# Короче этого свободный промежуток диспетчеру не показываем (PLAN 6.9).
MIN_IDLE_SEC = 10 * 60


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


def day_start(office: Office, engineer: Engineer) -> Position:
    """Где и когда у бригады **начался день** (PLAN 6.2).

    Стартовая точка — офис или её дом; время — начало смены, а у вышедшей в течение дня
    бригады время выхода. По этой позиции пересчитывается уже сделанное: закреплённый
    префикс маршрута и независимая проверка.
    """
    return Position(
        start_id(office, engineer),
        None
        if engineer.available_from is None
        else to_seconds(engineer.available_from),
        equipment_used=0,  # день только начался: рюкзак собран, ничего не отдано
    )


def free_from(office: Office, engineer: Engineer) -> Position:
    """Откуда и когда бригада берёт **новую** работу.

    Отличается от `day_start` только у выбывшей бригады: ей новых заявок не достаётся,
    и выражено это концом смены — то же самое одинаково видят проверка маршрута и
    модель OR-Tools (PLAN 6.12).
    """
    if engineer.unavailable_from is not None:
        return Position(
            start_id(office, engineer), to_seconds(engineer.shift_end), equipment_used=0
        )
    return day_start(office, engineer)


def start_positions(data: Input) -> dict[str, Position]:
    """Состояние бригад на начало дня: каждая — от своей стартовой точки (PLAN 6.2).

    Словарь заполняется на все бригады входа, поэтому дальше по коду позиция берётся по
    ключу, а не «по умолчанию, если не нашлось».
    """
    return {
        engineer.id: free_from(data.office, engineer) for engineer in data.engineers
    }


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
    when = to_seconds(engineer.shift_start)
    if position.available_from is not None:
        when = max(when, position.available_from)

    slots = []
    for visit in visits:
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
    два запуска на одних данных обязаны дать один результат.
    """
    if resource_violation(new_request, engineer) is not None:
        return None

    base = meters(build_schedule(engineer, requests, travel, position))
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
    shift_start = to_seconds(engineer.shift_start)
    shift_end = to_seconds(engineer.shift_end)
    where = position.point_id
    when = shift_start
    if position.available_from is not None:
        when = max(shift_start, position.available_from)

    # Оборудование берётся в офисе на весь день, пополнения нет (ответ 4): счёт идёт от того,
    # что уже унесла закреплённая часть.
    used = position.equipment_used

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

        leg = travel.travel(engineer.transport, where, point_id(request.id))
        if leg is None:
            # Пути нет: следующий выезд не от чего считать, дальше маршрута не существует.
            return visits, violation or Violation.NO_ROUTE
        travel_sec, travel_m = leg

        departure = when
        arrival = departure + travel_sec
        start = max(arrival, to_seconds(request.window_start))
        end = start + request.service_duration_min * 60
        visits.append(
            Visit(request.id, departure, arrival, start, end, travel_sec, travel_m)
        )

        if violation is None:
            violation = _time_violation(request, start, end, shift_end)
        where, when = point_id(request.id), end

    return visits, violation


def latest_start(request: Request) -> int:
    """Самое позднее допустимое начало работы (PLAN 6.2).

    Правило окна читается из конфига здесь — единственное место на весь проект. Отсюда же
    его берёт модель OR-Tools: верхняя граница `CumulVar` заявки — это оно же (PLAN 6.5).
    """
    latest = to_seconds(request.window_end)
    if settings.window_rule == "fit_in_window":
        latest -= request.service_duration_min * 60
    return latest


def _time_violation(
    request: Request, start: int, end: int, shift_end: int
) -> Violation | None:
    """Окно и смена (PLAN 6.2, п. 3).

    Нижняя граница окна выполняется по построению: раньше начала окна работу не начинаем, ждём.
    """
    if start > latest_start(request):
        return Violation.WINDOW_END
    if end > shift_end:
        return Violation.SHIFT_END
    return None
