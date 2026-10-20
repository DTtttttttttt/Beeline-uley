"""Оптимизация распределения на OR-Tools (PLAN 6.5).

Цель лексикографична: веса критериев строит `make_weights` из фактических границ набора,
поэтому каждый критерий дороже суммарной максимальной стоимости всех младших. Магических
констант в модели нет.

Поиск (`GUIDED_LOCAL_SEARCH`) эвристический и глобального оптимума не обещает, но повторяем:
его останавливает предел числа решений, а не часы, поэтому одна модель с одним пределом
проходит одну траекторию на любой машине (блок 45). Лимит времени — страховка. Гарантируется
и другое: план проходит независимую проверку и не хуже базового (PLAN 6.6).

Времена и расстояния — целые секунды и метры, как везде внутри алгоритма. Значения `CumulVar`
наружу не отдаются: расписание по готовым маршрутам пересчитывает `build_schedule`.

Транзиты модель получает готовыми матрицами по номерам узлов, а не Python-колбэками: солвер
не зовёт Python на каждой дуге, и молча обнулённого транзита быть не может (блок 43).
"""

from time import perf_counter
from typing import NamedTuple

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from ..dictionaries import DurationMode, Transport, WorkPriority
from ..models import Engineer, Input, Request, SearchStats
from ..routing.matrices import Travel
from ..routing.matrices import point_id as request_point
from ..timeutil import to_seconds
from . import duration
from .schedule import (
    LUNCH_SEC,
    Position,
    capacity,
    culprit,
    day_end,
    first_violation,
    latest_start,
    lunch_due,
    lunch_window,
    start_positions,
    units,
)

DAY = 24 * 3600
# Непроходимое плечо: транзит не помещается в ёмкость измерения времени, дуга запрещена.
UNREACHABLE = 2 * DAY
INT64_MAX = 2**63 - 1  # предел целевой функции OR-Tools
TRACE_POINTS = 50  # сколько точек кривой сходимости хранит статистика поиска

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
    # «Рейтинг в приоритете» (блок 71): при равном числе заявок работа уходит лучшим по
    # рейтингу, даже ценой лишней бригады из резерва.
    "rating_first": ["emergency", "count", "rating", "engineers", "distance"],
}
# Этап 1 «аварий вперёд» (PLAN 6.5, блок 49): в модели одни аварии, и после их числа важно,
# как рано они начнутся, — ожидание старше пробега. Бригад этап 1 не считает: его маршруты
# только задают срок начала, а бригаду аварии выбирает этап 2.
STAGE1_ORDER = ["emergency", "emergency_wait", "distance"]

RANK = {
    WorkPriority.EMERGENCY: 3,
    WorkPriority.NEW_CONNECTION: 2,
    WorkPriority.REGULAR: 1,
}

# Рейтинг исполнителя для цели (блок 71) — в сотых звезды, средняя со сглаживанием: к отзывам
# добавляются `PRIOR_COUNT` оценок по `PRIOR_STARS`. Без отзывов — ровно 4,5, а пара пятёрок
# не обгоняет того, у кого сорок отзывов по 4,8. Резервист без отзывов — по худшей оценке
# (`LOW_STARS`): он берёт работу последним (PLAN 71.5).
TOP_STARS = 500
LOW_STARS = 100
PRIOR_STARS = 450
PRIOR_COUNT = 5


def bounds(data: Input, travel: Travel) -> dict[str, int]:
    """Верхние границы стоимости каждого критерия на этом наборе (PLAN 6.5)."""
    emergencies = sum(
        request.work_priority is WorkPriority.EMERGENCY for request in data.requests
    )
    return {
        "distance": max_arc(travel) * len(data.requests),
        "engineers": len(data.engineers),
        "type": max(RANK.values()) * len(data.requests),
        "urgent": len(data.requests),
        "count": len(data.requests),
        # Невыполненные аварии (ответ 15): их не больше, чем аварий в наборе.
        "emergency": emergencies,
        # Ожидание аварий в секундах (этап 1): начало каждой — в пределах суток от её окна.
        "emergency_wait": emergencies * DAY,
        # Штраф рейтинга (блок 71): каждая заявка — у самого слабого по рейтингу.
        "rating": max(map(rating_cost, data.engineers), default=0) * len(data.requests),
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


def rating_cost(engineer: Engineer) -> int:
    """Штраф рейтинга за одну заявку этого инженера: `500 − рейтинг×100` (блок 71).

    Рейтинг — снимок плана (`Engineer.rating`) со сглаживанием (`PRIOR_STARS`), целыми
    сотыми: в модели OR-Tools float нет.
    """
    rating = engineer.rating
    if rating is None:
        return TOP_STARS - (LOW_STARS if engineer.reserve else PRIOR_STARS)
    stars = round(rating.stars * 100)
    smoothed = round(
        (stars * rating.count + PRIOR_STARS * PRIOR_COUNT)
        / (rating.count + PRIOR_COUNT)
    )
    return TOP_STARS - smoothed


def penalty(request: Request, weights: dict[str, int]) -> int:
    """Во что обходится неназначенная заявка (PLAN 6.5).

    Критерия может не быть в порядке (`emergency` у `count_first`, количество и тип — у этапа
    1 аварий вперёд): веса у него тогда нет, и слагаемое нулевое.
    """
    emergency = request.work_priority is WorkPriority.EMERGENCY
    return (
        weights.get("count", 0)
        + emergency * weights.get("emergency", 0)
        + request.urgent * weights.get("urgent", 0)
        + RANK[request.work_priority] * weights.get("type", 0)
    )


def cost(
    routes: dict[str, list[Request]],
    unassigned: list[Request],
    meters: int,
    weights: dict[str, int],
    engineers: list[Engineer],
) -> int:
    """Значение цели для готового плана — той же формулой, что минимизирует солвер.

    Ею сравниваются оптимизированный и базовый планы (PLAN 6.6): сравнение и цель
    не могут разъехаться, потому что это одна формула. Рейтинг (блок 71) — штраф
    исполнителя за каждую его заявку; вне `rating_first` веса у него нет.
    """
    used = sum(1 for route in routes.values() if route)
    rated = sum(
        rating_cost(engineer) * len(routes[engineer.id]) for engineer in engineers
    )
    return (
        sum(penalty(request, weights) for request in unassigned)
        + used * weights["engineers"]
        + rated * weights.get("rating", 0)
        + meters * weights["distance"]
    )


class Solved(NamedTuple):
    """Решение солвера: маршруты всех бригад, неназначенные заявки и как шёл поиск.

    `stats` описывает сырое решение солвера — до досчёта и выбора лучшего (PLAN 6.6), так что
    цель плана может оказаться меньше `stats.objective`. `None` — поиска не было: ни у одной
    заявки нет подходящей бригады.
    """

    routes: dict[str, list[Request]]
    unassigned: list[Request]
    stats: SearchStats | None


def solve(
    data: Input,
    travel: Travel,
    allowed: dict[str, list[str]],
    weights: dict[str, int],
    time_limit: int,
    positions: dict[str, Position] | None = None,
    initial: dict[str, list[str]] | None = None,
    guided: bool = True,
    *,
    solution_limit: int,
    soft: dict[str, int] | None = None,
    required: frozenset[str] = frozenset(),
    descent: bool = False,
) -> Solved | None:
    """Маршруты бригад и неназначенные заявки; `None` — решения не нашлось.

    `allowed` — кандидаты из PLAN 6.3: заявка без единой подходящей бригады в модель не
    попадает. `initial` — маршруты прошлого плана как стартовое решение (PLAN 6.5).
    Ключи маршрутов — **все** бригады набора: простаивающая тоже попадает в план.
    `guided=False` — вариант «быстрый расчёт» (PLAN 6.14): первое решение без поиска.

    `solution_limit` — сколько решений принимает поиск; `time_limit` — только страховка
    (PLAN 6.5). У предела нет умолчания: забытый аргумент молча вернул бы поиск, который
    останавливают часы, а с ним и план, который на другой машине вышел бы другим.

    Аварии вперёд (PLAN 6.5, блок 49). `soft` — заявка → секунда, после которой каждая
    секунда начала стоит `weights["emergency_wait"]` (этап 1). `required` — заявки без
    дизъюнкции: их нельзя оставить неназначенными (этап 2, назначенные этапом 1 аварии).
    `descent` — после первого решения спуск до локального оптимума вместо GLS (этап 1).
    """
    positions = positions or start_positions(data)
    engineers = data.engineers
    planned = [request for request in data.requests if allowed[request.id]]
    routes: dict[str, list[Request]] = {engineer.id: [] for engineer in engineers}
    if not planned:
        return Solved(routes, list(data.requests), None)

    # Узлы: 0 — фиктивный финиш (маршрут без возврата), 1..E — старты бригад, дальше заявки.
    first = 1 + len(engineers)
    points = (
        [""]
        + [positions[engineer.id].point_id for engineer in engineers]
        + [request_point(request.id) for request in planned]
    )
    # Точка, которой нет в таблицах, — ошибка входа: называем её, а не падаем `KeyError`
    # посреди сборки матриц.
    unknown = [point for point in points[1:] if point not in travel.index]
    if unknown:
        raise ValueError(f"точек нет в таблицах переездов: {', '.join(unknown)}")

    manager = pywrapcp.RoutingIndexManager(
        len(points), len(engineers), list(range(1, first)), [0] * len(engineers)
    )
    routing = pywrapcp.RoutingModel(manager)

    # Матрица времени — на каждую пару «транспорт, скорость» (PLAN 9.1): работа на узле
    # зависит от скорости бригады. По нормативу скорость не участвует, и пара сводится к
    # транспорту — модель та же, что до блока 66. Метры от скорости не зависят.
    mode = data.duration_mode
    arc = max_arc(travel)
    seconds, plain, works = {}, {}, {}
    for engineer in engineers:
        key = _time_key(engineer, mode)
        if key in seconds:
            continue
        service = [0] * first + [
            duration.seconds(request, engineer, mode) for request in planned
        ]
        time_matrix, meters_matrix = _matrices(
            travel, engineer.transport, points, service, arc
        )
        seconds[key] = routing.RegisterTransitMatrix(time_matrix)
        # Работа на узле — для обеда (блок 79): перерыв не ложится на работу. Обёртка
        # принимает вектор по routing-индексам, а не по номерам узлов (как матрицы).
        works[key] = [
            service[manager.IndexToNode(index)]
            for index in range(routing.Size() + len(engineers))
        ]
        plain.setdefault(engineer.transport, meters_matrix)

    # Стоимость дуги — метры, а в `rating_first` ещё и штраф рейтинга бригады на въезде в
    # заявку (блок 71): матрица на пару «транспорт, штраф», без колбэков (блок 43).
    rated = weights.get("rating", 0)
    costs = {}
    for vehicle, engineer in enumerate(engineers):
        extra = rated * rating_cost(engineer)
        key = engineer.transport, extra
        if key not in costs:
            costs[key] = routing.RegisterTransitMatrix(
                [
                    [
                        value + extra * (target >= first)
                        for target, value in enumerate(row)
                    ]
                    for row in plain[engineer.transport]
                ]
            )
        routing.SetArcCostEvaluatorOfVehicle(costs[key], vehicle)
    routing.SetFixedCostOfAllVehicles(weights.get("engineers", 0))
    routing.AddDimensionWithVehicleTransits(
        [seconds[_time_key(engineer, mode)] for engineer in engineers],
        DAY,
        DAY,
        False,
        "Time",
    )
    time = routing.GetDimensionOrDie("Time")

    # Вместимость оборудования (PLAN 6.2, 6.5): спрос узла — единицы его заявки, у стартов и
    # фиктивного финиша ноль. Вектор по номеру узла, как и матрицы переездов. Бригаде без
    # ограничения — весь спрос сразу, чтобы измерение у всех было одно и не ветвилось по
    # транспорту.
    demand = [0] * first + [units([request]) for request in planned]
    limits = [capacity(engineer) for engineer in engineers]
    routing.AddDimensionWithVehicleCapacity(
        routing.RegisterUnaryTransitVector(demand),
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
    solver = routing.solver()
    nodes = {}
    for offset, request in enumerate(planned):
        index = manager.NodeToIndex(first + offset)
        nodes[request.id] = index
        domain = [numbers[engineer_id] for engineer_id in allowed[request.id]]
        # Крайнее начало зависит от бригады при `fit_in_window` (PLAN 9.1). Граница
        # `CumulVar` — по самой поздней из кандидатов, точная — элементом по бригаде заявки:
        # элемент 0 — «не назначена» (`VehicleVar = -1`), там ограничения нет.
        latest = [latest_start(request, engineer, mode) for engineer in engineers]
        top = max(latest[vehicle] for vehicle in domain)
        time.CumulVar(index).SetRange(to_seconds(request.window_start), top)
        if any(latest[vehicle] < top for vehicle in domain):
            solver.Add(
                time.CumulVar(index)
                <= solver.Element([top, *latest], routing.VehicleVar(index) + 1)
            )
        if request.id not in required:
            routing.AddDisjunction([index], penalty(request, weights))
            # «Заявка не назначена» — это -1 в домене; без него дизъюнкция мертва.
            domain = [-1, *domain]
        # SetAllowedVehiclesForIndex в питоновской обёртке ortools 9.15 не вызывается
        # (TypeError на absl::Span). Делаем то же, что она делает внутри: домен переменной
        # бригады — кандидаты, а у необязательной заявки ещё и -1.
        routing.VehicleVar(index).SetValues(domain)
        if soft and request.id in soft:
            time.SetCumulVarSoftUpperBound(
                index, soft[request.id], weights["emergency_wait"]
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
        # Обед (блоки 79, 82) — перерыв измерения времени: час в окне и в смене, до выезда, в
        # ожидании или после последней работы, но не поверх работы. Модель строже расписания:
        # там обед бывает только между работами (блок 82), а здесь он обязателен у каждого, кому
        # может понадобиться. Необязательный перерыв «по числу заявок» проверяли — поиск с ним
        # находит заметно хуже (на «Востоке» 47 заявок против 51). Работу, которую модель не
        # взяла из-за обеда, досчитывает `repair` правилом расписания. Кому обед положен,
        # решают те же функции, что и проход расписания.
        lunch_start, lunch_end = lunch_window(engineer)
        lunch_first = max(lunch_start, earliest)
        lunch_last = min(lunch_end, day_end(engineer)) - LUNCH_SEC
        if (
            not positions[engineer.id].lunch_done
            and lunch_due(engineer, earliest)
            and lunch_first <= lunch_last
        ):
            lunch = solver.FixedDurationIntervalVar(
                lunch_first, lunch_last, LUNCH_SEC, False, f"lunch {engineer.id}"
            )
            time.SetBreakIntervalsOfVehicle(
                [lunch], vehicle, works[_time_key(engineer, mode)]
            )

    if initial is not None:
        initial = trimmed(initial, engineers, planned, allowed, travel, positions)
    solution, stats = _search(
        routing, engineers, nodes, initial, time_limit, solution_limit, guided, descent
    )
    if solution is None:
        return None

    for vehicle, engineer in enumerate(engineers):
        index = routing.Start(vehicle)
        while not routing.IsEnd(index):
            node = manager.IndexToNode(index)
            if node >= first:
                routes[engineer.id].append(planned[node - first])
            index = solution.Value(routing.NextVar(index))

    # Обед модель знает, но ставит его где угодно в окне, а проход расписания — своим жадным
    # правилом (блок 79). Редкий маршрут, где правило не нашло места, которое нашёл солвер,
    # подрезается тем же способом, что и старт; снятое досчитает `repair`.
    for engineer in engineers:
        route, at = routes[engineer.id], positions[engineer.id]
        while first_violation(engineer, route, travel, at) is not None:
            route.remove(culprit(engineer, route, travel, at)[0])

    assigned = {request.id for route in routes.values() for request in route}
    unassigned = [request for request in data.requests if request.id not in assigned]
    return Solved(routes, unassigned, stats)


def _search(
    routing,
    engineers,
    nodes,
    initial,
    time_limit,
    solution_limit,
    guided,
    descent=False,
):
    """Поиск: первое решение `LOCAL_CHEAPEST_INSERTION`, затем `GUIDED_LOCAL_SEARCH` до предела.

    Первое решение — вставкой, потому что она видит штрафы дизъюнкций, а
    `PATH_CHEAPEST_ARC` их не видит: на встроенном наборе он оставлял без исполнителя три
    аварии из шести. В `emergency_first` цена такого старта на порядок выше, GLS отмеряет
    свои штрафы от неё, и за 10 секунд основная модель находила 47 заявок против 49 у
    `count_first` при той же фактической цели. Со вставкой — 49 у обоих (PLAN 6.5).

    Поиск останавливает предел числа решений, а не часы (PLAN 6.5): одна и та же модель с
    одним и тем же пределом проходит одну и ту же траекторию, и план повторяется на любой
    машине. Часы — страховка: если машина не успела, `stopped_by = "time"`, и повторяемость
    этого плана уже не обещана.

    `guided=False` останавливает поиск на первом решении (`solution_limit = 1`), переданный
    предел тогда не действует. Метаэвристика в этом режиме не ставится — иначе она искала бы
    улучшение, которое всё равно некуда записать.

    Возвращает решение (или `None`) и статистику поиска. Статус солвера не отличает остановку
    по времени от остановки по числу решений, поэтому `stopped_by` выводится из счётчика.
    Третьего исхода — поиск кончился сам — у GLS нет, но и до предела он доходит, только
    пока есть что переставлять: штрафы GLS выталкивают его в новые решения. Если менять
    нечего (в модели одна заявка или день единственной бригады занят целиком), новых решений
    нет, и поиск останавливают часы. Поэтому этап 1 аварий вперёд (блок 49), где авария
    бывает одна, ищет спуском (`descent`): первое решение, затем `GREEDY_DESCENT` до
    локального оптимума. Спуск кончается сам и детерминированно; не дошёл до оптимума к
    страховке — `stopped_by = "time"`.
    """
    parameters = pywrapcp.DefaultRoutingSearchParameters()
    parameters.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.LOCAL_CHEAPEST_INSERTION
    )
    if descent:
        parameters.local_search_metaheuristic = (
            routing_enums_pb2.LocalSearchMetaheuristic.GREEDY_DESCENT
        )
        # Спуск кончается сам, но предел решений у него тот же, что у всякого поиска: без
        # него большой этап 1 останавливали бы только часы.
        parameters.solution_limit = solution_limit
    elif guided:
        parameters.local_search_metaheuristic = (
            routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
        )
        parameters.solution_limit = solution_limit
    else:
        parameters.solution_limit = 1
    parameters.time_limit.FromSeconds(time_limit)
    # Часы есть и внутри поиска: шаг достройки соседства LNS по умолчанию ограничен 100 мс,
    # и на загруженной машине соседство, не уложившееся в них, отбрасывалось бы — траектория
    # зависела бы от времени. Ограничивает его та же страховка, что и весь поиск.
    parameters.lns_time_limit.FromSeconds(time_limit)

    solutions, trace = 0, []

    # Единственный Python-колбэк модели: зовётся раз на принятое решение, а не на дугу, и
    # только считает. Исключение здесь SWIG проглатывает молча — статистика тогда пустая, это
    # ловит тест. Колбэк держит только переменную цели, а не `routing`: C++ хранит ссылку на
    # колбэк, и через `routing` вышел бы цикл, невидимый сборщику мусора.
    def found() -> None:
        nonlocal solutions
        solutions += 1
        value = objective.Value()
        if not trace or value < trace[-1][1]:
            trace.append((round(perf_counter() - started, 3), value))

    routing.AddAtSolutionCallback(found)
    # Модель закрывается до старта часов: время и кривая описывают поиск, а не сборку модели.
    # Переменная цели появляется только при закрытии — до него `CostVar()` пуст.
    routing.CloseModelWithParameters(parameters)
    objective = routing.CostVar()
    # ReadAssignmentFromRoutes ждёт routing-индексы, а не номера узлов: на номерах она молча
    # возвращает None. Модель к этому моменту должна быть закрыта.
    start = (
        None
        if initial is None
        else routing.ReadAssignmentFromRoutes(
            [
                [nodes[request_id] for request_id in initial[engineer.id]]
                for engineer in engineers
            ],
            True,
        )
    )
    started = perf_counter()
    # Прошлый план мог стать недопустимым (бригада выбыла, окно ушло) — тогда ищем с нуля,
    # и `warm_started` в статистике это показывает.
    solution = (
        routing.SolveWithParameters(parameters)
        if start is None
        else routing.SolveFromAssignmentWithParameters(start, parameters)
    )
    if solution is None:
        return None, None
    # Прочитанный старт колбэк тоже считает, а предел солвера — нет: без поправки поиск,
    # который часы остановили за решение до предела, назвался бы остановленным пределом.
    found = solutions - (start is not None)
    # Спуск, дошедший до оптимума или до предела, остановлен не часами; GLS сам не
    # кончается, поэтому у него о часах говорит только недобор до предела.
    unfinished = found < parameters.solution_limit and (
        not descent
        or routing.status()
        == routing_enums_pb2.RoutingSearchStatus.ROUTING_PARTIAL_SUCCESS_LOCAL_OPTIMUM_NOT_REACHED
    )
    return solution, SearchStats(
        objective=solution.ObjectiveValue(),
        solutions=solutions,
        stopped_by="time" if unfinished else "solutions",
        warm_started=start is not None,
        sec=round(perf_counter() - started, 3),
        trace=_thin(trace),
    )


def trimmed(
    initial: dict[str, list[str]],
    engineers: list[Engineer],
    planned: list[Request],
    allowed: dict[str, list[str]],
    travel: Travel,
    positions: dict[str, Position],
) -> dict[str, list[str]]:
    """Стартовое решение, подрезанное до допустимого (PLAN 6.5, блок 44).

    `ReadAssignmentFromRoutes` отвергает старт **целиком**, если в нём есть хоть одна
    невыполнимая заявка, и поиск молча идёт с нуля — да ещё на укороченном лимите
    перепланирования, которым он расплачивается как раз за этот прогрев. Поэтому старт
    сначала сводится к тому, из чего построена модель: только её заявки (`planned`) и
    только у бригад из их домена (`allowed`). Затем каждый маршрут проверяется арифметикой
    расписания от точки продолжения, и пока в нём есть нарушение, из него снимается
    виновник (`schedule.culprit`). Проверяется ровно то, что получит модель: заявка,
    выпавшая из маршрута, меняет плечо между соседями. Подрезаются только виновные
    маршруты. Стопы выбывшей бригады уходят так же: её день начинается концом смены.
    """
    known = {request.id: request for request in planned}
    trimmed = {}
    for engineer in engineers:
        at = positions[engineer.id]
        route = [
            known[request_id]
            for request_id in initial.get(engineer.id, [])
            if request_id in known and engineer.id in allowed[request_id]
        ]
        while first_violation(engineer, route, travel, at) is not None:
            route.remove(culprit(engineer, route, travel, at)[0])
        trimmed[engineer.id] = [request.id for request in route]
    return trimmed


def _thin(points: list[tuple[float, int]]) -> list[tuple[float, int]]:
    """Кривая не длиннее `TRACE_POINTS`: точки равномерно по списку, крайние на месте."""
    if len(points) <= TRACE_POINTS:
        return points
    step = (len(points) - 1) / (TRACE_POINTS - 1)
    return [points[round(number * step)] for number in range(TRACE_POINTS)]


def _time_key(engineer: Engineer, mode: DurationMode) -> tuple:
    """Чем бригады различаются во времени: транспортом, а с учётом сложности — и скоростью."""
    if mode == DurationMode.NORM:
        return (engineer.transport,)
    return engineer.transport, engineer.speed


def _matrices(
    travel: Travel,
    transport: Transport,
    points: list[str],
    service: list[int],
    arc: int,
) -> tuple[list[list[int]], list[list[int]]]:
    """Транзиты одного профиля по номерам узлов: секунды и метры (PLAN 6.5).

    Секунды — `длительность[откуда] + переезд[профиль][откуда][куда]`. До фиктивного финиша
    (узел 0) переезд нулевой, но работа последней заявки в транзит входит: иначе `CumulVar`
    конца маршрута описывал бы начало последней работы, и смена проверялась бы не по её
    окончанию. У непроходимого плеча (PLAN 3.4) секунды не помещаются в ёмкость измерения —
    дуга запрещена; метры такого плеча берём как самое длинное плечо набора, чтобы верхняя
    граница пробега из `bounds` осталась верной.

    Узел 0 бывает и **началом** дуги: солвер спрашивает транзиты и от конечных индексов.
    Точки у него нет, поэтому его строка и столбец заполняются без обращения к таблицам.

    `RegisterTransitMatrix` читает матрицу по номерам узлов, а не по routing-индексам, — на
    этой модели они не совпадают ни в одном узле. Это сверяет тест цены решения.
    """
    size = len(points)
    seconds = [[service[source]] * size for source in range(size)]
    meters = [[0] * size for _ in range(size)]
    for source in range(1, size):
        for target in range(1, size):
            leg = travel.travel(transport, points[source], points[target])
            seconds[source][target] += UNREACHABLE if leg is None else leg[0]
            meters[source][target] = arc if leg is None else leg[1]
    return seconds, meters
