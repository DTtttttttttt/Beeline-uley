"""Независимая проверка готового плана (PLAN 6.7).

Пересчитывает план с нуля **своим** кодом и специально не использует `schedule.py`: если
проверять результат тем же кодом, который его построил, проверка повторит его ошибку.
Общими остаются только модели, перевод единиц и настройка `WINDOW_RULE`.

Результат кладётся в `plan.validation`; при `ok: false` API отвечает 500.
"""

from ..config import settings
from ..dictionaries import Outcome, WorkPriority
from ..models import (
    Engineer,
    Office,
    Plan,
    ReplanMode,
    Request,
    Route,
    Stop,
    Validation,
)
from ..routing.matrices import Travel, point_id, start_id
from ..timeutil import to_clock_over_midnight, to_km, to_minutes, to_seconds


def validate(plan: Plan, travel: Travel) -> Validation:
    requests = {request.id: request for request in plan.input.requests}
    engineers = {engineer.id: engineer for engineer in plan.input.engineers}
    errors = _composition(plan, requests, engineers)
    # Момент события берётся из самого плана: в режимах from_event и insert бригада не
    # выезжает к незакреплённой заявке раньше T, даже если по расписанию освободилась до
    # него (PLAN 6.12). Не действует он только в full — там день считается заново.
    event_time = (
        to_seconds(plan.event.time)
        if plan.event and plan.replan_mode is not ReplanMode.FULL
        else None
    )

    km_by_engineer: dict[str, float] = {}
    total_m = travel_sec = wait_sec = work_sec = used = 0
    by_priority = dict.fromkeys(WorkPriority, 0)

    for route in plan.routes:
        engineer = engineers.get(route.engineer_id)
        if engineer is None:  # уже названо в _composition
            continue
        route_errors, counters = _route(
            route,
            engineer,
            plan.input.office,
            requests,
            travel,
            event_time,
            plan.closed,
        )
        errors += route_errors
        route_m, route_travel, route_wait, route_work, priorities = counters

        km_by_engineer[route.engineer_id] = to_km(route_m)
        if route.km != to_km(route_m):
            errors.append(
                f"{route.engineer_id}: пробег {route.km} км, пересчёт даёт {to_km(route_m)} км"
            )
        total_m += route_m
        travel_sec += route_travel
        wait_sec += route_wait
        work_sec += route_work
        used += 1 if route.stops else 0
        for priority, count in priorities.items():
            by_priority[priority] += count

    # Метрики считаются от суммы секунд и метров, а не от суммы округлённых значений.
    # utilizationByEngineer и expectedLate не сверяем: их считают блоки 7 и 21.
    not_planned = [
        requests[item.request_id]
        for item in plan.unassigned
        if item.request_id in requests
    ]
    unassigned_priority = dict.fromkeys(WorkPriority, 0)
    for request in not_planned:
        unassigned_priority[request.work_priority] += 1
    assigned_urgent = sum(
        requests[stop.request_id].urgent
        for route in plan.routes
        for stop in route.stops
        if stop.request_id in requests
    )
    # Перенесённая заявка не делается сегодня: ни в одном маршруте её быть не может,
    # но из дня она не исчезает — остаётся во входе и среди неназначенных (PLAN 6.19).
    planned = {stop.request_id for route in plan.routes for stop in route.stops}
    left = {item.request_id for item in plan.unassigned}
    errors += [
        f"заявка №{request_id} перенесена на следующий день, но стоит в маршруте"
        for request_id in plan.deferred
        if request_id in planned
    ]
    errors += [
        f"заявка №{request_id} перенесена на следующий день, но её нет {where}"
        for request_id in plan.deferred
        for where, missing in (
            ("во входе плана", request_id not in requests),
            ("среди неназначенных", request_id not in left),
        )
        if missing
    ]
    errors += _closures(plan, requests)
    # Ручное закрепление обязано быть выполнено: план несёт только те закрепления, которые
    # в нём же и сработали (PLAN 6.16, п. 6). Расхождение означало бы, что диспетчеру
    # показывают «закреплена за Новиковым», а работает по ней кто-то другой.
    errors += [
        f"заявка №{request_id} закреплена за {engineer_id}, "
        f"а назначена {plan.assignments.get(request_id)}"
        for request_id, engineer_id in plan.pinned.items()
        if plan.assignments.get(request_id) != engineer_id
    ]
    metrics = plan.metrics
    errors += [
        f"метрика {name}: в плане {shown}, пересчёт даёт {computed}"
        for name, shown, computed in (
            ("engineersUsed", metrics.engineers_used, used),
            ("totalKm", metrics.total_km, to_km(total_m)),
            ("kmByEngineer", metrics.km_by_engineer, km_by_engineer),
            (
                "assignedCount",
                metrics.assigned_count,
                sum(len(route.stops) for route in plan.routes),
            ),
            ("unassignedCount", metrics.unassigned_count, len(plan.unassigned)),
            (
                "assignedByWorkPriority",
                metrics.assigned_by_work_priority,
                by_priority,
            ),
            (
                "unassignedByWorkPriority",
                metrics.unassigned_by_work_priority,
                unassigned_priority,
            ),
            ("assignedUrgent", metrics.assigned_urgent, assigned_urgent),
            (
                "unassignedUrgent",
                metrics.unassigned_urgent,
                sum(request.urgent for request in not_planned),
            ),
            ("deferredCount", metrics.deferred_count, len(plan.deferred)),
            (
                "closedByOutcome",
                metrics.closed_by_outcome,
                _by_outcome(plan),
            ),
            ("travelMin", metrics.travel_min, to_minutes(travel_sec)),
            ("waitMin", metrics.wait_min, to_minutes(wait_sec)),
            ("workMin", metrics.work_min, to_minutes(work_sec)),
        )
        if shown != computed
    ]
    return Validation(ok=not errors, errors=errors)


def _by_outcome(plan: Plan) -> dict[Outcome, int]:
    """Исходы по фактам плана: все три ключа, отсутствующие нулями (PLAN 6.10)."""
    counts = dict.fromkeys(Outcome, 0)
    for closure in plan.closed.values():
        counts[closure.outcome] += 1
    return counts


def _closures(plan: Plan, requests: dict[str, Request]) -> list[str]:
    """Факт от диспетчера и план обязаны совпадать (PLAN 6.19, ответ 1).

    Закрытая заявка остаётся за той бригадой, у которой её закрыли, и её стоп закреплён —
    иначе диспетчеру показали бы «выполнена Новиковым», а в маршруте по ней ехал бы
    кто-то другой. Невыполненная, наоборот, обязана уйти из маршрутов в перенос: бригада
    её не сделала, и день за неё не отчитывается.
    """
    errors: list[str] = []
    placed = {
        stop.request_id: (route.engineer_id, stop.committed)
        for route in plan.routes
        for stop in route.stops
    }
    for request_id, closure in plan.closed.items():
        if request_id not in requests:
            errors.append(
                f"заявки №{request_id} нет во входе плана, но она закрыта фактом"
            )
        if closure.outcome is Outcome.FAILED:
            if request_id in placed:
                errors.append(
                    f"заявка №{request_id} закрыта как невыполненная, "
                    f"но стоит в маршруте {placed[request_id][0]}"
                )
            if request_id not in plan.deferred:
                errors.append(
                    f"заявка №{request_id} закрыта как невыполненная, "
                    "но не перенесена на следующий день"
                )
            continue
        if request_id not in placed:
            errors.append(
                f"заявка №{request_id} закрыта у {closure.engineer_id}, "
                "но её нет ни в одном маршруте"
            )
            continue
        engineer_id, committed = placed[request_id]
        if engineer_id != closure.engineer_id:
            errors.append(
                f"заявка №{request_id} закрыта у {closure.engineer_id}, "
                f"а стоит в маршруте {engineer_id}"
            )
        if not committed:
            errors.append(
                f"заявка №{request_id} закрыта фактом, но её стоп не закреплён"
            )
    return errors


def _composition(
    plan: Plan, requests: dict[str, Request], engineers: dict[str, Engineer]
) -> list[str]:
    """Каждая заявка ровно в одном месте, чужих идентификаторов нет (PLAN 6.7)."""
    errors: list[str] = []
    seen: dict[str, str | None] = {}  # заявка -> бригада, None — числится неназначенной
    for route in plan.routes:
        if route.engineer_id not in engineers:
            errors.append(f"маршрут неизвестной бригады {route.engineer_id}")
        for stop in route.stops:
            if stop.request_id not in requests:
                errors.append(
                    f"в маршруте {route.engineer_id} заявка {stop.request_id} не из набора"
                )
            if stop.request_id in seen:
                errors.append(
                    f"заявка {stop.request_id} встречается дважды: {seen[stop.request_id]} и {route.engineer_id}"
                )
            seen[stop.request_id] = route.engineer_id

    routes_by_engineer = [route.engineer_id for route in plan.routes]
    errors += [
        f"у бригады {engineer_id} больше одного маршрута"
        for engineer_id in dict.fromkeys(routes_by_engineer)
        if routes_by_engineer.count(engineer_id) > 1
    ]

    for item in plan.unassigned:
        if item.request_id not in requests:
            errors.append(f"неназначенная заявка {item.request_id} не из набора")
        if item.request_id in seen:
            engineer_id = seen[item.request_id]
            errors.append(
                f"заявка {item.request_id} числится неназначенной дважды"
                if engineer_id is None
                else f"заявка {item.request_id} и назначена бригаде {engineer_id}, "
                "и числится неназначенной"
            )
        seen.setdefault(item.request_id, None)

    errors += [
        f"заявки {request_id} нет ни в маршрутах, ни в неназначенных"
        for request_id in requests
        if request_id not in seen
    ]

    expected = {
        request_id: seen.get(request_id) if seen.get(request_id) in engineers else None
        for request_id in requests
    }
    if plan.assignments != expected:
        errors.append("assignments не совпадает с маршрутами и неназначенными заявками")
    return errors


def _route(
    route: Route,
    engineer: Engineer,
    office: Office,
    requests: dict[str, Request],
    travel: Travel,
    event_time: int | None = None,
    closed: dict | None = None,
) -> tuple[list[str], tuple[int, int, int, int, dict[WorkPriority, int]]]:
    """Пересчёт одного маршрута: ограничения, времена и слагаемые метрик."""
    errors: list[str] = []
    by_priority = dict.fromkeys(WorkPriority, 0)
    route_m = route_travel = route_wait = route_work = 0

    shift_end = to_seconds(engineer.shift_end)
    where = start_id(office, engineer)
    # День пересчитывается от стартовой точки бригады и начала её дня: закреплённые стопы
    # воспроизводятся один в один. Начало дня — начало смены, а у вышедшей в течение дня
    # бригады время выхода (PLAN 6.19). Дальше начинается остаток дня, и к нему бригада не
    # выезжает раньше события (PLAN 6.12, шаг 2) — бригада, простаивавшая к моменту T,
    # иначе получила бы выезд раньше, чем событие вообще случилось.
    when = to_seconds(engineer.shift_start)
    if engineer.available_from is not None:
        when = max(when, to_seconds(engineer.available_from))
    # Выбывшая бригада новых работ не берёт: после этого времени выезжать ей некуда.
    left = (
        None
        if engineer.unavailable_from is None
        else to_seconds(engineer.unavailable_from)
    )
    waited = event_time is None
    free_seen = False
    # Оборудование на весь день, пополнения нет (PLAN 6.2): счёт по всему маршруту, вместе
    # с закреплённой частью — свой, а не `schedule.capacity`.
    carried = 0
    limit = settings.equipment_capacity.get(engineer.transport)

    for stop in route.stops:
        request = requests.get(stop.request_id)
        if request is None:  # уже названо в _composition
            continue
        carried += len(request.required_equipment)
        # Ругаемся на новую работу, которая сама берёт оборудование сверх вместимости.
        # Закреплённую — нет: она уже начата, и план, посчитанный при другой настройке,
        # после смены вместимости иначе не принял бы ни одного события.
        if (
            not stop.committed
            and request.required_equipment
            and limit is not None
            and carried > limit
        ):
            errors.append(
                f"{engineer.id}: с заявкой {request.id} маршрут расходует {carried} ед. "
                f"оборудования, на транспорте {engineer.transport} берётся не больше {limit}"
            )
        if stop.committed:
            errors += _commitment(
                stop, engineer, event_time, free_seen, stop.request_id in (closed or {})
            )
        else:
            free_seen = True
            if not waited:
                when, waited = max(when, event_time), True
            if left is not None and when >= left:
                errors.append(
                    f"{engineer.id}: бригада недоступна с {engineer.unavailable_from}, "
                    f"а к заявке {stop.request_id} выезжает в {to_clock_over_midnight(when)}"
                )
        errors += _resources(request, engineer)

        leg = travel.travel(engineer.transport, where, point_id(request.id))
        if leg is None:
            errors.append(
                f"{engineer.id}: до заявки {request.id} нет пути на этом транспорте"
            )
            break
        seconds, meters = leg

        departure = when
        arrival = departure + seconds
        start = max(arrival, to_seconds(request.window_start))
        end = start + request.service_duration_min * 60

        errors += _same(stop, departure, arrival, start, end, seconds, meters, engineer)
        errors += _time(request, engineer, start, end, shift_end)

        route_m += meters
        route_travel += seconds
        route_wait += start - arrival
        route_work += end - start
        by_priority[request.work_priority] += 1
        where, when = point_id(request.id), end

    return errors, (route_m, route_travel, route_wait, route_work, by_priority)


def _commitment(
    stop: Stop,
    engineer: Engineer,
    event_time: int | None,
    free_seen: bool,
    closed: bool,
) -> list[str]:
    """Закреплённый стоп: только в начале маршрута и только по одной из двух причин —
    выезд к нему был не позже события (PLAN 6.12, шаг 1) или диспетчер закрыл его фактом
    (PLAN 6.19). Второе не зависит ни от режима пересчёта, ни от времени выезда: факт
    записан, и пересматривать его нечем.

    Проверять флаг обязательно: это он выключает правило «не выезжать раньше `T`», и без
    проверки подделка отключала бы проверку сама на себя. Выезд сверяется по напечатанному
    времени — тому самому, по которому идёт фиксация; само это время уже сверено с пересчётом.
    """
    errors = []
    if free_seen:
        errors.append(
            f"{engineer.id}: закреплённая заявка {stop.request_id} стоит после незакреплённой"
        )
    if closed:
        return errors
    if event_time is None:
        errors.append(
            f"{engineer.id}: заявка {stop.request_id} закреплена, хотя план считался "
            "не с момента события и фактом она не закрыта"
        )
    elif to_seconds(stop.departure) > event_time:
        errors.append(
            f"{engineer.id}: заявка {stop.request_id} закреплена, но выезд {stop.departure} "
            f"позже события {to_clock_over_midnight(event_time)}"
        )
    return errors


def _resources(request: Request, engineer: Engineer) -> list[str]:
    """Квалификация и ресурс (PLAN 6.2, пп. 1–2) — свой разбор, не из schedule.py."""
    errors = []
    if request.skill not in engineer.skills:
        errors.append(f"{engineer.id}: нет навыка для заявки {request.id}")
    if request.required_transport and request.required_transport != engineer.transport:
        errors.append(f"{engineer.id}: не тот транспорт для заявки {request.id}")
    missing = set(request.required_equipment) - set(engineer.equipment)
    if missing:
        errors.append(
            f"{engineer.id}: нет оборудования для заявки {request.id}: {', '.join(sorted(missing))}"
        )
    missing = set(request.required_tools) - set(engineer.tools)
    if missing:
        errors.append(
            f"{engineer.id}: нет инструмента для заявки {request.id}: {', '.join(sorted(missing))}"
        )
    return errors


def _time(
    request: Request, engineer: Engineer, start: int, end: int, shift_end: int
) -> list[str]:
    """Окно заявки по текущему WINDOW_RULE и конец смены (PLAN 6.2, п. 3)."""
    errors = []
    window_start, window_end = (
        to_seconds(request.window_start),
        to_seconds(request.window_end),
    )
    latest = window_end
    if settings.window_rule == "fit_in_window":
        latest -= request.service_duration_min * 60
    if not window_start <= start <= latest:
        errors.append(
            f"{engineer.id}: заявка {request.id} начинается в {to_clock_over_midnight(start)}, "
            f"окно {request.window_start}–{request.window_end} ({settings.window_rule})"
        )
    if end > shift_end:
        errors.append(
            f"{engineer.id}: заявка {request.id} заканчивается в {to_clock_over_midnight(end)}, "
            f"смена до {engineer.shift_end}"
        )
    return errors


def _same(
    stop: Stop,
    departure: int,
    arrival: int,
    start: int,
    end: int,
    seconds: int,
    meters: int,
    engineer: Engineer,
) -> list[str]:
    """Сверка того, что записано в плане, с пересчётом: подделанное время обязано всплыть."""
    return [
        f"{engineer.id}, заявка {stop.request_id}: {name} {shown}, пересчёт даёт {computed}"
        for name, shown, computed in (
            ("выезд", stop.departure, to_clock_over_midnight(departure)),
            ("прибытие", stop.arrival, to_clock_over_midnight(arrival)),
            ("начало", stop.start, to_clock_over_midnight(start)),
            ("окончание", stop.end, to_clock_over_midnight(end)),
            ("переезд, мин", stop.travel_min, to_minutes(seconds)),
            ("переезд, км", stop.travel_km, to_km(meters)),
        )
        if shown != computed
    ]
