"""Факты дня на сервере: скрытая длительность работ и часы дня (PLAN 9.1, блок 68).

Фактическая длительность — чистая функция, а не хранилище: истинная скорость инженера
(оценка `Engineer.speed` ± 5 %, своим потоком от его id) × треугольный разброс работы из модели
прогноза (`FORECAST_WORK`). Значение нигде не лежит и в ответы не попадает — узнать его можно
только по факту закрытия. Детерминизм — `FORECAST_SEED`: два прогона дня дают одни факты.
"""

import random

from ..config import settings
from ..models import ClockClosure, ClockEvent, Engineer, Plan, Request
from ..routing.matrices import Travel, point_id
from ..timeutil import to_clock, to_seconds
from . import duration
from .forecast import _factor
from .schedule import Position, first_violation, units

# Истинная скорость — оценка ± 5 % (решение заказчика): оценка почти верна, и за день видно,
# как скорость в профиле подтягивается к правде.
SPEED_SPREAD = 0.05


def true_speed(engineer: Engineer) -> int:
    """Истинная скорость инженера в тысячных — своим потоком от его id."""
    rnd = random.Random(f"{settings.forecast_seed}:speed:{engineer.id}")
    return round(engineer.speed * (1 + rnd.uniform(-SPEED_SPREAD, SPEED_SPREAD)))


def actual(request: Request, engineer: Engineer) -> int:
    """Сколько секунд работа займёт на самом деле: сложность объекта, истинная скорость и
    разброс работы. От режима плана не зависит — реальность одна."""
    rnd = random.Random(f"{settings.forecast_seed}:work:{request.id}:{engineer.id}")
    spread = round(_factor(rnd, settings.forecast_work_factor) * duration.UNIT)
    work = request.service_duration_min - request.documents_min
    product = work * duration.complexity(request) * true_speed(engineer) * spread
    minutes = -(-product // duration.UNIT**3)  # ceil без float
    return (minutes + duration.access_min(request) + request.documents_min) * 60


def observe(parent: Plan, travel: Travel, moment: int) -> ClockEvent | None:
    """Что часы дня узнали к `moment` (блок 68); `None` — ничего нового, версии не будет.

    У каждого инженера смотрится одна работа — первая незакрытая, если по плану она уже
    началась. Её скрытая длительность истекла — закрытие с фактическим концом. Не истекла, а
    известный конец (план или прошлое «ещё идёт») уже позади — работа затянулась: если из-за
    этого остаток дня инженера от `(её адрес, минута)` стал недопустимым, она попадает в
    `running`, и пересчёт починит остаток. Без конфликта задержка версии не даёт.

    Событие встаёт **в самый ранний момент** до `moment`, когда что-то случилось, — факт или
    первая минута конфликта, — но не раньше события родителя (блок 69). Часы, отставшие на
    часы (страницу открыли вечером), иначе закрыли бы работу задним числом «в 23:03»: следующие
    заявки к этому моменту по плану уже выехали, закреплены и пересчитались бы от факта за окно
    и смену. Так же день идёт по порядку: следующий вызов часов смотрит уже новую версию.
    """
    floor = to_seconds(parent.event.time) if parent.event else 0
    engineers = {engineer.id: engineer for engineer in parent.input.engineers}
    known = {request.id: request for request in parent.input.requests}
    # Инженер → (ранний момент, закрытие или затянувшаяся работа).
    found: dict[str, tuple[int, ClockClosure | str]] = {}
    for route in parent.routes:
        engineer = engineers.get(route.engineer_id)
        index = next(
            (
                n
                for n, stop in enumerate(route.stops)
                if stop.request_id not in parent.closed
            ),
            None,
        )
        if engineer is None or index is None:
            continue
        stop = route.stops[index]
        start = to_seconds(stop.start)
        if start > moment:
            continue
        request = known[stop.request_id]
        end = start + actual(request, engineer)
        # Затянулась: первая целая минута после известного конца, когда остаток дня инженера
        # стал недопустимым, — до фактического конца и не позже `moment`.
        lasting = to_seconds(parent.running.get(request.id, stop.end))
        known_end = max(to_seconds(stop.end), lasting)
        done = [known[item.request_id] for item in route.stops[: index + 1]]
        rest = [known[item.request_id] for item in route.stops[index + 1 :]]
        first = -(-max(known_end + 1, floor) // 60) * 60
        for minute in range(first, min(end - 1, moment) + 1, 60):
            at = Position(
                point_id(request.id),
                minute,
                equipment_used=units(done),
                duration_mode=parent.input.duration_mode,
                finished={},
                turns={},
                # Обед был, если по плану он стоит раньше этой работы (блок 79).
                lunch_done=route.lunch is not None
                and to_seconds(route.lunch.start) < start,
                after_work=True,  # стоит у начатой работы
            )
            if first_violation(engineer, rest, travel, at) is not None:
                found[engineer.id] = (minute, request.id)
                break
        else:
            if end <= moment:
                closure = ClockClosure(request_id=request.id, actual_end=to_clock(end))
                found[engineer.id] = (max(end, floor), closure)
    if not found:
        return None
    earliest = min(when for when, _ in found.values())
    news = [what for when, what in found.values() if when == earliest]
    return ClockEvent(
        type="clock",
        time=to_clock(earliest),
        closed=[what for what in news if isinstance(what, ClockClosure)],
        running=[what for what in news if isinstance(what, str)],
    )
