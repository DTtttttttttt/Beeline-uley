"""Длительность работы — функция пары «заявка, инженер» (PLAN 9.1).

`ceil(работа × сложность × скорость + подъезд + документы)`: работа и документы — из норматива,
сложность — по объекту заявки, скорость — инженера. Всё целое: коэффициенты в тысячных
(`1200` = ×1,2), результат — целые минуты. В режиме `norm` сложность и скорость не применяются,
и длительность — `serviceDurationMin`, как до блока 66.

Единственное место формулы: расписание, модель OR-Tools, граница числа бригад, прогноз,
объяснения и ручная правка зовут отсюда. `validator.py` считает длительность своей арифметикой
и берёт отсюда только таблицу коэффициентов — данные, а не расписание.
"""

from dataclasses import dataclass

from ..dictionaries import (
    BUILDING_TYPE_NAMES,
    DURATION_FACTOR_NAMES,
    Access,
    BuildingType,
    DurationMode,
)
from ..models import Engineer, Request

UNIT = 1000  # коэффициент ×1,0 в тысячных
# Таблица — допущение команды (PLAN 9.1, `docs/data.md`). Добавки к работе в тысячных.
FLOOR_STEPS = ((10, 300), (6, 200), (3, 100))  # этаж без лифта, от → добавка
FLATS_STEPS = ((60, 100), (30, 50))  # квартир в подъезде, от → добавка (блок 78)
BUILDING = {
    BuildingType.APARTMENT: 0,
    BuildingType.PRIVATE: -100,
    BuildingType.OFFICE: 50,
}
GIGABIT = 100
REPEAT_VISIT = 150
CABLE = 200  # прокладка кабеля (блок 78)
DRILLING = 100  # сверление (блок 78)
DEVICE = 50  # за каждое устройство сверх первого
ACCESS_MIN = {
    Access.NORMAL: 0,
    Access.FAR_PARKING: 5,
    Access.CLOSED: 10,
}  # минуты, не %
CLAMP = (800, 2000)
# Уровень объекта по коэффициенту: простой — до ×1,0 включительно, средний — до ×1,4.
LEVELS = {"simple": 1000, "medium": 1400, "complex": CLAMP[1]}


def factors(request: Request) -> list[tuple[str, str, int]]:
    """Добавки к работе в тысячных — ключ пояснения, подпись для диспетчера и добавка, в
    порядке таблицы."""
    found = []
    item = request.object
    if item is not None:
        private = item.building_type is BuildingType.PRIVATE
        if not private and not item.elevator and item.floor is not None:
            step = next((add for low, add in FLOOR_STEPS if item.floor >= low), 0)
            if step:
                found.append(
                    ("floor", DURATION_FACTOR_NAMES["floor"].format(item.floor), step)
                )
        if not private and item.flats is not None:
            step = next((add for low, add in FLATS_STEPS if item.flats >= low), 0)
            if step:
                found.append(
                    ("flats", DURATION_FACTOR_NAMES["flats"].format(item.flats), step)
                )
        if BUILDING[item.building_type]:
            found.append(
                (
                    item.building_type.value,
                    BUILDING_TYPE_NAMES[item.building_type],
                    BUILDING[item.building_type],
                )
            )
    for key, flag, add in (
        ("gigabit", request.gigabit, GIGABIT),
        ("repeat_visit", request.repeat_visit, REPEAT_VISIT),
        ("cable", request.cable, CABLE),
        ("drilling", request.drilling, DRILLING),
    ):
        if flag:
            found.append((key, DURATION_FACTOR_NAMES[key], add))
    devices = len(request.required_equipment)
    if devices > 1:
        found.append(
            (
                "devices",
                DURATION_FACTOR_NAMES["devices"].format(devices),
                DEVICE * (devices - 1),
            )
        )
    return found


def complexity(request: Request) -> int:
    """Коэффициент сложности в тысячных: добавки складываются, итог зажат в ×0,8…×2,0."""
    total = UNIT + sum(add for _, _, add in factors(request))
    return min(max(total, CLAMP[0]), CLAMP[1])


def access_min(request: Request) -> int:
    return ACCESS_MIN[request.object.access] if request.object else 0


def level(request: Request) -> str:
    """Простой, средний или сложный объект — по коэффициенту, ключ `LEVELS`."""
    value = complexity(request)
    return next(name for name, upper in LEVELS.items() if value <= upper)


def at_speed(request: Request, speed: int) -> int:
    """Минуты работы с учётом сложности при скорости `speed` (тысячные)."""
    work = request.service_duration_min - request.documents_min
    scaled = -(-work * complexity(request) * speed // (UNIT * UNIT))  # ceil без float
    return scaled + access_min(request) + request.documents_min


def minutes(request: Request, engineer: Engineer, mode: DurationMode) -> int:
    """Длительность работы пары в минутах."""
    if mode == DurationMode.NORM:
        return request.service_duration_min
    return at_speed(request, engineer.speed)


def seconds(request: Request, engineer: Engineer, mode: DurationMode) -> int:
    return minutes(request, engineer, mode) * 60


@dataclass(frozen=True)
class Breakdown:
    """«Почему 101 минута»: из чего сложилась длительность (PLAN 9.1)."""

    norm_min: int  # работа на объекте по нормативу
    minutes: int
    work_min: int
    documents_min: int
    access_min: int
    complexity: int
    speed: int


def breakdown(request: Request, speed: int = UNIT) -> Breakdown:
    """Разбивка длительности для формы и карточки; без инженера скорость — норма. Причины
    словами — `explain.duration_reasons`, их минуты — `reason_minutes`."""
    return Breakdown(
        norm_min=request.service_duration_min,
        minutes=at_speed(request, speed),
        work_min=request.service_duration_min - request.documents_min,
        documents_min=request.documents_min,
        access_min=access_min(request),
        complexity=complexity(request),
        speed=speed,
    )


def reason_minutes(
    request: Request, speed: int = UNIT
) -> tuple[list[tuple[str, str, int]], int]:
    """Минуты каждой добавки и скорости (блок 78, «70 → 94 мин · +24 мин»): добавка — её доля
    от работы по нормативу, с округлением до минуты; скорость — разница с той же работой при
    норме. Сумма причин, подъезда и документов всегда сходится с длительностью. Зажим ×0,8…×2,0
    делится между добавками пропорционально, а округление достаётся самой большой: иначе
    упёршаяся в ×2,0 сложность ушла бы в строку скорости, и медленный инженер «ускорял» бы
    работу."""
    work = request.service_duration_min - request.documents_min
    listed = factors(request)
    total = sum(add for _, _, add in listed)
    # Сколько добавок осталось после зажима; без зажима — ровно их сумма.
    kept = complexity(request) - UNIT if total else 1
    total = total or 1
    found = [
        (key, name, (2 * work * add * kept + UNIT * total) // (2 * UNIT * total))
        for key, name, add in listed
    ]
    normal = at_speed(request, UNIT)
    extra = normal - request.service_duration_min - access_min(request)
    extra -= sum(minutes for _, _, minutes in found)
    if found and extra:
        top = max(range(len(found)), key=lambda index: abs(found[index][2]))
        key, name, minutes = found[top]
        found[top] = (key, name, minutes + extra)
    rest = at_speed(request, speed) - normal
    return found, rest
