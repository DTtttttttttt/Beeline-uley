"""Оптимизация OR-Tools: веса цели, порядок критериев, досчёт и выбор лучшего (блок 8.11).

Критерии проверяются на маленьких искусственных примерах с очевидным ответом и коротким
лимитом: эвристический поиск не обещает глобального оптимума, и тест не должен этого
утверждать. На встроенном наборе проверяется только то, что план валиден и не хуже базового.

Ни Valhalla, ни Mongo тестам не нужны: таблицы переездов собираются фикстурой из плеч.
"""

import pytest
from ortools.constraint_solver import pywrapcp

from app.config import Settings, settings
from app.data import seed
from app.models import Algorithm
from app.planning import candidates, optimizer, repair, service
from app.planning.schedule import Position
from app.routing import matrices
from app.routing.matrices import point_id

# Заявка на 8 часов: бригада, взявшая её, за день больше ничего не успевает.
WHOLE_DAY = {"serviceDurationMin": 480, "windowStart": "09:00", "windowEnd": "10:00"}


def weights_for(data, travel, mode="count_first"):
    return optimizer.make_weights(
        optimizer.ORDERS[mode], optimizer.bounds(data, travel)
    )


def solve(data, travel, mode="count_first", limit=2, positions=None, initial=None):
    """Решение солвера без досчёта и сравнения с базовым: проверяем саму цель.

    `positions` идёт и в отбор кандидатов: 6.3 требует отбирать от текущего состояния
    бригады, и тест, прогоняющий состояние только через модель, этого бы не заметил.
    """
    return optimizer.solve(
        data,
        travel,
        candidates.allowed(data, travel, positions),
        weights_for(data, travel, mode),
        limit,
        positions=positions,
        initial=initial,
    )


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
    routes, _ = optimizer.solve(
        data,
        travel,
        candidates.allowed(data, travel),
        weights_for(data, travel, "emergency_first"),
        1,
        guided=False,
    )

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
    positions = {"brigade-1": Position(point_id("done"), 17 * 3600, equipment_used=0)}

    routes, unassigned = solve(data, travel, positions=positions)

    assert served(routes) == {"short"}
    assert [request.id for request in unassigned] == ["long"]
    assert solve(data, travel)[1] == []  # без этого состояния успевают обе


# --- стартовое решение (PLAN 6.5, блок 8.7) ----------------------------------


@pytest.fixture
def spy_initial(monkeypatch):
    """Запоминает, что вернуло `ReadAssignmentFromRoutes`: `None` означает, что стартовое
    решение молча не прочиталось, и проверка «не упало» ничего бы не значила."""
    read = []
    original = pywrapcp.RoutingModel.ReadAssignmentFromRoutes

    def spy(self, routes, ignore_inactive):
        result = original(self, routes, ignore_inactive)
        read.append(result)
        return result

    monkeypatch.setattr(pywrapcp.RoutingModel, "ReadAssignmentFromRoutes", spy)
    return read


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


def test_impossible_initial_solution_does_not_break_the_search(
    make_request, make_engineer, make_travel, make_dataset, spy_initial
):
    """Прошлый план мог стать недопустимым — тогда считаем с нуля, а не падаем."""
    requests = [make_request("1", **WHOLE_DAY), make_request("2", **WHOLE_DAY)]
    data = make_dataset(requests, [make_engineer("brigade-1")])

    routes, unassigned = solve(
        data, make_travel(requests), initial={"brigade-1": ["1", "2"]}
    )

    assert spy_initial == [None]  # обе заявки на весь день в один маршрут не читаются
    assert len(served(routes)) == 1 and len(unassigned) == 1


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
        lambda *_, **__: ({"brigade-1": [requests[0]]}, [requests[1]]),
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
        lambda *_, **__: ({"brigade-1": [requests[0]], "brigade-2": [requests[1]]}, []),
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


def test_optimized_builtin_dataset_is_valid_and_better_than_baseline(monkeypatch):
    """66 заявок и 10 бригад на готовых матрицах.

    Глобальный оптимум эвристический поиск не обещает, поэтому проверяем только то, что
    план сходится сам с собой и что экономия бригад и километров не потеряла ни одной
    заявки против базового варианта.
    """
    monkeypatch.setattr(settings, "solver_time_limit_sec", 3)
    dataset = seed.load_builtin()
    travel = matrices.Travel(
        dataset.id,
        matrices.points(dataset),
        {
            document["profile"]: matrices.from_document(document)
            for document in seed.load_matrices()
        },
    )

    base = service.build_plan(dataset, travel, Algorithm.BASELINE)
    plan = service.build_plan(dataset, travel, Algorithm.OPTIMIZED)

    assert plan.validation.errors == []
    # «Не хуже базового» — это свойство сравнения планов, а не удачи поиска: при проигрыше
    # вернулся бы базовый. Насколько именно лучше — зависит от лимита и загрузки машины,
    # поэтому конкретных 53 из 66 здесь нет, они измеряются руками и записаны в README.
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
        positions={"brigade-1": Position(matrices.OFFICE, None, equipment_used=1)},
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
