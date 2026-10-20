"""Метрики плана (PLAN 6.10). Ожидаемые опоздания приходят готовыми из прогноза (PLAN 6.18).

Всё считается от сумм целых секунд и метров, а не от сумм округлённых значений стопов:
иначе независимая проверка (PLAN 6.7) расходилась бы с планом на округлении, а не по делу.
"""

from ..dictionaries import TOP_RATING, Outcome, WorkPriority
from ..models import Closure, Input, Metrics, Request
from ..timeutil import to_km, to_minutes, to_seconds
from . import duration
from .schedule import Visit


def compute(
    data: Input,
    visits: dict[str, list[Visit]],
    unassigned: list[Request],
    deferred: int = 0,
    expected_late: float = 0,
    closed: dict[str, Closure] | None = None,
) -> Metrics:
    """Метрики по маршрутам во внутренних единицах.

    `visits` — стопы каждой бригады набора; у простаивающей список пуст, но в метриках она
    есть: `kmByEngineer` и `utilizationByEngineer` описывают все бригады плана (PLAN 5.2).
    `unassigned` включает перенесённые заявки: в плане они тоже без исполнителя, а `deferred`
    говорит, сколько из них отложил диспетчер (PLAN 6.19).

    `expected_late` — сумма вероятностей опоздания из прогноза (PLAN 6.18). Здесь она только
    округляется: своей модели отклонений у метрик нет, а у плана без прогноза она нулевая.

    `closed` — факты от диспетчера (PLAN 6.19): считаются по исходам, а не по маршрутам,
    потому что невыполненная заявка из маршрута уже ушла.

    Ожидание аварий — по всему дню, вместе с закреплённой частью: это время, которое авария
    ждала, а не только то, что ещё можно сократить.
    """
    requests = {request.id: request for request in data.requests}
    km_by_engineer: dict[str, float] = {}
    utilization: dict[str, float] = {}
    by_priority = dict.fromkeys(WorkPriority, 0)
    total_m = travel_sec = wait_sec = work_sec = assigned = used = urgent = 0

    for engineer in data.engineers:
        stops = visits.get(engineer.id, [])
        route_m = sum(visit.travel_m for visit in stops)
        route_travel = sum(visit.travel_sec for visit in stops)
        route_work = sum(visit.end - visit.start for visit in stops)
        shift = to_seconds(engineer.shift_end) - to_seconds(engineer.shift_start)

        km_by_engineer[engineer.id] = to_km(route_m)
        # Загрузка смены — переезды и работы; ожидание в неё не входит (PLAN 6.10).
        utilization[engineer.id] = round((route_travel + route_work) / shift, 2)
        total_m += route_m
        travel_sec += route_travel
        work_sec += route_work
        wait_sec += sum(visit.start - visit.arrival for visit in stops)
        assigned += len(stops)
        used += 1 if stops else 0
        for visit in stops:
            request = requests[visit.request_id]
            by_priority[request.work_priority] += 1
            urgent += request.urgent

    return Metrics(
        engineers_used=used,
        total_km=to_km(total_m),
        km_by_engineer=km_by_engineer,
        assigned_count=assigned,
        unassigned_count=len(unassigned),
        assigned_by_work_priority=by_priority,
        unassigned_by_work_priority=_by_priority(unassigned),
        assigned_urgent=urgent,
        unassigned_urgent=sum(request.urgent for request in unassigned),
        travel_min=to_minutes(travel_sec),
        wait_min=to_minutes(wait_sec),
        work_min=to_minutes(work_sec),
        utilization_by_engineer=utilization,
        deferred_count=deferred,
        closed_by_outcome=_by_outcome(closed or {}),
        expected_late=round(expected_late, 2),
        emergency_wait_min=to_minutes(emergency_wait(data, visits, day_opens(data))),
        # Сложность и скорость (PLAN 9.1): сколько добавили к нормативу назначенные заявки.
        complexity_extra_min=to_minutes(
            work_sec
            - sum(
                requests[visit.request_id].service_duration_min * 60
                for stops in visits.values()
                for visit in stops
            )
        ),
        objects_by_level=objects_by_level(data.requests),
        first_leg_min=_first_leg(visits),
        **_ratings(data, visits),
    )


def _first_leg(visits: dict[str, list[Visit]]) -> int | None:
    """Дорога до первой заявки (блок 73): в среднем по инженерам с работой, от их старта —
    офиса или дома. Среднее — в целых секундах, потом минуты: сверке не о чем спорить."""
    firsts = [stops[0].travel_sec for stops in visits.values() if stops]
    return to_minutes(round(sum(firsts) / len(firsts))) if firsts else None


def _ratings(data: Input, visits: dict[str, list[Visit]]) -> dict:
    """Рейтинг исполнителей назначенных заявок (блок 71): средний и сколько у 4,5+.

    Снимок плана как есть, без сглаживания цели: его диспетчер видит у инженера. Считается в
    целых сотых звезды — сверке валидатора не о чем спорить в округлении.
    """
    stars = [
        round(engineer.rating.stars * 100)
        for engineer in data.engineers
        if engineer.rating
        for _ in visits.get(engineer.id, [])
    ]
    return {
        "rating_avg": round(sum(stars) / len(stars)) / 100 if stars else None,
        "top_rated_count": sum(value >= round(TOP_RATING * 100) for value in stars),
    }


def objects_by_level(requests: list[Request]) -> dict[str, int]:
    """Сколько заявок простых, средних и сложных; все уровни, отсутствующие нулями."""
    counts = dict.fromkeys(duration.LEVELS, 0)
    for request in requests:
        counts[duration.level(request)] += 1
    return counts


def emergency_wait(data: Input, visits: dict[str, list[Visit]], opened: int) -> int:
    """Сколько секунд ждали назначенные аварии, в сумме (PLAN 6.10, блок 49).

    Тем же счётом аварии вперёд сравнивают план с базовым (PLAN 6.6), а мягкая граница
    этапа 1 отсчитывается от того же `ready`: метрика и решение не могут разойтись.
    """
    requests = {request.id: request for request in data.requests}
    return sum(
        visit.start - ready(requests[visit.request_id], opened)
        for stops in visits.values()
        for visit in stops
        if requests[visit.request_id].work_priority is WorkPriority.EMERGENCY
    )


def ready(request: Request, opened: int) -> int:
    """С какого момента авария ждёт: начало окна, но не раньше начала рабочего дня."""
    return max(to_seconds(request.window_start), opened)


def day_opens(data: Input) -> int:
    """Начало рабочего дня — самое раннее начало смены бригад (блок 49).

    От него, а не от начала окна, ждёт авария с окном на весь день (00:01–23:59): до первой
    смены её не начать никому, и «ждала с полуночи» было бы неправдой. У аварии из события
    начало окна уже прижато к моменту поступления (PLAN 6.12).
    """
    return min(
        (to_seconds(engineer.shift_start) for engineer in data.engineers), default=0
    )


def _by_priority(requests: list[Request]) -> dict[WorkPriority, int]:
    """Все три типа работ, отсутствующие нулями — как их сверяет валидатор (PLAN 6.7)."""
    counts = dict.fromkeys(WorkPriority, 0)
    for request in requests:
        counts[request.work_priority] += 1
    return counts


def _by_outcome(closed: dict[str, Closure]) -> dict[Outcome, int]:
    """Все три исхода, отсутствующие нулями — как их сверяет валидатор (PLAN 6.7)."""
    counts = dict.fromkeys(Outcome, 0)
    for closure in closed.values():
        counts[closure.outcome] += 1
    return counts
