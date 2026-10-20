"""Сборка плана, метрики и сохранение (PLAN 5.2, 6.10, блок 7.5).

Метрики, времена и километры отдельными тестами не пересчитываются: это делает валидатор
(PLAN 6.7) своим кодом, поэтому «план прошёл проверку» и есть проверка метрик. Отдельно
проверяется то, чего валидатор не сверяет: загрузка смены, состав маршрутов и заглушка причины.
"""

import pytest

from app import db
from app.config import settings
from app.data import seed
from app.models import Algorithm, CancelRequestEvent, ReplanMode, Variant
from app.planning import optimizer, service
from app.routing import matrices, valhalla
from app.timeutil import to_minutes, to_seconds


@pytest.fixture
def parts(make_request, make_engineer, make_travel, make_dataset):
    """Две бригады и три заявки: «3» не назначается никому — нет навыка.

    Бригада 2 остаётся без работы: базовый вариант отдаёт всё первой подходящей.
    Заявка «3» ещё и срочная авария — на ней проверяется разбивка метрик (PLAN 6.10).
    """
    requests = [
        make_request("1"),
        make_request("2", windowStart="11:00", windowEnd="13:00"),
        make_request("3", skill="emergency", workPriority="emergency", urgent=True),
    ]
    engineers = [make_engineer("brigade-1"), make_engineer("brigade-2")]
    return make_dataset(requests, engineers), make_travel(requests)


@pytest.fixture
def plan(parts):
    return service.build_plan(*parts)


# --- план целиком ------------------------------------------------------------


def test_built_plan_passes_the_validator(plan):
    """Независимая проверка сверяет времена, километры и все метрики (PLAN 6.7)."""
    assert plan.validation.errors == []
    assert plan.validation.ok


def test_routes_hold_every_engineer_including_the_idle_one(plan):
    assert [route.engineer_id for route in plan.routes] == ["brigade-1", "brigade-2"]
    idle = plan.routes[1]
    assert idle.stops == [] and idle.km == 0
    assert plan.metrics.engineers_used == 1
    assert plan.metrics.km_by_engineer["brigade-2"] == 0
    assert plan.metrics.utilization_by_engineer["brigade-2"] == 0


def test_metrics_split_requests_by_type_and_urgency(plan):
    """PLAN 6.10: назначено и не назначено — в том числе по типам работ и по срочности."""
    metrics = plan.metrics
    assert metrics.assigned_by_work_priority == {
        "emergency": 0,
        "new_connection": 0,
        "regular": 2,
    }
    assert metrics.unassigned_by_work_priority == {
        "emergency": 1,
        "new_connection": 0,
        "regular": 0,
    }
    assert (metrics.assigned_urgent, metrics.unassigned_urgent) == (0, 1)


def test_stops_follow_the_route_order(plan):
    assert [stop.request_id for stop in plan.routes[0].stops] == ["1", "2"]
    assert plan.routes[0].stops[0].start == "09:10"
    # Окно заявки «2» открывается в 11:00 — раньше работу не начинаем, ждём (PLAN 6.2).
    assert plan.routes[0].stops[1].arrival == "10:20"
    assert plan.routes[0].stops[1].start == "11:00"


def test_assignments_cover_every_request(plan):
    assert plan.assignments == {"1": "brigade-1", "2": "brigade-1", "3": None}


def test_unassigned_carries_its_own_reason(plan):
    # Коды, тексты и таблица кандидатов — блок 9 (PLAN 6.8, 6.9); здесь важно, что сборка
    # плана их вызывает, а не то, как они устроены: это проверяет test_explain.py.
    assert [item.request_id for item in plan.unassigned] == ["3"]
    assert plan.unassigned[0].reason_code == "NO_SKILL"
    assert plan.unassigned[0].defer_next_day
    assert set(plan.explanations) == {"1", "2", "3"}
    assert plan.route_explanations.keys() == {"brigade-1", "brigade-2"}


def test_utilization_counts_travel_and_work_without_waiting(parts, make_request):
    """Одна заявка: 10 минут переезда и 60 минут работы при смене 9 часов — 4200 / 32400."""
    dataset, travel = parts
    dataset.requests = dataset.requests[:1]
    assert service.build_plan(dataset, travel).metrics.utilization_by_engineer == {
        "brigade-1": round(4200 / 32400, 2),
        "brigade-2": 0,
    }


def test_travel_wait_and_work_match_the_schedule(plan):
    """Три метрики времени сходятся с временами стопов плана (PLAN 6.15, блок 18.5).

    Валидатор их уже пересчитывает своим кодом (6.7), но по своим таблицам; здесь сверка
    идёт по самому плану — так виден и состав дня: два переезда по 10 минут, ожидание окна
    заявки «2» с 10:20 до 11:00 и две работы по часу. Плечи фикстуры кратны минуте,
    поэтому округление (6.7) в сверку не вмешивается.
    """
    stops = [stop for route in plan.routes for stop in route.stops]

    def spans(begin: str, end: str) -> int:
        return sum(
            to_seconds(getattr(stop, end)) - to_seconds(getattr(stop, begin))
            for stop in stops
        )

    metrics = plan.metrics
    assert (metrics.travel_min, metrics.wait_min, metrics.work_min) == (20, 40, 120)
    assert metrics.travel_min == to_minutes(spans("departure", "arrival"))
    assert metrics.wait_min == to_minutes(spans("arrival", "start"))
    assert metrics.work_min == to_minutes(spans("start", "end"))


def test_geometry_is_empty_until_block_10(plan):
    assert all(route.geometry == [] for route in plan.routes)


def test_approximate_comes_from_the_travel_tables(parts):
    """Флаг и оповещение к нему: обе бригады на автомобиле, значит план приблизителен весь."""
    dataset, travel = parts
    travel.approximate_profiles.add("auto")

    plan = service.build_plan(dataset, travel)

    assert plan.approximate
    assert plan.approximate_note == (
        "Valhalla не ответила, расстояния посчитаны по прямой (× 1.3 и городская "
        "скорость): километры и времена всего плана приблизительные."
    )


def test_plan_without_straight_lines_has_no_note(plan):
    assert plan.approximate is False and plan.approximate_note is None


def test_manual_assignment_needs_ready_routes(parts):
    """Посчитать базовый вариант вместо запрошенного молча нельзя (PLAN 6.6).

    У ручного переназначения алгоритма нет: распределение задал диспетчер (PLAN 6.16),
    и без готовых маршрутов считать нечего — подменять их базовым вариантом запрещено.
    """
    with pytest.raises(ValueError, match="готовым маршрутам"):
        service.build_plan(*parts, algorithm=Algorithm.MANUAL)


def test_every_plan_gets_its_own_id(parts):
    assert service.build_plan(*parts).id != service.build_plan(*parts).id


# --- встроенный набор целиком ------------------------------------------------


def test_builtin_dataset_is_planned_and_valid():
    """66 заявок и 10 бригад на готовых матрицах: ни Mongo, ни Valhalla не нужны (PLAN 5.7)."""
    dataset = seed.load_builtin()
    documents = seed.load_matrices()
    assert documents, (
        "нет data/seed/matrices.json — запустите prepare_data.py --matrices"
    )
    travel = matrices.Travel(
        dataset.id,
        matrices.points(dataset),
        {
            document["profile"]: matrices.from_document(document)
            for document in documents
        },
    )

    plan = service.build_plan(dataset, travel)

    assert plan.validation.errors == []
    assert not plan.approximate
    assert plan.metrics.assigned_count + plan.metrics.unassigned_count == len(
        dataset.requests
    )
    assert len(plan.routes) == len(dataset.engineers)


# --- сохранение --------------------------------------------------------------


def test_saved_plan_is_read_back_as_it_was(mongo, plan):
    db.plans.insert_one(service.to_document(plan))
    stored = service.get_plan(plan.id)

    assert stored is not None
    assert stored.model_dump(exclude={"created_at"}) == plan.model_dump(
        exclude={"created_at"}
    )


def test_unknown_plan_is_not_found(mongo):
    assert service.get_plan("нет такого") is None


def test_create_plan_saves_a_valid_plan_with_route_lines(mongo, parts, monkeypatch):
    """Линии рисуются до сохранения, иначе прочитанный план остался бы без карты (блок 10)."""
    dataset, travel = parts
    monkeypatch.setattr(matrices, "get", lambda _: travel)
    # Valhalla в тестах не вызывается: линия приходит из подмены, а не из сети.
    monkeypatch.setattr(valhalla, "route", lambda locations, costing: [list(locations)])

    plan = service.create_plan(dataset)

    assert plan.validation.ok
    stored = service.get_plan(plan.id)
    assert stored is not None
    assert stored.routes[0].geometry == plan.routes[0].geometry != []


# --- кэш решений (PLAN 6.5, блок 45) ------------------------------------------


@pytest.fixture
def solves(mongo, parts, monkeypatch):
    """Сколько раз звали солвер: кэш проверяется тем, что второй расчёт его не зовёт.

    Таблицы и линии маршрутов — из подмен, как у `create_plan` выше. Предел решений
    маленький: пример очевидный, и поиск останавливает предел, а не часы.
    """
    _, travel = parts
    monkeypatch.setattr(matrices, "get", lambda _: travel)
    monkeypatch.setattr(valhalla, "route", lambda locations, costing: [list(locations)])
    monkeypatch.setattr(settings, "solver_solution_limit", 50)
    monkeypatch.setattr(settings, "replan_solution_limit", 50)
    calls = []
    original = optimizer.solve

    def spy(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(optimizer, "solve", spy)
    return calls


def test_repeated_calculation_takes_the_solution_from_the_cache(parts, solves):
    """Тот же вход — то же решение без солвера, а план собирается заново: свой `id`,
    свежие объяснения и проверка (PLAN 6.5)."""
    dataset, _ = parts

    first = service.create_plan(dataset, Algorithm.OPTIMIZED)
    second = service.create_plan(dataset, Algorithm.OPTIMIZED)

    assert len(solves) == 1
    assert db.solutions.count_documents({}) == 1
    assert second.id != first.id and second.validation.ok
    assert second.routes == first.routes
    assert second.unassigned == first.unassigned
    assert second.metrics == first.metrics
    assert second.search == first.search
    assert second.fallback_to_baseline == first.fallback_to_baseline


def test_changed_input_calls_the_solver_again(parts, solves, make_travel, monkeypatch):
    """Отпечаток берёт всё, от чего зависит решение: правило окна, вместимость
    оборудования и сами таблицы переездов, а не только их точки."""
    dataset, _ = parts
    service.create_plan(dataset, Algorithm.OPTIMIZED)

    monkeypatch.setattr(settings, "window_rule", "fit_in_window")
    service.create_plan(dataset, Algorithm.OPTIMIZED)
    assert len(solves) == 2

    monkeypatch.setattr(settings, "equipment_capacity", {})
    service.create_plan(dataset, Algorithm.OPTIMIZED)
    assert len(solves) == 3

    # Те же точки, другие плечи — так выглядит таблица, посчитанная по прямой.
    slower = make_travel(dataset.requests, leg=(900, 7000))
    monkeypatch.setattr(matrices, "get", lambda _: slower)
    service.create_plan(dataset, Algorithm.OPTIMIZED)
    assert len(solves) == 4


def test_variant_does_not_share_a_solution_with_the_main_calculation(parts, solves):
    """У варианта нет подмены базовым (PLAN 6.14), поэтому при том же порядке критериев
    его результат может отличаться от первичного расчёта, и ключи у них разные."""
    dataset, _ = parts

    service.create_plan(dataset, Algorithm.OPTIMIZED)
    service.compare(dataset, [Variant.EMERGENCY_FIRST])
    service.compare(dataset, [Variant.EMERGENCY_FIRST])

    assert len(solves) == 2


def test_warm_up_leaves_nothing_for_compare_to_solve(parts, solves, monkeypatch):
    """Прогрев считает ровно то, что потом попросит сравнение всех вариантов: с другим
    отпечатком кэш остался бы холодным, и демо-пара снова ждала бы поиска."""
    dataset, _ = parts
    monkeypatch.setattr(seed, "DEMO_REQUESTS", {"vostok": "-"})
    monkeypatch.setattr(seed, "DEMO_CREWS", {"staff": 2})
    monkeypatch.setattr(seed, "demo_dataset", lambda *_: dataset)

    service.warm_up()
    warmed = len(solves)
    service.compare(dataset, list(Variant))

    assert warmed > 0
    assert len(solves) == warmed


def test_search_stopped_by_time_is_not_cached(parts, solves, monkeypatch):
    """Поиск, который остановили часы, не повторяем — хранить его нечего."""
    dataset, _ = parts
    monkeypatch.setattr(settings, "solver_solution_limit", 10**9)
    monkeypatch.setattr(settings, "solver_time_limit_sec", 1)

    plan = service.create_plan(dataset, Algorithm.OPTIMIZED)
    service.create_plan(dataset, Algorithm.OPTIMIZED)

    assert plan.search.stopped_by == "time"
    assert len(solves) == 2
    assert db.solutions.count_documents({}) == 0


def test_replan_is_not_cached(parts, solves):
    """Перепланирование не кэшируется: с пределом решений оно и так повторяемо."""
    dataset, _ = parts
    parent = service.create_plan(dataset, Algorithm.OPTIMIZED)
    event = CancelRequestEvent(type="cancel_request", time="08:30", requestIds=["2"])

    for _ in range(2):
        service.replan_plan(parent, dataset, event, ReplanMode.FROM_EVENT)

    assert len(solves) == 3
    assert db.solutions.count_documents({}) == 1


def test_cached_solutions_expire(mongo):
    """Кэш не растёт бесконечно: после правки кода прежние ключи никто не спросит."""
    db.ensure_indexes()

    ttl = [
        index
        for index in db.solutions.list_indexes()
        if index.get("expireAfterSeconds") is not None
    ]
    assert [index["key"] for index in ttl] == [{"createdAt": 1}]


def test_no_solution_is_not_cached(parts, solves, monkeypatch):
    """«Солвер не нашёл решения» значит одно — часы кончились раньше первого решения: у
    каждой заявки есть дизъюнкция, и пустое решение допустимо всегда. Повторять такой исход
    из кэша нельзя — на другой раз поиск успеет."""
    dataset, _ = parts
    monkeypatch.setattr(optimizer, "solve", lambda *_, **__: None)

    plan = service.create_plan(dataset, Algorithm.OPTIMIZED)

    assert plan.fallback_to_baseline
    assert db.solutions.count_documents({}) == 0
