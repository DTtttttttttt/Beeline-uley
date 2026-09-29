"""Оптимизация распределения на OR-Tools (PLAN 6.5).

Цель лексикографична: веса критериев строит `make_weights` из фактических границ набора,
поэтому каждый критерий дороже суммарной максимальной стоимости всех младших. Магических
констант в модели нет.

Поиск (`GUIDED_LOCAL_SEARCH` с лимитом времени) эвристический: он не обещает ни глобального
оптимума, ни одинакового результата двух прогонов — лимит настенный. Гарантируется другое:
план проходит независимую проверку и не хуже базового (PLAN 6.6).

Времена и расстояния — целые секунды и метры, как везде внутри алгоритма. Значения `CumulVar`
наружу не отдаются: расписание по готовым маршрутам пересчитывает `build_schedule`.
"""

from collections.abc import Callable

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from ..dictionaries import Transport, WorkPriority
from ..models import Input, Request
from ..routing.matrices import Travel
from ..routing.matrices import point_id as request_point
from ..timeutil import to_seconds
from .schedule import Position, capacity, latest_start, start_positions, units

DAY = 24 * 3600
# Непроходимое плечо: транзит не помещается в ёмкость измерения времени, дуга запрещена.
UNREACHABLE = 2 * DAY
INT64_MAX = 2**63 - 1  # предел целевой функции OR-Tools

# Порядок критериев от старшего к младшему (PLAN 6.5). Смена бизнес-приоритетов —
# перестановка списка, а не правка модели. Пробег всегда младший: его вес равен 1.
# `emergency_first` — умолчание (ответ 15: у аварии максимальный приоритет): невыполненная
# авария старше количества, дальше буквально по Дополнениям, п. 2.
ORDERS = {
    "emergency_first": [
        "emergency",
        "count",
        "engineers",
        "urgent",
        "type",
        "distance",
    ],
    "count_first": ["count", "engineers", "urgent", "type", "distance"],
    "urgent_first": ["count", "urgent", "type", "engineers", "distance"],
    "priority_first": ["type", "urgent", "count", "engineers", "distance"],
}

RANK = {
    WorkPriority.EMERGENCY: 3,
    WorkPriority.NEW_CONNECTION: 2,
    WorkPriority.REGULAR: 1,
}


def bounds(data: Input, travel: Travel) -> dict[str, int]:
    """Верхние границы стоимости каждого критерия на этом наборе (PLAN 6.5)."""
    return {
        "distance": max_arc(travel) * len(data.requests),
        "engineers": len(data.engineers),
        "type": max(RANK.values()) * len(data.requests),
        "urgent": len(data.requests),
        "count": len(data.requests),
        # Невыполненные аварии (ответ 15): их не больше, чем аварий в наборе.
        "emergency": sum(
            request.work_priority is WorkPriority.EMERGENCY for request in data.requests
        ),
    }


def make_weights(order: list[str], bounds: dict[str, int]) -> dict[str, int]:
    """Веса критериев: каждый дороже **суммы** максимальных стоимостей всех младших.

    Накопление важно. При границах `distance = 100`, `engineers = 10`, `type = 30` вес по
    соседнему диапазону дал бы `W["type"] = 1011`, тогда как младшие критерии вместе дают
    `10 × 101 + 100 × 1 = 1110` — и один шаг по типу работ перевесился бы экономией бригад.

    Пробег обязан быть младшим критерием: дуги модели стоят ровно столько метров, сколько
    в них есть (`SetArcCostEvaluatorOfVehicle`), то есть с весом 1. Переставить `distance`
    выше — значит молча развести цель солвера и `cost`, поэтому такой порядок отвергается.
    """
    if order[-1] != "distance":
        raise ValueError(
            "пробег обязан быть младшим критерием: его вес в модели равен 1"
        )
    weights, lower_max = {}, 0
    for name in reversed(order):  # идём снизу вверх
        weights[name] = lower_max + 1  # дороже всей максимальной стоимости младших
        lower_max += bounds[name] * weights[name]
    # Цель OR-Tools — int64, и при переполнении она насыщается молча: планы перестают
    # различаться. Старший вес растёт примерно как n⁴ × число бригад, поэтому на наборе
    # в несколько сотен заявок предел достижим — лучше отказать внятно.
    if lower_max > INT64_MAX:
        raise ValueError(
            "набор слишком велик для лексикографической цели: её максимум "
            f"{lower_max:.1e} больше предела int64 — уменьшите число заявок или инженеров"
        )
    return weights


def max_arc(travel: Travel) -> int:
    """Самое длинное плечо по всем матрицам набора — верхняя граница одного переезда."""
    return max(
        (
            value
            for matrix in travel.matrices.values()
            for row in matrix.distances
            for value in row
            if value is not None
        ),
        default=0,
    )


def penalty(request: Request, weights: dict[str, int]) -> int:
    """Во что обходится неназначенная заявка (PLAN 6.5).

    Критерия «невыполненные аварии» в порядке может не быть (`count_first` и остальные):
    веса у него тогда нет, и слагаемое нулевое.
    """
    emergency = request.work_priority is WorkPriority.EMERGENCY
    return (
        weights["count"]
        + emergency * weights.get("emergency", 0)
        + request.urgent * weights["urgent"]
        + RANK[request.work_priority] * weights["type"]
    )


def cost(
    routes: dict[str, list[Request]],
    unassigned: list[Request],
    meters: int,
    weights: dict[str, int],
) -> int:
    """Значение цели для готового плана — той же формулой, что минимизирует солвер.

    Ею сравниваются оптимизированный и базовый планы (PLAN 6.6): сравнение и цель
    не могут разъехаться, потому что это одна формула.
    """
    used = sum(1 for route in routes.values() if route)
    return (
        sum(penalty(request, weights) for request in unassigned)
        + used * weights["engineers"]
        + meters * weights["distance"]
    )


def solve(
    data: Input,
    travel: Travel,
    allowed: dict[str, list[str]],
    weights: dict[str, int],
    time_limit: int,
    positions: dict[str, Position] | None = None,
    initial: dict[str, list[str]] | None = None,
    guided: bool = True,
) -> tuple[dict[str, list[Request]], list[Request]] | None:
    """Маршруты бригад и неназначенные заявки; `None` — решения не нашлось.

    `allowed` — кандидаты из PLAN 6.3: заявка без единой подходящей бригады в модель не
    попадает. `initial` — маршруты прошлого плана как стартовое решение (PLAN 6.5).
    Ключи маршрутов — **все** бригады набора: простаивающая тоже попадает в план.
    `guided=False` — вариант «быстрый расчёт» (PLAN 6.14): первое решение без поиска.
    """
    positions = positions or start_positions(data)
    engineers = data.engineers
    planned = [request for request in data.requests if allowed[request.id]]
    routes: dict[str, list[Request]] = {engineer.id: [] for engineer in engineers}
    if not planned:
        return routes, list(data.requests)

    # Узлы: 0 — фиктивный финиш (маршрут без возврата), 1..E — старты бригад, дальше заявки.
    first = 1 + len(engineers)
    points = (
        [""]
        + [positions[engineer.id].point_id for engineer in engineers]
        + [request_point(request.id) for request in planned]
    )
    service = [0] * first + [request.service_duration_min * 60 for request in planned]
    # Точка, которой нет в таблицах, обернулась бы `KeyError` внутри callback'а — а его SWIG
    # не пропускает наружу и молча возвращает 0, обнуляя все транзиты. Проверяем заранее.
    unknown = [point for point in points[1:] if point not in travel.index]
    if unknown:
        raise ValueError(f"точек нет в таблицах переездов: {', '.join(unknown)}")

    manager = pywrapcp.RoutingIndexManager(
        len(points), len(engineers), list(range(1, first)), [0] * len(engineers)
    )
    routing = pywrapcp.RoutingModel(manager)

    arc = max_arc(travel)
    seconds, meters = {}, {}
    for transport in dict.fromkeys(engineer.transport for engineer in engineers):
        time_cb, meters_cb = _callbacks(
            manager, travel, transport, points, service, arc
        )
        seconds[transport] = routing.RegisterTransitCallback(time_cb)
        meters[transport] = routing.RegisterTransitCallback(meters_cb)

    for vehicle, engineer in enumerate(engineers):
        routing.SetArcCostEvaluatorOfVehicle(meters[engineer.transport], vehicle)
    routing.SetFixedCostOfAllVehicles(weights["engineers"])
    routing.AddDimensionWithVehicleTransits(
        [seconds[engineer.transport] for engineer in engineers], DAY, DAY, False, "Time"
    )
    time = routing.GetDimensionOrDie("Time")

    # Вместимость оборудования (PLAN 6.2, 6.5): спрос узла — единицы его заявки, у стартов и
    # фиктивного финиша ноль. Список по номеру узла, без таблиц: исключение внутри callback'а
    # SWIG наружу не пропускает. Бригаде без ограничения — весь спрос сразу, чтобы измерение
    # у всех было одно и не ветвилось по транспорту.
    demand = [0] * first + [units([request]) for request in planned]
    limits = [capacity(engineer) for engineer in engineers]
    routing.AddDimensionWithVehicleCapacity(
        routing.RegisterUnaryTransitCallback(
            lambda index: demand[manager.IndexToNode(index)]
        ),
        0,
        [
            sum(demand)
            if limit is None
            # Закреплённая часть могла выйти за вместимость (план другой настройки): тогда
            # остаётся ноль, и бригада берёт только работу без оборудования.
            else max(0, limit - positions[engineer.id].equipment_used)
            for engineer, limit in zip(engineers, limits, strict=True)
        ],
        True,
        "Equipment",
    )

    numbers = {engineer.id: number for number, engineer in enumerate(engineers)}
    nodes = {}
    for offset, request in enumerate(planned):
        index = manager.NodeToIndex(first + offset)
        nodes[request.id] = index
        time.CumulVar(index).SetRange(
            to_seconds(request.window_start), latest_start(request)
        )
        routing.AddDisjunction([index], penalty(request, weights))
        # SetAllowedVehiclesForIndex в питоновской обёртке ortools 9.15 не вызывается
        # (TypeError на absl::Span). Делаем то же, что она делает внутри: домен переменной
        # бригады — кандидаты плюс -1, то есть «заявка не назначена», иначе дизъюнкция мертва.
        routing.VehicleVar(index).SetValues(
            [-1] + [numbers[engineer_id] for engineer_id in allowed[request.id]]
        )

    for vehicle, engineer in enumerate(engineers):
        shift_start = to_seconds(engineer.shift_start)
        shift_end = to_seconds(engineer.shift_end)
        available = positions[engineer.id].available_from
        earliest = shift_start if available is None else max(shift_start, available)
        # min со сменой: бригада, освободившаяся после её конца, работы уже не получит,
        # а границы измерения обязаны остаться осмысленными.
        time.CumulVar(routing.Start(vehicle)).SetRange(
            min(earliest, shift_end), shift_end
        )
        time.CumulVar(routing.End(vehicle)).SetMax(shift_end)

    solution = _search(routing, engineers, nodes, initial, time_limit, guided)
    if solution is None:
        return None

    for vehicle, engineer in enumerate(engineers):
        index = routing.Start(vehicle)
        while not routing.IsEnd(index):
            node = manager.IndexToNode(index)
            if node >= first:
                routes[engineer.id].append(planned[node - first])
            index = solution.Value(routing.NextVar(index))

    assigned = {request.id for route in routes.values() for request in route}
    return routes, [request for request in data.requests if request.id not in assigned]


def _search(routing, engineers, nodes, initial, time_limit, guided=True):
    """Поиск: первое решение `LOCAL_CHEAPEST_INSERTION`, затем `GUIDED_LOCAL_SEARCH` до лимита.

    Первое решение — вставкой, потому что она видит штрафы дизъюнкций, а
    `PATH_CHEAPEST_ARC` их не видит: на встроенном наборе он оставлял без исполнителя три
    аварии из шести. В `emergency_first` цена такого старта на порядок выше, GLS отмеряет
    свои штрафы от неё, и за 10 секунд основная модель находила 47 заявок против 49 у
    `count_first` при той же фактической цели. Со вставкой — 49 у обоих (PLAN 6.5).

    `guided=False` останавливает поиск на первом решении (`solution_limit = 1`): лимит
    времени при этом остаётся страховкой, а не мерой качества. Метаэвристика в этом режиме
    не ставится — иначе она искала бы улучшение, которое всё равно некуда записать.
    """
    parameters = pywrapcp.DefaultRoutingSearchParameters()
    parameters.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.LOCAL_CHEAPEST_INSERTION
    )
    if guided:
        parameters.local_search_metaheuristic = (
            routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
        )
    else:
        parameters.solution_limit = 1
    parameters.time_limit.FromSeconds(time_limit)
    if initial is None:
        return routing.SolveWithParameters(parameters)

    # ReadAssignmentFromRoutes ждёт routing-индексы, а не номера узлов: на номерах она
    # молча возвращает None. Модель к этому моменту должна быть закрыта.
    routing.CloseModelWithParameters(parameters)
    start = routing.ReadAssignmentFromRoutes(
        [
            [
                nodes[request_id]
                for request_id in initial.get(engineer.id, [])
                if request_id in nodes
            ]
            for engineer in engineers
        ],
        True,
    )
    # Прошлый план мог стать недопустимым (бригада выбыла, окно ушло) — тогда ищем с нуля.
    if start is None:
        return routing.SolveWithParameters(parameters)
    return routing.SolveFromAssignmentWithParameters(start, parameters)


def _callbacks(
    manager,
    travel: Travel,
    transport: Transport,
    points: list[str],
    service: list[int],
    arc: int,
) -> tuple[Callable[[int, int], int], Callable[[int, int], int]]:
    """Транзиты одного профиля: секунды и метры (PLAN 6.5).

    Секунды — `длительность[откуда] + переезд[профиль][откуда][куда]`. До фиктивного финиша
    (узел 0) переезд нулевой, но работа последней заявки в транзит входит: иначе `CumulVar`
    конца маршрута описывал бы начало последней работы, и смена проверялась бы не по её
    окончанию. У непроходимого плеча (PLAN 3.4) секунды не помещаются в ёмкость измерения —
    дуга запрещена; метры такого плеча берём как самое длинное плечо набора, чтобы верхняя
    граница пробега из `bounds` осталась верной.

    Узел 0 бывает и **началом** дуги: солвер спрашивает транзиты и от конечных индексов.
    Точки у него нет, поэтому оба нуля обрабатываются до обращения к таблицам. Проверка
    обязана быть здесь: исключение внутри callback'а SWIG не пропускает наружу, а молча
    возвращает 0 — измерение времени тогда перестаёт что-либо ограничивать, и это видно
    только по нелепому расписанию.
    """

    def seconds(from_index: int, to_index: int) -> int:
        source, target = manager.IndexToNode(from_index), manager.IndexToNode(to_index)
        if 0 in (source, target):
            return service[source]  # у фиктивного финиша работы нет: service[0] == 0
        leg = travel.travel(transport, points[source], points[target])
        return service[source] + (UNREACHABLE if leg is None else leg[0])

    def meters(from_index: int, to_index: int) -> int:
        source, target = manager.IndexToNode(from_index), manager.IndexToNode(to_index)
        if 0 in (source, target):
            return 0
        leg = travel.travel(transport, points[source], points[target])
        return arc if leg is None else leg[1]

    return seconds, meters
