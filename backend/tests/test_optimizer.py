"""Оптимизация OR-Tools: веса цели, порядок критериев, досчёт и выбор лучшего (блок 8.11).

Критерии проверяются на маленьких искусственных примерах с очевидным ответом и коротким
лимитом: эвристический поиск не обещает глобального оптимума, и тест не должен этого
утверждать. На встроенном наборе проверяется только то, что план валиден и не хуже базового.

Ни Valhalla, ни Mongo тестам не нужны: таблицы переездов собираются фикстурой из плеч.
"""

import gc
from dataclasses import replace

import pytest
from ortools.constraint_solver import pywrapcp

from app.config import Settings, settings
from app.data import seed
from app.dictionaries import DurationMode
from app.models import Algorithm, Variant
from app.planning import candidates, optimizer, repair, service, variants
from app.planning.schedule import Position, build_schedule, meters, start_positions
from app.routing import matrices
from app.routing.matrices import point_id

# Заявка на 8 часов: бригада, взявшая её, за день больше ничего не успевает.
WHOLE_DAY = {"serviceDurationMin": 480, "windowStart": "09:00", "windowEnd": "10:00"}

# Предел решений маленьких примеров: ответ у них очевиден, и поиск останавливается за
# миллисекунды, а не ждёт лимит времени (PLAN 6.5).
SOLUTIONS = 300


def weights_for(data, travel, mode="count_first"):
    return optimizer.make_weights(
        optimizer.ORDERS[mode], optimizer.bounds(data, travel)
    )


def solve(data, travel, mode="count_first", limit=2, positions=None, initial=None):
    """Решение солвера без досчёта и сравнения с базовым: проверяем саму цель.

    `positions` идёт и в отбор кандидатов: 6.3 требует отбирать от текущего состояния
    бригады, и тест, прогоняющий состояние только через модель, этого бы не заметил.
    """
    solved = optimizer.solve(
        data,
        travel,
        candidates.allowed(data, travel, positions),
        weights_for(data, travel, mode),
        limit,
        positions=positions,
        initial=initial,
        solution_limit=SOLUTIONS,
    )
    return solved.routes, solved.unassigned


def served(routes) -> set[str]:
    return {request.id for route in routes.values() for request in route}


def busy(routes) -> list[str]:
    return [engineer_id for engineer_id, route in routes.items() if route]


# --- веса цели (PLAN 6.5) ----------------------------------------------------


@pytest.mark.parametrize("mode", list(optimizer.ORDERS))
def test_every_weight_beats_the_sum_of_all_lower_criteria(mode):
    """Критерий дороже **суммы** максимальных стоимостей всех младших, а не соседнего."""
    bounds = {
        "distance": 100,
        "engineers": 10,
        "type": 30,
        "urgent": 7,
        "count": 7,
        "emergency": 7,
        "rating": 400 * 7,
    }
    order = optimizer.ORDERS[mode]
    weights = optimizer.make_weights(order, bounds)

    for position, name in enumerate(order):
        lower = sum(bounds[name] * weights[name] for name in order[position + 1 :])
        assert weights[name] > lower, f"{mode}: {name} не перекрывает младшие"
    assert weights["distance"] == 1  # пробег — младший критерий, его вес единичный


def test_weights_refuse_to_overflow_int64():
    """Цель OR-Tools — int64 и насыщается молча; сотни заявок доводят старший вес до
    предела, и лучше внятный отказ, чем планы, которые солвер перестал различать."""
    bounds = {
        "distance": 300_000 * 600,
        "engineers": 60,
        "type": 3 * 600,
        "urgent": 600,
        "count": 600,
        "emergency": 600,
    }

    with pytest.raises(ValueError, match="int64"):
        optimizer.make_weights(optimizer.ORDERS["emergency_first"], bounds)


def test_bounds_come_from_the_dataset(
    make_request, make_engineer, make_travel, make_dataset
):
    """Границы берутся из фактических данных, а не из констант (PLAN 6.5)."""
    requests = [make_request("1"), make_request("2")]
    data = make_dataset(requests, [make_engineer()])

    bounds = optimizer.bounds(data, make_travel(requests, leg=(600, 7000)))

    assert bounds == {
        "distance": 7000 * 2,
        "engineers": 1,
        "type": 3 * 2,
        "urgent": 2,
        "count": 2,
        "emergency": 0,  # аварий в наборе нет — и невыполненных быть не может
        "emergency_wait": 0,  # и ждать некому (этап 1 аварий вперёд, блок 49)
        "rating": (500 - 450) * 2,  # без отзывов — 4,5, штраф 50 на заявку (блок 71)
    }


# --- порядок критериев (PLAN 6.5) --------------------------------------------


@pytest.fixture
def one_or_two(make_request, make_engineer, make_travel, make_dataset):
    """Одна бригада: либо авария на весь день, либо две обычные заявки по часу."""
    requests = [
        make_request("r1", windowEnd="17:00"),
        make_request(
            "emergency", skill="emergency", workPriority="emergency", **WHOLE_DAY
        ),
        make_request("r2", windowEnd="17:00"),
    ]
    engineers = [make_engineer("brigade-1", skills=["local", "emergency"])]
    return make_dataset(requests, engineers), make_travel(requests)


def test_count_first_takes_two_regular_requests_over_one_emergency(one_or_two):
    """Заявок больше — план лучше, даже если это обычные заявки (Дополнения, п. 2)."""
    routes, unassigned = solve(*one_or_two)

    assert served(routes) == {"r1", "r2"}
    assert [request.id for request in unassigned] == ["emergency"]


def test_emergency_first_takes_the_emergency_over_two_regular_requests(one_or_two):
    """Ответ 15: у аварии максимальный приоритет — две обычные заявки её не перевешивают."""
    routes, unassigned = solve(*one_or_two, mode="emergency_first")

    assert served(routes) == {"emergency"}
    assert {request.id for request in unassigned} == {"r1", "r2"}


def test_request_needing_a_tool_goes_only_to_a_brigade_with_it(
    make_request, make_engineer, make_travel, make_dataset
):
    """Инструмент доезжает до модели через домен бригады (PLAN 6.5, блок 27.8): первая
    бригада стоит в списке раньше и ничем не хуже, но ноутбука у неё нет."""
    requests = [make_request("1", requiredTools=["laptop"], windowEnd="17:00")]
    engineers = [
        make_engineer("brigade-1", tools=["cable_tester"]),
        make_engineer("brigade-2", tools=["laptop"]),
    ]

    routes, unassigned = solve(make_dataset(requests, engineers), make_travel(requests))

    assert [request.id for request in routes["brigade-2"]] == ["1"]
    assert routes["brigade-1"] == []
    assert unassigned == []


def test_model_knows_lunch_and_splits_what_does_not_fit_with_it(
    make_request, make_engineer, make_travel, make_dataset
):
    """Обед в модели (блок 79). Ближней бригаде по силам каждая заявка, но не обе: обед
    12:00–14:00 не встаёт ни между ними (вторая опоздает), ни после (выйдет за окно). Модель
    сразу не даёт ей обе, и все заявки назначены. Без обеда в модели обе взяла бы ближняя, и подрезка оставила
    бы одну без исполнителя."""
    requests = [
        make_request("1", windowStart="11:00", windowEnd="11:05"),
        make_request("2", windowStart="12:00", windowEnd="13:00"),
    ]
    engineers = [
        make_engineer("brigade-1", lunchStart="12:00", lunchEnd="14:00"),
        make_engineer("brigade-2", startLat=55.80, startLon=37.90),
    ]
    data = make_dataset(requests, engineers)
    starts = start_positions(data)
    positions = {
        engineer_id: replace(position, lunch_done=False)
        for engineer_id, position in starts.items()
    }
    far = starts["brigade-2"].point_id
    travel = make_travel(
        requests,
        legs={(far, "1"): (1800, 20_000), (far, "2"): (1800, 20_000)},
        engineers=engineers,
    )

    routes, unassigned = solve(data, travel, positions=positions)

    assert unassigned == []
    assert len(routes["brigade-1"]) < 2


def test_single_work_through_the_lunch_window_is_still_planned(
    make_request, make_engineer, make_travel, make_dataset, monkeypatch
):
    """Обед только между работами (блок 82): одна работа через всё окно обеда допустима.
    Модель держит обед строже расписания и такую работу не берёт, но досчёт ставит её правилом
    расписания — в плане она есть, обеда нет, валидатор согласен."""
    request = make_request("1", serviceDurationMin=480, windowEnd="10:00")
    engineer = make_engineer(lunchStart="13:00", lunchEnd="16:00")
    data = make_dataset([request], [engineer])
    # У модели одно решение — «не назначена», и предела решений поиск не наберёт: ждать часы.
    monkeypatch.setattr(settings, "solver_time_limit_sec", 2)

    plan = service.build_plan(data, make_travel([request]), Algorithm.OPTIMIZED)

    assert plan.validation.ok, plan.validation.errors
    (route,) = plan.routes
    assert [stop.request_id for stop in route.stops] == ["1"]
    assert route.lunch is None


def test_emergency_first_is_the_default_and_survives_the_baseline_check(
    one_or_two, monkeypatch
):
    """По умолчанию авария старше количества, и выбор лучшего это не отменяет (PLAN 6.6).

    Базовый вариант ТЗ берёт заявки по порядку файла — две обычные — и по цели
    `emergency_first` проигрывает: подменять им план с аварией нельзя.
    """
    default = Settings.model_fields["objective_mode"].default
    monkeypatch.setattr(settings, "objective_mode", default)
    monkeypatch.setattr(settings, "solver_time_limit_sec", 2)

    plan = service.build_plan(*one_or_two, Algorithm.OPTIMIZED)

    assert default == "emergency_first"
    assert plan.assignments["emergency"] == "brigade-1"
    assert not plan.fallback_to_baseline
    assert plan.validation.ok


def test_first_solution_already_sees_the_emergency_penalty(one_or_two):
    """Первое решение — вставкой, которая видит штрафы дизъюнкций (PLAN 6.5).

    `PATH_CHEAPEST_ARC` их не видел и оставлял аварии без исполнителя, а от цены старта
    GLS отмеряет свои штрафы. Без поиска («быстрый расчёт») авария берётся сразу.
    """
    data, travel = one_or_two
    routes = optimizer.solve(
        data,
        travel,
        candidates.allowed(data, travel),
        weights_for(data, travel, "emergency_first"),
        1,
        guided=False,
        solution_limit=SOLUTIONS,
    ).routes

    assert served(routes) == {"emergency"}


def test_priority_first_takes_the_emergency_instead(one_or_two):
    """Тот же набор, другой порядок критериев — другой план (PLAN 6.5)."""
    routes, unassigned = solve(*one_or_two, mode="priority_first")

    assert served(routes) == {"emergency"}
    assert {request.id for request in unassigned} == {"r1", "r2"}


def test_count_first_saves_an_engineer_at_the_cost_of_kilometres(
    make_request, make_engineer, make_travel, make_dataset
):
    """При равном количестве заявок — меньше бригад, даже если пробег вырастет."""
    requests = [make_request("1"), make_request("2")]
    engineers = [make_engineer("brigade-1"), make_engineer("brigade-2")]
    # Развезти по бригадам — 10 км, одной бригадой — 25 км. Экономия бригады старше километров.
    travel = make_travel(
        requests, legs={("1", "2"): (600, 20000), ("2", "1"): (600, 20000)}
    )

    routes, unassigned = solve(make_dataset(requests, engineers), travel)

    assert unassigned == []
    assert len(busy(routes)) == 1


@pytest.fixture
def one_slot(make_request, make_engineer, make_travel, make_dataset):
    """Бригада успевает ровно одну заявку из двух: выбор решают младшие критерии."""

    def make(first: dict, second: dict):
        requests = [
            make_request("first", **WHOLE_DAY | first),
            make_request("second", **WHOLE_DAY | second),
        ]
        engineers = [make_engineer("brigade-1", skills=["local", "emergency"])]
        return make_dataset(requests, engineers), make_travel(requests)

    return make


def test_count_first_prefers_the_urgent_request(one_slot):
    """При равном количестве и числе бригад — срочная заявка (ТЗ, п. 2.4.1)."""
    routes, _ = solve(*one_slot({}, {"urgent": True}))

    assert served(routes) == {"second"}


def test_count_first_prefers_the_emergency_work_type(one_slot):
    """При равной срочности — более приоритетный тип работ (Дополнения, п. 9)."""
    routes, _ = solve(
        *one_slot({}, {"skill": "emergency", "workPriority": "emergency"})
    )

    assert served(routes) == {"second"}


@pytest.fixture
def urgent_or_engineer(make_request, make_engineer, make_travel, make_dataset):
    """Две заявки можно взять либо одной бригадой без срочной, либо двумя со срочной.

    Бригада 1 успевает или срочное подключение на весь день, или обе обычные заявки.
    Бригада 2 работает до 10:30 и успевает только первую из них.
    """
    requests = [
        make_request("regular-1"),
        make_request("regular-2", windowStart="11:00", windowEnd="12:00"),
        make_request("urgent", skill="connection", urgent=True, **WHOLE_DAY),
    ]
    engineers = [
        make_engineer("brigade-1", skills=["local", "connection"]),
        make_engineer("brigade-2", shiftEnd="10:30"),
    ]
    return make_dataset(requests, engineers), make_travel(requests)


def test_count_first_skips_the_urgent_request_to_save_an_engineer(urgent_or_engineer):
    """Буквальное чтение Дополнений, п. 2: экономия бригады старше срочности (PLAN 6.5)."""
    routes, unassigned = solve(*urgent_or_engineer)

    assert served(routes) == {"regular-1", "regular-2"}
    assert len(busy(routes)) == 1
    assert [request.id for request in unassigned] == ["urgent"]


def test_urgent_first_takes_the_urgent_request_instead(urgent_or_engineer):
    """Тот же набор при `urgent_first`: срочность важнее экономии бригад."""
    routes, _ = solve(*urgent_or_engineer, mode="urgent_first")

    assert "urgent" in served(routes)
    assert len(busy(routes)) == 2


# --- состояние бригады: точка продолжения и availableFrom (PLAN 6.3, 6.5) ----


def test_engineer_continues_from_its_position_and_available_time(
    make_request, make_engineer, make_travel, make_dataset
):
    """Работы считаются от точки продолжения и времени освобождения, а не от офиса и смены."""
    done = make_request("done")
    long_one = make_request("long", windowEnd="18:00")  # час работы: до 18:00 не успеть
    short = make_request("short", serviceDurationMin=30, windowEnd="18:00")
    data = make_dataset([long_one, short], [make_engineer("brigade-1")])
    travel = make_travel([done, long_one, short])
    # Бригада освободилась в 17:00 у заявки «done»: переезд 10 минут, работа с 17:10.
    positions = {
        "brigade-1": Position(
            point_id("done"),
            17 * 3600,
            equipment_used=0,
            duration_mode=DurationMode.NORM,
            finished={},
            turns={},
            lunch_done=True,
            after_work=False,
        )
    }

    routes, unassigned = solve(data, travel, positions=positions)

    assert served(routes) == {"short"}
    assert [request.id for request in unassigned] == ["long"]
    assert solve(data, travel)[1] == []  # без этого состояния успевают обе


# --- стартовое решение (PLAN 6.5, блок 8.7; шпион — в conftest) --------------


def test_previous_plan_is_read_as_the_initial_solution(
    make_request, make_engineer, make_travel, make_dataset, spy_initial
):
    requests = [make_request("1"), make_request("2")]
    data = make_dataset(requests, [make_engineer("brigade-1")])

    routes, unassigned = solve(
        data, make_travel(requests), initial={"brigade-1": ["2", "1"]}
    )

    assert spy_initial and all(assignment is not None for assignment in spy_initial)
    assert served(routes) == {"1", "2"}
    assert unassigned == []


@pytest.fixture
def two_whole_days(make_request, make_engineer, make_travel, make_dataset):
    """Две заявки на весь день в одном маршруте: такой старт целиком не выполним."""
    requests = [make_request("1", **WHOLE_DAY), make_request("2", **WHOLE_DAY)]
    data = make_dataset(requests, [make_engineer("brigade-1")])
    travel = make_travel(requests)

    def solve_from(initial):
        return optimizer.solve(
            data,
            travel,
            candidates.allowed(data, travel),
            weights_for(data, travel),
            1,
            initial=initial,
            solution_limit=SOLUTIONS,
        )

    return solve_from


def test_impossible_initial_solution_is_trimmed_not_dropped(
    two_whole_days, spy_initial
):
    """Недопустимый старт теряет только виноватую заявку, а не читается целиком мимо
    (блок 44): иначе одна заявка отправила бы весь остаток дня считаться с нуля."""
    solved = two_whole_days({"brigade-1": ["1", "2"]})

    assert spy_initial and all(start is not None for start in spy_initial)
    assert solved.stats.warm_started is True
    assert len(served(solved.routes)) == 1 and len(solved.unassigned) == 1


def test_unreadable_initial_solution_falls_back_to_a_cold_search(
    two_whole_days, spy_initial, monkeypatch
):
    """Если старт всё-таки не прочитан — арифметика расписания и модель разошлись, —
    поиск идёт с нуля, а не падает, и говорит об этом флагом: молча посчитанный с нуля
    план диспетчер не отличил бы от пересчитанного от прошлого (блок 44)."""
    monkeypatch.setattr(optimizer, "trimmed", lambda initial, *_: initial)

    solved = two_whole_days({"brigade-1": ["1", "2"]})

    assert spy_initial == [None]
    assert solved.stats.warm_started is False
    assert len(served(solved.routes)) == 1 and len(solved.unassigned) == 1


# --- цена решения и статистика поиска (PLAN 6.5, блок 43) --------------------


def test_objective_matches_the_cost_of_the_rebuilt_schedule(
    make_request, make_engineer, make_travel, make_dataset
):
    """Цель солвера сходится с `cost` по расписанию `build_schedule`.

    Транзиты лежат в матрицах по номерам узлов, а routing-индексы в этой модели не совпадают
    с номерами ни в одном узле: финиш 0 стоит только концом маршрута. Матрица, прочитанная
    по индексам, сдвинула бы плечи на узел, и цена разошлась бы с пересчётом. Плечи разные и
    несимметричные, а три заявки на весь день на две бригады не помещаются — в цели есть и
    штраф дизъюнкции.
    """
    requests = [
        make_request("1", windowEnd="18:00"),
        make_request("2", windowEnd="18:00"),
        make_request("3", windowStart="13:00", windowEnd="17:00"),
        *(make_request(f"day-{number}", **WHOLE_DAY) for number in (1, 2, 3)),
    ]
    engineers = [make_engineer("brigade-1"), make_engineer("brigade-2")]
    data = make_dataset(requests, engineers)
    travel = make_travel(
        requests,
        legs={
            ("office", "1"): (300, 2000),
            ("office", "2"): (900, 11000),
            ("1", "2"): (600, 4000),
            ("2", "1"): (1200, 9000),
            ("2", "3"): (300, 1500),
            ("3", "2"): (900, 7000),
            ("1", "3"): (1500, 13000),
        },
    )
    weights = weights_for(data, travel)

    solved = optimizer.solve(
        data,
        travel,
        candidates.allowed(data, travel),
        weights,
        1,
        solution_limit=SOLUTIONS,
    )

    positions = start_positions(data)
    driven = sum(
        meters(
            build_schedule(
                engineer, solved.routes[engineer.id], travel, positions[engineer.id]
            )
        )
        for engineer in engineers
    )
    assert solved.unassigned  # штраф дизъюнкции входит в цель
    assert solved.stats.objective == optimizer.cost(
        solved.routes, solved.unassigned, driven, weights, engineers
    )


def test_search_reports_how_it_went(one_or_two):
    """Статистика поиска: сколько решений, чем остановлен, сходимость, тёплый старт."""
    data, travel = one_or_two
    allowed = candidates.allowed(data, travel)
    weights = weights_for(data, travel)

    # Предел, которого секунда не достигнет: проверяется и остановка по времени.
    endless = 10**9
    guided = optimizer.solve(
        data, travel, allowed, weights, 1, solution_limit=endless
    ).stats
    first = optimizer.solve(
        data, travel, allowed, weights, 1, guided=False, solution_limit=endless
    ).stats
    warm = optimizer.solve(
        data,
        travel,
        allowed,
        weights,
        1,
        initial={"brigade-1": ["r1", "r2"]},
        guided=False,
        solution_limit=endless,
    ).stats

    # GLS принимает и худшие решения, а кривая хранит только улучшения.
    assert guided.stopped_by == "time" and guided.solutions > 1
    objectives = [objective for _, objective in guided.trace]
    assert objectives == sorted(set(objectives), reverse=True)
    assert objectives[-1] == guided.objective
    assert first.stopped_by == "solutions" and first.solutions == 1
    assert not guided.warm_started and warm.warm_started


def test_solver_model_is_freed_after_the_search(one_or_two):
    """Колбэк решения не держит модель: C++ хранит ссылку на колбэк, и цикл через `routing`
    сборщик мусора не видит — каждый расчёт оставлял бы свою модель в памяти навсегда."""
    data, travel = one_or_two

    def alive() -> int:
        gc.collect()
        # `type`, а не `isinstance`: тот разыменовывает weakproxy, и мёртвый прокси из
        # соседнего теста роняет перебор `ReferenceError`.
        return sum(type(item) is pywrapcp.RoutingModel for item in gc.get_objects())

    before = alive()
    optimizer.solve(
        data,
        travel,
        candidates.allowed(data, travel),
        weights_for(data, travel),
        1,
        guided=False,
        solution_limit=SOLUTIONS,
    )

    assert alive() == before


def test_convergence_curve_is_thinned_to_fifty_points():
    """Кривая сходимости не длиннее 50 точек, первая и последняя на месте."""
    points = [(second / 10, 1000 - second) for second in range(500)]

    thinned = optimizer._thin(points)

    assert len(thinned) == 50
    assert thinned[0] == points[0] and thinned[-1] == points[-1]
    assert optimizer._thin(points[:7]) == points[:7]


# --- досчёт и выбор лучшего (PLAN 6.6) ---------------------------------------


def test_repair_adds_what_the_solver_left_out(
    make_request, make_engineer, make_travel, make_dataset, monkeypatch
):
    """Досчёт вставкой обязан добрать заявку, которую солвер не взял (PLAN 6.6, п. 1)."""
    requests = [make_request("1"), make_request("2")]
    data = make_dataset(requests, [make_engineer("brigade-1")])
    travel = make_travel(requests)
    monkeypatch.setattr(
        optimizer,
        "solve",
        lambda *_, **__: optimizer.Solved(
            {"brigade-1": [requests[0]]}, [requests[1]], None
        ),
    )

    plan = service.build_plan(data, travel, Algorithm.OPTIMIZED)

    assert plan.metrics.assigned_count == 2
    assert not plan.fallback_to_baseline


def test_repair_does_not_wake_an_idle_engineer_to_save_kilometres(
    make_request, make_engineer, make_travel, make_dataset
):
    """Досчёт выбирает бригаду по цели, а не по километрам (PLAN 6.5, 6.6).

    `best_insertion` знает только километры, а километры младше бригад во всех режимах.
    Вставка «где ближе» подняла бы простаивающую бригаду ради 5 км вместо 30 и сделала
    бы план хуже по собственной цели проекта — вплоть до проигрыша базовому варианту.
    """
    requests = [make_request("1"), make_request("2")]
    data = make_dataset(
        requests, [make_engineer("brigade-1"), make_engineer("brigade-2")]
    )
    travel = make_travel(
        requests, legs={("1", "2"): (600, 30000), ("2", "1"): (600, 30000)}
    )
    routes = {"brigade-1": [requests[0]], "brigade-2": []}

    rest = repair.fill(data, routes, [requests[1]], travel, weights_for(data, travel))

    assert rest == []
    # Позиция внутри маршрута здесь не важна — оба порядка дают те же 35 км; важно,
    # что заявка досталась занятой бригаде, а простаивающая так и осталась без работы.
    assert {request.id for request in routes["brigade-1"]} == {"1", "2"}
    assert routes["brigade-2"] == []


@pytest.mark.parametrize(
    ("mode", "placed"), [("emergency_first", "emergency"), ("count_first", "urgent")]
)
def test_repair_order_follows_the_objective(
    mode, placed, make_request, make_engineer, make_travel, make_dataset
):
    """Досчёт разбирает заявки в порядке цели (PLAN 6.6): место одно, и получает его та,
    чьё неназначение дороже. В `emergency_first` это несрочная авария, хотя срочная
    обычная заявка стоит в списке раньше; в `count_first` — срочная."""
    requests = [
        make_request("urgent", urgent=True, **WHOLE_DAY),
        make_request(
            "emergency", skill="emergency", workPriority="emergency", **WHOLE_DAY
        ),
    ]
    data = make_dataset(
        requests, [make_engineer("brigade-1", skills=["local", "emergency"])]
    )
    travel = make_travel(requests)
    routes = {"brigade-1": []}

    rest = repair.fill(data, routes, requests, travel, weights_for(data, travel, mode))

    assert [request.id for request in routes["brigade-1"]] == [placed]
    assert len(rest) == 1


def test_plan_falls_back_to_baseline_when_optimization_loses(
    make_request, make_engineer, make_travel, make_dataset, monkeypatch
):
    """Хуже базового — возвращаем базовый и говорим об этом вслух (PLAN 6.6, п. 2).

    Обе заявки помещаются в один маршрут, и базовый вариант так и делает; подменённый солвер
    разводит их по двум бригадам. Заявок поровну, поэтому решает вторый критерий — бригады.
    Досчёт такое не исправляет: он только добавляет неназначенные, но не переносит чужие.
    """
    requests = [make_request("1"), make_request("2")]
    engineers = [make_engineer("brigade-1"), make_engineer("brigade-2")]
    data = make_dataset(requests, engineers)
    monkeypatch.setattr(
        optimizer,
        "solve",
        lambda *_, **__: optimizer.Solved(
            {"brigade-1": [requests[0]], "brigade-2": [requests[1]]}, [], None
        ),
    )

    plan = service.build_plan(data, make_travel(requests), Algorithm.OPTIMIZED)

    assert plan.fallback_to_baseline
    assert plan.metrics.engineers_used == 1
    assert plan.validation.ok


def test_plan_falls_back_to_baseline_when_there_is_no_solution(
    make_request, make_engineer, make_travel, make_dataset, monkeypatch
):
    requests = [make_request("1")]
    data = make_dataset(requests, [make_engineer("brigade-1")])
    monkeypatch.setattr(optimizer, "solve", lambda *_, **__: None)

    plan = service.build_plan(data, make_travel(requests), Algorithm.OPTIMIZED)

    assert plan.fallback_to_baseline
    assert plan.metrics.assigned_count == 1


# --- встроенный набор целиком ------------------------------------------------


@pytest.fixture
def builtin():
    """Встроенный набор на готовых матрицах: ни Mongo, ни Valhalla."""
    dataset = seed.load_builtin()
    travel = matrices.Travel(
        dataset.id,
        matrices.points(dataset),
        {
            document["profile"]: matrices.from_document(document)
            for document in seed.load_matrices()
        },
    )
    return dataset, travel


def test_search_with_a_solution_limit_repeats_itself(builtin):
    """Предел решений делает поиск повторяемым (PLAN 6.5): два прогона на одном наборе
    проходят одну траекторию и дают одни маршруты. Сверяются прогоны между собой, а не с
    записанным маршрутом: какой он, решает поиск, а не тест."""
    data, travel = builtin
    allowed = candidates.allowed(data, travel)
    weights = weights_for(data, travel, "emergency_first")

    first, second = (
        # Лимит времени с запасом: остановить поиск должен предел, а не часы.
        optimizer.solve(data, travel, allowed, weights, 60, solution_limit=100)
        for _ in range(2)
    )

    assert first.stats.stopped_by == "solutions" and first.stats.solutions == 100
    assert first.stats.objective == second.stats.objective
    assert {key: [r.id for r in route] for key, route in first.routes.items()} == {
        key: [r.id for r in route] for key, route in second.routes.items()
    }
    assert [r.id for r in first.unassigned] == [r.id for r in second.unassigned]


def test_optimized_builtin_dataset_is_valid_and_better_than_baseline(
    builtin, monkeypatch
):
    """66 заявок и 12 бригад на готовых матрицах.

    Глобальный оптимум эвристический поиск не обещает, поэтому проверяем только то, что
    план сходится сам с собой и что экономия бригад и километров не потеряла ни одной
    заявки против базового варианта.
    """
    monkeypatch.setattr(settings, "solver_solution_limit", 200)
    dataset, travel = builtin

    base = service.build_plan(dataset, travel, Algorithm.BASELINE)
    plan = service.build_plan(dataset, travel, Algorithm.OPTIMIZED)

    assert plan.validation.errors == []
    # «Не хуже базового» — это свойство сравнения планов, а не удачи поиска: при проигрыше
    # вернулся бы базовый. Насколько именно лучше — зависит от предела поиска, поэтому
    # конкретных чисел здесь нет: они измеряются и записаны в пояснительной записке.
    assert plan.metrics.assigned_count >= base.metrics.assigned_count
    assert plan.metrics.unassigned_count <= base.metrics.unassigned_count
    assert plan.metrics.assigned_count + plan.metrics.unassigned_count == len(
        dataset.requests
    )


# --- вместимость оборудования (PLAN 6.5, блок 27) ----------------------------


def test_pedestrian_takes_no_more_equipment_than_it_carries(
    make_request, make_engineer, make_dataset, make_travel
):
    tasks = [
        make_request(str(number), requiredEquipment=["router"], windowEnd="18:00")
        for number in (1, 2, 3)
    ]
    walker = make_engineer(
        "brigade-1", transport="public_transport", equipment=["router"]
    )
    data = make_dataset(tasks, [walker])

    routes, unassigned = solve(data, make_travel(tasks))

    assert len(routes["brigade-1"]) == 2
    assert len(unassigned) == 1


def test_capacity_counts_what_the_committed_part_spent(
    make_request, make_engineer, make_dataset, make_travel
):
    """Одна единица уже унесена начатой работой — остаток дня берёт ещё одну заявку."""
    tasks = [
        make_request(str(number), requiredEquipment=["router"], windowEnd="18:00")
        for number in (1, 2)
    ]
    walker = make_engineer(
        "brigade-1", transport="public_transport", equipment=["router"]
    )
    data = make_dataset(tasks, [walker])

    routes, _ = solve(
        data,
        make_travel(tasks),
        positions={
            "brigade-1": Position(
                matrices.OFFICE,
                None,
                equipment_used=1,
                duration_mode=DurationMode.NORM,
                finished={},
                turns={},
                lunch_done=True,
                after_work=False,
            )
        },
    )

    assert len(routes["brigade-1"]) == 1


def test_car_takes_what_the_pedestrian_cannot(
    make_request, make_engineer, make_dataset, make_travel
):
    tasks = [
        make_request(str(number), requiredEquipment=["router"], windowEnd="18:00")
        for number in (1, 2, 3)
    ]
    brigades = [
        make_engineer("brigade-1", transport="public_transport", equipment=["router"]),
        make_engineer("brigade-2", equipment=["router"]),
    ]
    data = make_dataset(tasks, brigades)

    routes, unassigned = solve(data, make_travel(tasks))

    assert served(routes) == {"1", "2", "3"}
    assert not unassigned
    assert len(routes["brigade-1"]) <= 2


# --- «Рейтинг в приоритете» (блок 71) ----------------------------------------


def rated(stars: float, count: int = 40) -> dict:
    return {"rating": {"stars": stars, "count": count}}


@pytest.mark.parametrize(
    ("rating", "cost"),
    [
        (None, 50),  # без отзывов — ровно 4,5
        ({"stars": 4.9, "count": 40}, 14),  # (490·40 + 450·5) / 45 = 485,6 → 486
        ({"stars": 5.0, "count": 1}, 42),  # одна пятёрка лучшим не делает: 458
        ({"stars": 1.0, "count": 3}, 181),  # (100·3 + 450·5) / 8 = 318,75 → 319
    ],
)
def test_rating_cost_is_smoothed_towards_the_prior(make_engineer, rating, cost):
    """`500 − рейтинг×100`, рейтинг — средняя с 5 априорными оценками по 4,5, в сотых."""
    assert optimizer.rating_cost(make_engineer(rating=rating)) == cost


def test_reserve_without_rating_costs_the_worst(make_engineer):
    """Резервист без отзывов — по худшей оценке: иначе типовой универсал на равных со
    своими новичками забирал бы их работу, как только так выходит меньше бригад."""
    engineer = make_engineer("reserve-1")

    assert optimizer.rating_cost(engineer.model_copy(update={"reserve": True})) == 400
    assert optimizer.rating_cost(engineer) == 50


@pytest.fixture
def two_raters(make_request, make_engineer, make_travel, make_dataset):
    """Одна заявка, две бригады с одинаковой дорогой; лучшая по рейтингу — вторая по входу."""
    requests = [make_request("1", windowEnd="17:00")]
    engineers = [
        make_engineer("brigade-1", **rated(3.9)),
        make_engineer("brigade-2", **rated(4.8)),
    ]
    return make_dataset(requests, engineers), make_travel(requests)


def test_rating_first_gives_the_request_to_the_better_rated_engineer(two_raters):
    routes, unassigned = solve(*two_raters, mode="rating_first")

    assert unassigned == []
    assert busy(routes) == ["brigade-2"]


@pytest.fixture
def rating_or_engineers(make_request, make_engineer, make_travel, make_dataset):
    """Слабая по рейтингу бригада берёт обе заявки, сильная — только первую: навыка
    подключения у неё нет. Рейтинг против экономии бригады."""
    requests = [
        make_request("local", windowEnd="17:00"),
        make_request("connection", skill="connection", windowEnd="17:00"),
    ]
    engineers = [
        make_engineer("brigade-1", skills=["local", "connection"], **rated(3.9)),
        make_engineer("brigade-2", skills=["local"], **rated(4.9)),
    ]
    return make_dataset(requests, engineers), make_travel(requests)


def test_rating_first_spends_an_engineer_for_the_better_rating(rating_or_engineers):
    routes, _ = solve(*rating_or_engineers, mode="rating_first")

    assert [request.id for request in routes["brigade-2"]] == ["local"]
    assert [request.id for request in routes["brigade-1"]] == ["connection"]


def test_emergency_first_saves_the_engineer_instead(rating_or_engineers):
    """Тот же набор, другой порядок критериев: бригада старше рейтинга — работает одна."""
    routes, _ = solve(*rating_or_engineers, mode="emergency_first")

    assert busy(routes) == ["brigade-1"]


def test_repair_gives_the_request_to_the_better_rated_engineer(two_raters):
    """Досчёт ставит заявку той же ценой, что солвер: штраф рейтинга входит в цену места."""
    data, travel = two_raters
    routes = {engineer.id: [] for engineer in data.engineers}

    left = repair.fill(
        data, routes, data.requests, travel, weights_for(data, travel, "rating_first")
    )

    assert left == []
    assert busy(routes) == ["brigade-2"]


def test_objective_counts_the_rating_like_cost(
    make_request, make_engineer, make_travel, make_dataset
):
    """Штраф рейтинга сидит в стоимости дуг модели, а `cost` считает его по маршрутам:
    одна формула, иначе выбор лучшего (PLAN 6.6) сравнивал бы не то, что искал солвер."""
    requests = [
        make_request("1", windowEnd="18:00"),
        make_request("2", windowStart="12:00", windowEnd="18:00"),
        *(make_request(f"day-{number}", **WHOLE_DAY) for number in (1, 2, 3)),
    ]
    engineers = [
        make_engineer("brigade-1", **rated(4.2, 7)),
        make_engineer("brigade-2", **rated(4.7, 30)),
    ]
    data = make_dataset(requests, engineers)
    travel = make_travel(requests, legs={("office", "2"): (900, 11000)})
    weights = weights_for(data, travel, "rating_first")

    solved = optimizer.solve(
        data,
        travel,
        candidates.allowed(data, travel),
        weights,
        1,
        solution_limit=SOLUTIONS,
    )

    positions = start_positions(data)
    driven = sum(
        meters(build_schedule(e, solved.routes[e.id], travel, positions[e.id]))
        for e in engineers
    )
    assert all(solved.routes.values())  # штраф рейтинга есть у обеих бригад
    assert solved.unassigned  # и штраф дизъюнкции
    assert solved.stats.objective == optimizer.cost(
        solved.routes, solved.unassigned, driven, weights, engineers
    )


def test_reserve_without_rating_takes_work_last(
    make_request, make_engineer, make_travel, make_dataset
):
    """Резерв загруженного набора — типовые универсалы без отзывов. Один такой мог бы
    сделать всё и сэкономить бригаду, но берёт только то, что свои не могут: здесь — аварию,
    на которую у бригады набора нет навыка."""
    requests = [
        make_request("local-1", windowEnd="17:00"),
        make_request("local-2", windowEnd="17:00"),
        make_request(
            "emergency",
            skill="emergency",
            workPriority="emergency",
            requiredTransport="car",
            windowEnd="17:00",
        ),
    ]
    dataset = make_dataset(requests, [make_engineer("brigade-1")])
    day = service.staffed(dataset, "rating_first")
    reserve = day.engineers[1:]

    assert len(day.engineers) == seed.CREW_POOL
    assert all(engineer.reserve and engineer.rating is None for engineer in reserve)
    assert service.staffed(dataset, "emergency_first") is dataset

    routes, unassigned = solve(day, make_travel(requests), mode="rating_first")

    assert unassigned == []
    assert served({"own": routes["brigade-1"]}) == {"local-1", "local-2"}
    (helper,) = [
        engineer_id for engineer_id in busy(routes) if engineer_id != "brigade-1"
    ]
    assert [request.id for request in routes[helper]] == ["emergency"]


def test_builtin_reserve_is_the_rest_of_the_crew_list(builtin):
    """Резерв встроенного набора — люди `crews.json` вне набора, из офиса, после своих."""
    dataset, _ = builtin
    day = service.staffed(dataset, "rating_first")
    own = len(dataset.engineers)

    assert len(day.engineers) == seed.CREW_POOL
    assert day.engineers[:own] == dataset.engineers
    assert [engineer.id for engineer in day.engineers[own:]] == [
        raw["id"] for raw in seed.load_crew_pool()[own:]
    ]
    assert all(
        engineer.reserve
        and (engineer.start_lat, engineer.start_lon)
        == (dataset.office.lat, dataset.office.lon)
        for engineer in day.engineers[own:]
    )


@pytest.fixture
def rated_builtin(builtin):
    """Встроенный набор с резервом и рейтингом демо-истории — без Mongo."""
    from scripts import measure

    dataset, travel = builtin
    return measure.rated(service.staffed(dataset, "rating_first")), travel


def test_rating_first_repeats_itself_on_the_builtin_dataset(rated_builtin):
    """Сорок бригад с матрицей стоимости на каждую: предел решений держит повторяемость."""
    data, travel = rated_builtin
    allowed = candidates.allowed(data, travel)
    weights = weights_for(data, travel, "rating_first")

    first, second = (
        optimizer.solve(data, travel, allowed, weights, 60, solution_limit=100)
        for _ in range(2)
    )

    assert first.stats.stopped_by == "solutions"
    assert first.stats.objective == second.stats.objective
    assert {key: [r.id for r in route] for key, route in first.routes.items()} == {
        key: [r.id for r in route] for key, route in second.routes.items()
    }


def test_rating_first_plan_is_valid_and_prefers_the_better_rated(rated_builtin):
    """План варианта проходит проверку, берёт людей из резерва и отдаёт работу лучшим:
    средний рейтинг исполнителя выше, чем у основной модели на своих людях."""
    data, travel = rated_builtin
    own = data.model_copy(
        update={"engineers": [e for e in data.engineers if not e.reserve]}
    )

    def run(dataset, variant):
        setup = variants.SETUPS[variant]
        return service.build_plan(
            dataset, travel, setup.algorithm, setup=replace(setup, solution_limit=200)
        )

    plan = run(data, Variant.RATING_FIRST)
    main = run(own, Variant.EMERGENCY_FIRST)

    assert plan.validation.errors == []
    reserve = {e.id for e in plan.input.engineers if e.reserve}
    assert any(route.stops for route in plan.routes if route.engineer_id in reserve)
    assert plan.metrics.assigned_count >= main.metrics.assigned_count
    assert plan.metrics.rating_avg > main.metrics.rating_avg
