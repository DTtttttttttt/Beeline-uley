"""Независимая проверка готового плана (PLAN 6.7).

Пересчитывает план с нуля **своим** кодом и специально не использует `schedule.py`: если
проверять результат тем же кодом, который его построил, проверка повторит его ошибку.
Общими остаются только модели, перевод единиц и настройка `WINDOW_RULE`.

Результат кладётся в `plan.validation`; при `ok: false` API отвечает 500.
"""

from ..config import settings
from ..dictionaries import (
    LUNCH_MIN,
    TOP_RATING,
    BuildingType,
    DurationMode,
    Outcome,
    WorkPriority,
)
from ..models import (
    Algorithm,
    Dispatch,
    Engineer,
    Input,
    Plan,
    ReplanMode,
    Request,
    Route,
    Stop,
    Validation,
)
from ..routing.matrices import Travel, haversine, point_id, start_id, turn_id
from ..timeutil import (
    to_clock_over_midnight,
    to_km,
    to_meters,
    to_minutes,
    to_seconds,
)
from . import duration as table  # только таблица коэффициентов, не формула

# Насколько точка разворота может выйти за «эллипс» плеча (блок 74): Valhalla снимает концы
# линии на дорогу, и они лежат в десятках метров от точек заявок (PLAN 6.11).
TURN_SLACK_M = 200


def validate(
    plan: Plan,
    travel: Travel,
    pinned: dict[str, str] | None = None,
    staged: bool = False,
) -> Validation:
    """`pinned` и `staged` — условия досчёта, которых в самом плане нет (блок 52).

    `pinned` — закрепления, действовавшие в расчёте: не вставшая к своей бригаде заявка из
    `plan.pinned` выпадает. `staged` — досчёт шёл на ужатых сроках аварий (PLAN 6.5), а в
    `plan.input` лежат исходные окна.
    """
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
    total_m = travel_sec = wait_sec = work_sec = used = emergency_sec = 0
    # Начало рабочего дня — своим кодом, а не `metrics.day_opens` (PLAN 6.10, блок 49).
    opened = min(
        (to_seconds(engineer.shift_start) for engineer in plan.input.engineers),
        default=0,
    )
    by_priority = dict.fromkeys(WorkPriority, 0)

    for route in plan.routes:
        engineer = engineers.get(route.engineer_id)
        if engineer is None:  # уже названо в _composition
            continue
        route_errors, counters = _route(
            route,
            engineer,
            plan.input,
            requests,
            travel,
            event_time,
            plan.closed,
            opened,
            running=plan.running,
        )
        errors += route_errors
        route_m, route_travel, route_wait, route_work, priorities, route_emergency = (
            counters
        )

        km_by_engineer[route.engineer_id] = to_km(route_m)
        if route.km != to_km(route_m):
            errors.append(
                f"{route.engineer_id}: пробег {route.km} км, пересчёт даёт {to_km(route_m)} км"
            )
        total_m += route_m
        travel_sec += route_travel
        wait_sec += route_wait
        work_sec += route_work
        emergency_sec += route_emergency
        used += 1 if route.stops else 0
        for priority, count in priorities.items():
            by_priority[priority] += count

    norm_sec = sum(
        requests[stop.request_id].service_duration_min * 60
        for route in plan.routes
        for stop in route.stops
        if stop.request_id in requests
    )
    # Метрики считаются от суммы секунд и метров, а не от суммы округлённых значений.
    # utilizationByEngineer и expectedLate не сверяем: их считают блоки 7 и 21. Нижнюю
    # границу бригад и число выполнимых — тоже: это оценки, а не факты плана (блок 50).
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
    errors += _insertable(
        plan, requests, engineers, travel, event_time, opened, pinned or {}, staged
    )
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
            (
                "emergencyWaitMin",
                metrics.emergency_wait_min,
                to_minutes(emergency_sec),
            ),
            (
                "complexityExtraMin",
                metrics.complexity_extra_min,
                to_minutes(work_sec - norm_sec),
            ),
            ("objectsByLevel", metrics.objects_by_level, _levels(plan.input.requests)),
            *_ratings(plan, engineers),
            ("firstLegMin", metrics.first_leg_min, _first_leg(plan, travel)),
        )
        if shown != computed
    ]
    errors += _complexities(plan)
    return Validation(ok=not errors, errors=errors)


def _ratings(plan: Plan, engineers: dict[str, Engineer]) -> list[tuple]:
    """Рейтинг исполнителей по маршрутам (блок 71): средний и заявок у 4,5+, в сотых звезды."""
    hundredths = []
    for route in plan.routes:
        engineer = engineers.get(route.engineer_id)
        if engineer is not None and engineer.rating is not None:
            hundredths += [round(engineer.rating.stars * 100)] * len(route.stops)
    average = round(sum(hundredths) / len(hundredths)) / 100 if hundredths else None
    top = sum(value >= round(TOP_RATING * 100) for value in hundredths)
    return [
        ("ratingAvg", plan.metrics.rating_avg, average),
        ("topRatedCount", plan.metrics.top_rated_count, top),
    ]


def _first_leg(plan: Plan, travel: Travel) -> int | None:
    """Дорога до первой заявки (блок 73): первое плечо каждого маршрута — по таблице от
    старта бригады, своим кодом. Плечо без пути уже названо в `_route` и в среднее не идёт."""
    engineers = {engineer.id: engineer for engineer in plan.input.engineers}
    firsts = []
    for route in plan.routes:
        engineer = engineers.get(route.engineer_id)
        if engineer is None or not route.stops:
            continue
        where, lead = start_id(plan.input.office, engineer), 0
        # Развернули на первом же плече (блок 74): дорога до аварии — проеханный кусок и
        # переезд от точки разворота.
        record = _records(engineer).get(route.stops[0].request_id)
        if record is not None and record.lat is not None and record.lon is not None:
            began = to_seconds(engineer.shift_start)
            if engineer.available_from is not None:
                began = max(began, to_seconds(engineer.available_from))
            where = turn_id(record.lat, record.lon)
            lead = max(to_seconds(record.time) - began, 0)
        leg = travel.travel(
            engineer.transport, where, point_id(route.stops[0].request_id)
        )
        if leg is not None:
            firsts.append(lead + leg[0])
    return to_minutes(round(sum(firsts) / len(firsts))) if firsts else None


def _complexities(plan: Plan) -> list[str]:
    """Сложность каждой заявки для бейджа (блок 67) — своим разбором объекта."""
    errors = []
    if set(plan.complexity) != {request.id for request in plan.input.requests}:
        errors.append("complexity: заявки не совпадают со входом плана")
    for request in plan.input.requests:
        shown = plan.complexity.get(request.id)
        value = _complexity(request)
        level = _level(value)
        if shown is not None and (shown.complexity, shown.level) != (
            value / table.UNIT,
            level,
        ):
            errors.append(
                f"заявка №{request.id}: сложность ×{shown.complexity} ({shown.level}), "
                f"пересчёт даёт ×{value / table.UNIT} ({level})"
            )
    return errors


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


def _insertable(
    plan: Plan,
    requests: dict[str, Request],
    engineers: dict[str, Engineer],
    travel: Travel,
    event_time: int | None,
    opened: int,
    pinned: dict[str, str],
    staged: bool,
) -> list[str]:
    """Досчёт обязан поставить всё, что встаёт (PLAN 6.6, блок 52).

    Неназначенная `NOT_FITTED` пробуется в каждый маршрут на каждое место после закреплённой
    головы, и пробный маршрут пересчитывается тем же `_route`: встала без единой ошибки —
    досчёт её пропустил.

    Законно не встают: у ручной правки (распределение задал диспетчер), при поэтапном
    расчёте (досчёт видел ужатые сроки аварий), в `insert` все, кроме заявки события, —
    прежние неназначенные он по контракту не трогает (PLAN 6.12), — и у базового варианта
    (алгоритм ТЗ в середину не вставляет, и `fallbackToBaseline` — тоже он). Заявку события
    `insert` ставит досчётом при любом алгоритме родителя, поэтому она проверяется всегда.
    """
    # Режим `delay` (блок 68) прежних неназначенных не трогает, как и `insert`.
    if (
        plan.algorithm is Algorithm.MANUAL
        or staged
        or plan.replan_mode is ReplanMode.DELAY
    ):
        return []
    inserting = plan.replan_mode is ReplanMode.INSERT
    if not inserting and (
        plan.algorithm is not Algorithm.OPTIMIZED or plan.fallback_to_baseline
    ):
        return []
    # Код строкой, а не из `explain`: валидатор не тянет за собой код, который план строил.
    left = [
        item.request_id
        for item in plan.unassigned
        if item.reason_code == "NOT_FITTED"
        and item.request_id in requests
        and (not inserting or item.request_id == plan.event.request.id)
    ]

    errors = []
    for request_id in left:
        spot = _spot(
            plan, request_id, requests, engineers, travel, event_time, opened, pinned
        )
        if spot is not None:
            errors.append(
                f"заявка №{request_id} встаёт к {spot[0]} на место {spot[1]} — "
                "досчёт её пропустил"
            )
    return errors


def _spot(
    plan: Plan,
    request_id: str,
    requests: dict[str, Request],
    engineers: dict[str, Engineer],
    travel: Travel,
    event_time: int | None,
    opened: int,
    pinned: dict[str, str],
) -> tuple[str, int] | None:
    """Первое место, куда заявка встаёт: бригада и номер места с единицы."""
    # Времён у пробного стопа нет: напечатанное `_route` с `printed=False` не сверяет.
    trial = Stop.model_construct(request_id=request_id, committed=False)
    for route in plan.routes:
        engineer = engineers.get(route.engineer_id)
        # Выбывшая бригада новой работы не берёт вовсе, и в `full`, где момента события нет,
        # тоже (`schedule.free_from`). Ресурс от места не зависит — не подошла, мест не
        # перебираем.
        if (
            engineer is None
            or engineer.unavailable_from is not None
            or pinned.get(request_id, engineer.id) != engineer.id
            or _resources(requests[request_id], engineer)
        ):
            continue
        # Закреплённое — голова маршрута (это проверяет `_commitment`), перед ним не встать.
        head = sum(stop.committed for stop in route.stops)
        for place in range(head, len(route.stops) + 1):
            stops = route.stops[:place] + [trial] + route.stops[place:]
            route_errors, _ = _route(
                route.model_copy(update={"stops": stops}),
                engineer,
                plan.input,
                requests,
                travel,
                event_time,
                plan.closed,
                opened,
                printed=False,
                running=plan.running,
            )
            if not route_errors:
                return engineer.id, place + 1
    return None


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
    day: Input,
    requests: dict[str, Request],
    travel: Travel,
    event_time: int | None = None,
    closed: dict | None = None,
    opened: int = 0,
    printed: bool = True,
    *,
    running: dict[str, str],
) -> tuple[list[str], tuple[int, int, int, int, dict[WorkPriority, int], int]]:
    """Пересчёт одного маршрута: ограничения, времена и слагаемые метрик.

    `opened` — начало рабочего дня: от него, если окно открылось раньше, ждёт авария.
    `printed=False` — маршрут пробный (блок 52): напечатанные времена с пересчётом не
    сверяются, после вставки они законно сдвигаются.
    """
    errors: list[str] = []
    by_priority = dict.fromkeys(WorkPriority, 0)
    route_m = route_travel = route_wait = route_work = route_emergency = 0

    shift_end = to_seconds(engineer.shift_end)
    where = start_id(day.office, engineer)
    mode = day.duration_mode
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
    records = _records(engineer)
    # Обед (блоки 79, 82) — своим правилом, не `schedule`: час между работами — после
    # работы, в её точке, перед выездом к следующей; до первой работы дня его нет. Ставится
    # сейчас, если не сдвигает начало следующей работы или после неё в окно уже не встанет;
    # иначе позже, а после последней работы — нет. Напечатанный обед в конце сверяется с
    # найденным здесь.
    window = (to_seconds(engineer.lunch_start), to_seconds(engineer.lunch_end))
    lunch_sec = LUNCH_MIN * 60
    hungry = when <= window[1] - lunch_sec and shift_end >= window[0] + lunch_sec
    worked = False
    # Конец обеда — в окне и в смене; после выбытия бригада не обедает.
    lunch_last = min(window[1], shift_end if left is None else min(shift_end, left))
    eaten: tuple[int, int] | None = None
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
        record = records.get(stop.request_id)
        if stop.committed:
            # Аварию события закрепляет сам выезд на неё (блок 74): исполнитель назначен в
            # момент события, даже если ещё доделывает текущую работу.
            sent = (
                record is not None
                and event_time is not None
                and to_seconds(record.time) == event_time
            )
            errors += _commitment(
                stop,
                engineer,
                event_time,
                free_seen,
                stop.request_id in (closed or {}) or sent,
            )
        else:
            free_seen = True
            if not waited:
                # Простой до события: обед, начатый в нём, был — и событие его не
                # обрывает, остаток дня идёт после обеда.
                at = max(when, window[0])
                if (
                    hungry
                    and worked
                    and at < event_time
                    and at + lunch_sec <= lunch_last
                ):
                    eaten, hungry = (at, at + lunch_sec), False
                    when = at + lunch_sec
                when, waited = max(when, event_time), True
            if left is not None and when >= left:
                errors.append(
                    f"{engineer.id}: бригада недоступна с {engineer.unavailable_from}, "
                    f"а к заявке {stop.request_id} выезжает в {to_clock_over_midnight(when)}"
                )
        errors += _resources(request, engineer)

        departure = when
        # Выезд на аварию (блок 74) — не раньше её поступления; развёрнутая бригада доехала
        # по прежнему плечу до точки разворота и поехала оттуда.
        lead_sec = lead_m = 0
        if record is not None and (record.lat is None or record.lon is None):
            # Свободная бригада стояла до аварии после работы: час окна в простое — её обед.
            at = max(when, window[0])
            if (
                hungry
                and worked
                and at + lunch_sec <= min(lunch_last, to_seconds(record.time))
            ):
                eaten, hungry = (at, at + lunch_sec), False
            departure = when = max(when, to_seconds(record.time))
        elif record is not None:
            errors += _turned(record, engineer, where, when, requests, travel)
            lead_sec = max(to_seconds(record.time) - when, 0)
            lead_m = to_meters(record.km or 0)
            where = turn_id(record.lat, record.lon)
            when = max(when, to_seconds(record.time))
        leg = travel.travel(engineer.transport, where, point_id(request.id))
        if leg is None:
            errors.append(
                f"{engineer.id}: до заявки {request.id} нет пути на этом транспорте"
            )
            break
        opens = to_seconds(request.window_start)
        work = _work_sec(request, engineer, mode)
        if hungry and worked and record is None:
            at = max(when, window[0])
            if at + lunch_sec > lunch_last:
                hungry = False
            elif (
                max(at + lunch_sec + leg[0], opens) == max(when + leg[0], opens)
                or max(max(when + leg[0], opens) + work, window[0]) + lunch_sec
                > lunch_last
            ):
                eaten, hungry = (at, at + lunch_sec), False
                departure = when = at + lunch_sec
        arrival = when + leg[0]
        seconds, meters = leg[0] + lead_sec, leg[1] + lead_m
        start = max(arrival, opens)
        end = start + work
        # Начатая работа кончается по факту (блок 68) — своим разбором `closed` и `running`:
        # закрытая — в момент закрытия, но не раньше начала; затянувшаяся — не раньше, чем
        # она ещё шла. Окно и смену у неё не проверяем: это уже случилось.
        closure = (closed or {}).get(stop.request_id)
        lasting = running.get(stop.request_id)
        if closure is not None:
            end = max(start, to_seconds(closure.time))
        elif lasting is not None:
            end = max(end, to_seconds(lasting))

        if printed:
            errors += _same(
                stop, departure, arrival, start, end, seconds, meters, work, engineer
            )
        if closure is None and lasting is None:
            errors += _time(request, engineer, start, end, shift_end, work)

        route_m += meters
        route_travel += seconds
        route_wait += start - arrival
        route_work += end - start
        if request.work_priority is WorkPriority.EMERGENCY:
            route_emergency += start - max(to_seconds(request.window_start), opened)
        by_priority[request.work_priority] += 1
        where, when, worked = point_id(request.id), end, True

    if printed:
        # Сверка — в минутах, как обед напечатан: секунды переезда в `HH:MM` не попадают.
        expected = None if eaten is None or not route.stops else _span(eaten)
        shown = (
            None if route.lunch is None else f"{route.lunch.start}–{route.lunch.end}"
        )
        if shown != expected:
            errors.append(
                f"{engineer.id}: обед в плане — {shown or 'нет'}, "
                f"по пересчёту — {expected or 'нет'}"
            )

    return errors, (
        route_m,
        route_travel,
        route_wait,
        route_work,
        by_priority,
        route_emergency,
    )


def _span(slot: tuple[int, int] | None) -> str:
    return (
        "нет"
        if slot is None
        else f"{to_clock_over_midnight(slot[0])}–{to_clock_over_midnight(slot[1])}"
    )


def _records(engineer: Engineer) -> dict[str, Dispatch]:
    """Выезды на аварию по аварии-якорю (блок 74) — своим разбором бригады."""
    return {record.emergency_id: record for record in engineer.dispatched}


def _turned(
    record: Dispatch,
    engineer: Engineer,
    where: str,
    when: int,
    requests: dict[str, Request],
    travel: Travel,
) -> list[str]:
    """Разворот лежит на брошенном плече, а не где угодно (блок 74).

    Точка на дороге от `where` к брошенной заявке не дальше от обоих концов, чем длина плеча:
    сумма расстояний по прямой не больше дороги — точка внутри эллипса с фокусами в концах.
    Проехано не больше плеча, развернули не раньше выезда.
    """
    name = f"{engineer.id}: разворот к аварии {record.emergency_id}"
    abandoned = requests.get(record.request_id or "")
    if abandoned is None or record.km is None:
        return [f"{name} без брошенной заявки"]
    leg = travel.travel(engineer.transport, where, point_id(abandoned.id))
    if leg is None:
        return [f"{name}: до заявки {abandoned.id} нет пути"]
    turn = (record.lat, record.lon)
    a = tuple(travel.points[travel.index[where]][1:])
    b = tuple(travel.points[travel.index[point_id(abandoned.id)]][1:])
    errors = []
    if haversine(a, turn) + haversine(turn, b) > leg[1] + TURN_SLACK_M:
        errors.append(f"{name}: точка разворота не на плече к заявке {abandoned.id}")
    if to_meters(record.km) > leg[1]:
        errors.append(
            f"{name}: проехано {record.km} км, плечо к заявке {abandoned.id} короче"
        )
    if to_seconds(record.time) < when:
        errors.append(f"{name} в {record.time}, раньше выезда к заявке {abandoned.id}")
    return errors


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
    request: Request,
    engineer: Engineer,
    start: int,
    end: int,
    shift_end: int,
    work: int,
) -> list[str]:
    """Окно заявки по текущему WINDOW_RULE и конец смены (PLAN 6.2, п. 3)."""
    errors = []
    window_start, window_end = (
        to_seconds(request.window_start),
        to_seconds(request.window_end),
    )
    latest = window_end
    if settings.window_rule == "fit_in_window":
        latest -= work
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


def _work_sec(request: Request, engineer: Engineer, mode: DurationMode) -> int:
    """Длительность работы пары своей арифметикой (PLAN 9.1), не `duration.seconds`.

    Из `duration.py` берётся только таблица коэффициентов — данные, как правило окна из
    конфига. Формула здесь своя: разъедься она с расписанием, план не пройдёт проверку.
    """
    if mode == DurationMode.NORM:
        return request.service_duration_min * 60
    return _complex_min(request, engineer.speed) * 60


def _complexity(request: Request) -> int:
    """Коэффициент сложности в тысячных — свой разбор объекта по таблице `duration`."""
    total = table.UNIT
    item = request.object
    if item is not None:
        house = item.building_type is BuildingType.PRIVATE
        if not house and not item.elevator and item.floor is not None:
            total += max(
                (add for low, add in table.FLOOR_STEPS if item.floor >= low), default=0
            )
        if not house and item.flats is not None:
            total += max(
                (add for low, add in table.FLATS_STEPS if item.flats >= low), default=0
            )
        total += table.BUILDING[item.building_type]
    total += request.gigabit * table.GIGABIT + request.repeat_visit * table.REPEAT_VISIT
    total += request.cable * table.CABLE + request.drilling * table.DRILLING
    total += table.DEVICE * max(0, len(request.required_equipment) - 1)
    low, high = table.CLAMP
    return max(low, min(high, total))


def _levels(requests: list[Request]) -> dict[str, int]:
    """Простые, средние и сложные объекты — по своему коэффициенту и порогам таблицы."""
    counts = dict.fromkeys(table.LEVELS, 0)
    for request in requests:
        counts[_level(_complexity(request))] += 1
    return counts


def _level(value: int) -> str:
    """Простой, средний или сложный объект — по порогам таблицы `duration.LEVELS`."""
    return next(name for name, top in table.LEVELS.items() if value <= top)


def _complex_min(request: Request, speed: int) -> int:
    """Минуты работы с учётом сложности при скорости `speed`."""
    work = request.service_duration_min - request.documents_min
    product = work * _complexity(request) * speed
    whole, rest = divmod(product, table.UNIT * table.UNIT)
    access = table.ACCESS_MIN[request.object.access] if request.object else 0
    return whole + (rest > 0) + access + request.documents_min


def _same(
    stop: Stop,
    departure: int,
    arrival: int,
    start: int,
    end: int,
    seconds: int,
    meters: int,
    work: int,
    engineer: Engineer,
) -> list[str]:
    """Сверка того, что записано в плане, с пересчётом: подделанное время обязано всплыть.

    Расчётная работа (`workMin`, блок 69) сверяется и у работы с фактом: её конец — факт,
    а план — по-прежнему длительность пары. У планов до блока 69 поля нет.
    """
    planned = (
        []
        if stop.work_min is None
        else [("работа по плану, мин", stop.work_min, to_minutes(work))]
    )
    return [
        f"{engineer.id}, заявка {stop.request_id}: {name} {shown}, пересчёт даёт {computed}"
        for name, shown, computed in (
            ("выезд", stop.departure, to_clock_over_midnight(departure)),
            ("прибытие", stop.arrival, to_clock_over_midnight(arrival)),
            ("начало", stop.start, to_clock_over_midnight(start)),
            ("окончание", stop.end, to_clock_over_midnight(end)),
            ("переезд, мин", stop.travel_min, to_minutes(seconds)),
            ("переезд, км", stop.travel_km, to_km(meters)),
            *planned,
        )
        if shown != computed
    ]
