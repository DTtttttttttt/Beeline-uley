"""Независимая проверка плана (PLAN 6.7, блок 6.3).

План собирается руками, с посчитанными на бумаге временами: смена 09:00–18:00, переезд
10 минут и 5 км, заявка «1» — 60 минут работы с 09:10, заявка «2» — окно с 11:00, значит
40 минут ожидания и 30 минут работы. Заявка «3» не назначена.

Валидатор обязан ругаться на каждую подделку — иначе «валидатор доволен» ничего не стоит.
"""

import ast
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.config import settings
from app.models import AddRequestEvent, Algorithm, Lunch, Plan, Rating, ReplanMode
from app.planning import service, validator


@pytest.fixture
def parts(make_request, make_engineer, make_travel):
    """Заявки, бригада и таблицы переездов правильного плана."""
    requests = [
        make_request("1"),
        make_request(
            "2", serviceDurationMin=30, windowStart="11:00", windowEnd="13:00"
        ),
        make_request("3", workPriority="emergency", urgent=True),
    ]
    engineer = make_engineer()
    return requests, engineer, make_travel(requests)


@pytest.fixture
def plan(parts):
    requests, engineer, _ = parts
    return Plan(
        id="plan-1",
        datasetId="test",
        algorithm="baseline",
        createdAt=datetime.now(UTC),
        input={
            "office": {"address": "офис", "lat": 55.70, "lon": 37.77},
            "requests": [r.model_dump(by_alias=True) for r in requests],
            "engineers": [engineer.model_dump(by_alias=True)],
        },
        routes=[
            {
                "engineerId": "brigade-1",
                "km": 10.0,
                "stops": [
                    {
                        "requestId": "1",
                        "departure": "09:00",
                        "arrival": "09:10",
                        "start": "09:10",
                        "end": "10:10",
                        "travelKm": 5.0,
                        "travelMin": 10,
                    },
                    {
                        "requestId": "2",
                        "departure": "10:10",
                        "arrival": "10:20",
                        "start": "11:00",
                        "end": "11:30",
                        "travelKm": 5.0,
                        "travelMin": 10,
                    },
                ],
            }
        ],
        assignments={"1": "brigade-1", "2": "brigade-1", "3": None},
        unassigned=[
            {
                "requestId": "3",
                "reasonCode": "NOT_FITTED",
                "reasonText": "не помещается",
            }
        ],
        metrics={
            "engineersUsed": 1,
            "totalKm": 10.0,
            "kmByEngineer": {"brigade-1": 10.0},
            "assignedCount": 2,
            "unassignedCount": 1,
            "assignedByWorkPriority": {
                "emergency": 0,
                "new_connection": 0,
                "regular": 2,
            },
            "unassignedByWorkPriority": {
                "emergency": 1,
                "new_connection": 0,
                "regular": 0,
            },
            "assignedUrgent": 0,
            "unassignedUrgent": 1,
            "closedByOutcome": {"done": 0, "cancelled": 0, "failed": 0},
            "travelMin": 20,
            "waitMin": 40,
            "workMin": 90,
            "emergencyWaitMin": 0,  # авария здесь не назначена — ждать некому
            "complexityExtraMin": 0,  # день по нормативу
            "objectsByLevel": {"simple": 3, "medium": 0, "complex": 0},
            "firstLegMin": 10,
        },
        complexity={
            request.id: {"complexity": 1.0, "level": "simple"} for request in requests
        },
    )


def check(plan, parts):
    return validator.validate(plan, parts[2])


def test_correct_plan_passes(plan, parts):
    result = check(plan, parts)
    assert result.errors == []
    assert result.ok


# --- состав (PLAN 6.7) -------------------------------------------------------


def test_duplicate_request_in_two_routes(plan, parts):
    plan.routes.append(
        plan.routes[0].model_copy(update={"engineer_id": "brigade-2", "km": 5.0})
    )
    assert _says(check(plan, parts), "встречается дважды")


def test_request_lost_between_routes_and_unassigned(plan, parts):
    plan.unassigned = []
    plan.assignments["3"] = None
    assert _says(check(plan, parts), "нет ни в маршрутах, ни в неназначенных")


def test_request_both_assigned_and_unassigned(plan, parts):
    plan.unassigned[0].request_id = "2"
    assert _says(check(plan, parts), "и числится неназначенной")


def test_assignments_disagree_with_routes(plan, parts):
    plan.assignments["1"] = "brigade-2"
    assert _says(check(plan, parts), "assignments не совпадает")


def test_unknown_engineer(plan, parts):
    plan.routes[0].engineer_id = "brigade-9"
    assert _says(check(plan, parts), "маршрут неизвестной бригады")


# --- ограничения -------------------------------------------------------------


def test_foreign_skill(plan, parts):
    plan.input.requests[0].skill = "emergency"
    assert _says(check(plan, parts), "нет навыка для заявки 1")


def test_missing_equipment(plan, parts):
    plan.input.requests[0].required_equipment = ["router"]
    assert _says(check(plan, parts), "нет оборудования для заявки 1")


def test_missing_tool(plan, parts):
    plan.input.requests[0].required_tools = ["laptop"]
    assert _says(check(plan, parts), "нет инструмента для заявки 1: laptop")


def test_window_is_broken(plan, parts):
    """Окно заявки сужено так, что записанное в плане начало в него не попадает."""
    plan.input.requests[0].window_end = "09:05"
    assert _says(check(plan, parts), "окно 09:00–09:05")


def test_shift_end_is_broken(plan, parts):
    plan.input.engineers[0].shift_end = "11:00"
    assert _says(check(plan, parts), "смена до 11:00")


def test_no_route_between_points(plan, parts, make_travel):
    requests, _, _ = parts
    travel = make_travel(requests, legs={("1", "2"): None})
    assert _says(validator.validate(plan, travel), "нет пути на этом транспорте")


# --- времена и метрики -------------------------------------------------------


def test_forged_arrival(plan, parts):
    """Время в плане подправлено руками — пересчёт обязан это увидеть."""
    plan.routes[0].stops[0].arrival = "09:05"
    assert _says(check(plan, parts), "прибытие 09:05, пересчёт даёт 09:10")


def test_forged_stop_distance(plan, parts):
    plan.routes[0].stops[1].travel_km = 0.5
    assert _says(check(plan, parts), "переезд, км")


def test_route_km_does_not_match(plan, parts):
    plan.routes[0].km = 7.5
    assert _says(check(plan, parts), "пробег 7.5 км, пересчёт даёт 10.0 км")


@pytest.mark.parametrize(
    ("field", "value", "text"),
    [
        ("wait_min", 0, "метрика waitMin"),
        ("work_min", 60, "метрика workMin"),
        ("total_km", 12.0, "метрика totalKm"),
        ("engineers_used", 2, "метрика engineersUsed"),
        ("assigned_count", 3, "метрика assignedCount"),
        ("assigned_urgent", 1, "метрика assignedUrgent"),
        ("unassigned_urgent", 0, "метрика unassignedUrgent"),
        ("emergency_wait_min", 5, "метрика emergencyWaitMin"),
        # План без метрики — не план до блока 49, а подделка: свежий план её всегда несёт.
        ("emergency_wait_min", None, "метрика emergencyWaitMin"),
    ],
)
def test_metrics_must_match(plan, parts, field, value, text):
    setattr(plan.metrics, field, value)
    assert _says(check(plan, parts), text)


def test_rating_metrics_are_recounted(plan, parts):
    """Рейтинг исполнителя (блок 71): средний по заявкам и сколько заявок у 4,5+."""
    plan.input.engineers[0].rating = Rating(stars=4.6, count=12)
    assert _says(
        check(plan, parts), "метрика ratingAvg: в плане None, пересчёт даёт 4.6"
    )

    plan.metrics.rating_avg, plan.metrics.top_rated_count = 4.6, 2
    assert check(plan, parts).errors == []

    plan.metrics.rating_avg = 4.9
    assert _says(check(plan, parts), "метрика ratingAvg")
    plan.metrics.rating_avg, plan.metrics.top_rated_count = 4.6, 0
    assert _says(check(plan, parts), "метрика topRatedCount")


def test_first_leg_is_recounted_from_the_engineer_start(plan, parts, make_travel):
    """Дорога до первой заявки (блок 73): подделанная цифра и плечо от дома ловятся."""
    plan.metrics.first_leg_min = 25
    assert _says(
        check(plan, parts), "метрика firstLegMin: в плане 25, пересчёт даёт 10"
    )

    # Тот же маршрут из дома: первое плечо — 25 минут от дома, а не 10 от офиса.
    requests, engineer, _ = parts
    home = engineer.model_copy(update={"start_lat": 55.80, "start_lon": 37.60})
    plan.input.engineers = [home]
    travel = make_travel(
        requests,
        engineers=[home],
        legs={("start:55.800000,37.600000", "1"): (1500, 9000)},
    )
    errors = validator.validate(plan, travel).errors
    assert not any("firstLegMin" in error for error in errors)


def test_unassigned_breakdown_must_match(plan, parts):
    """Тип неназначенной заявки в метрике подменён — пересчёт обязан это увидеть."""
    plan.metrics.unassigned_by_work_priority = {
        "emergency": 0,
        "new_connection": 1,
        "regular": 0,
    }
    assert _says(check(plan, parts), "метрика unassignedByWorkPriority")


# --- правило окна и независимость --------------------------------------------


def test_window_rule_changes_the_verdict(plan, parts, monkeypatch):
    """Один и тот же план, два вердикта.

    Заявка «1» начинается в 09:10 и работает 60 минут при окне 09:00–10:00: по ТЗ допустимо
    (в окно попало начало), по Дополнениям — нет, работа не влезает в окно целиком.
    """
    plan.input.requests[0].window_end = "10:00"
    assert check(plan, parts).ok

    monkeypatch.setattr(settings, "window_rule", "fit_in_window")
    assert _says(check(plan, parts), "окно 09:00–10:00 (fit_in_window)")


def test_validator_does_not_use_schedule():
    """Проверка обязана быть независимой: переиспользование schedule.py убивает её смысл."""
    source = Path(validator.__file__).read_text("utf-8")
    imported = {
        name.name if isinstance(node, ast.Import) else f"{node.module}.{name.name}"
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for name in node.names
    }
    assert not any("schedule" in name for name in imported)


def _says(result, text: str) -> bool:
    assert not result.ok, "валидатор не заметил подделку"
    return any(text in error for error in result.errors)


# --- досчёт ничего не пропустил (PLAN 6.6, 6.7, блок 52) ----------------------
#
# В маршруте фикстуры после заявки «2» день свободен до 18:00, и неназначенная авария «3»
# (60 мин, окно 09:00–12:00) встаёт в него на любое место: в базовом варианте это законно,
# у оптимизации — пропуск досчёта.


def _optimized(plan):
    plan.algorithm = Algorithm.OPTIMIZED
    return plan


def _event(parts, request_index: int, time: str = "09:00"):
    return AddRequestEvent(
        type="add_request", time=time, request=parts[0][request_index]
    )


def test_refill_skipped_request_is_caught(plan, parts):
    assert _says(
        check(_optimized(plan), parts),
        "заявка №3 встаёт к brigade-1 на место 1 — досчёт её пропустил",
    )


def test_refill_finds_a_place_in_the_middle(plan, parts):
    """Окно 10:20–10:30 достижимо только между «1» и «2»: перед «1» опоздает сама «1»
    (окно до 09:30), после «2» — авария."""
    plan.input.requests[0].window_end = "09:30"
    plan.input.requests[2].window_start = "10:20"
    plan.input.requests[2].window_end = "10:30"
    result = check(_optimized(plan), parts)
    assert result.errors == [
        "заявка №3 встаёт к brigade-1 на место 2 — досчёт её пропустил"
    ]


def test_request_that_does_not_fit_passes(plan, parts):
    """Окно 15:00–15:10 при смене до 15:30: час работы не помещается ни на одно место."""
    plan.input.engineers[0].shift_end = "15:30"
    plan.input.requests[2].window_start = "15:00"
    plan.input.requests[2].window_end = "15:10"
    assert check(_optimized(plan), parts).errors == []


@pytest.mark.parametrize("case", ["baseline", "fallback", "manual", "staged"])
def test_refill_lawful_exceptions(plan, parts, case):
    """Базовый вариант в середину не вставляет, ручную правку задал диспетчер, а поэтапный
    расчёт досчитывал на ужатых сроках аварий, которых в плане нет."""
    plan.algorithm = {
        "baseline": Algorithm.BASELINE,
        "manual": Algorithm.MANUAL,
    }.get(case, Algorithm.OPTIMIZED)
    plan.fallback_to_baseline = case == "fallback"
    if case == "manual":
        # Правка поверх встраивания наследует его режим, но заявку события ставил диспетчер.
        plan.replan_mode = ReplanMode.INSERT
        plan.event = _event(parts, 2)
    result = validator.validate(plan, parts[2], staged=case == "staged")
    assert result.errors == []


@pytest.mark.parametrize("parent", ["optimized", "baseline", "fallback"])
def test_insert_mode_checks_only_the_event_request(plan, parts, parent):
    """`insert` прежних неназначенных не трогает по контракту — но заявку события обязан, и
    ставит её досчётом при любом алгоритме родителя."""
    plan.algorithm = Algorithm.BASELINE if parent == "baseline" else Algorithm.OPTIMIZED
    plan.fallback_to_baseline = parent == "fallback"
    plan.replan_mode = ReplanMode.INSERT
    plan.event = _event(parts, 0)
    assert check(plan, parts).errors == []

    plan.event = _event(parts, 2)
    assert _says(check(plan, parts), "заявка №3 встаёт к brigade-1")


def test_pinned_request_is_tried_only_with_its_engineer(plan, parts):
    """Закреплённая досчитывается только к своей бригаде; не встала — закрепление из плана
    выпадает, и знает о нём только расчёт (PLAN 6.16)."""
    _optimized(plan)
    assert validator.validate(plan, parts[2], pinned={"3": "brigade-2"}).ok
    assert _says(
        validator.validate(plan, parts[2], pinned={"3": "brigade-1"}),
        "заявка №3 встаёт к brigade-1",
    )


def test_no_place_before_a_committed_stop(plan, parts):
    """Окно 09:00–09:15 достижимо только перед «1», а «1» закреплена событием 09:00."""
    plan.input.requests[2].window_end = "09:15"
    _optimized(plan).replan_mode = ReplanMode.FROM_EVENT
    plan.event = _event(parts, 2)
    assert _says(check(plan, parts), "на место 1")

    plan.routes[0].stops[0].committed = True
    assert check(plan, parts).errors == []


def test_engineer_gone_takes_no_new_work(plan, parts):
    """Выбывшая бригада новой работы не берёт вовсе (`schedule.free_from`) — и в `full`,
    где момента события нет, а место 1 с выездом в 09:00 раньше выбытия в 11:00."""
    _optimized(plan).replan_mode = ReplanMode.FULL
    plan.event = _event(parts, 2)
    assert _says(check(plan, parts), "на место 1")

    plan.input.engineers[0].unavailable_from = "11:00"
    assert check(plan, parts).errors == []


def test_validator_counts_the_route_from_the_engineer_home(
    plan, make_engineer, make_travel, make_request
):
    """План, где бригада из Каширы выехала из офиса, проверку не проходит (ответ 13).

    Валидатор пересчитывает день от стартовой точки бригады: плечо «дом → заявка» другое,
    и подделанные времена с километрами на нём не сходятся.
    """
    requests = [make_request("1", windowEnd="18:00")]
    engineer = make_engineer("brigade-1", startLat=54.84, startLon=38.19)
    home = "start:54.840000,38.190000"
    travel = make_travel(
        requests,
        engineers=[engineer],
        legs={(home, "1"): (1200, 20_000), ("office", "1"): (600, 5000)},
    )
    plan.input.requests = requests
    plan.input.engineers = [engineer]
    plan.routes = plan.routes[:1]
    plan.routes[0].stops = plan.routes[0].stops[:1]
    plan.assignments = {"1": "brigade-1"}
    plan.unassigned = []

    result = validator.validate(plan, travel)

    assert not result.ok
    assert any("прибытие" in error or "километры" in error for error in result.errors)


def test_route_over_equipment_capacity(
    make_request, make_engineer, make_dataset, make_travel, monkeypatch
):
    """Маршрут, который бригада не унесёт, ловится своим счётом (PLAN 6.7, блок 27)."""
    tasks = [
        make_request(str(number), requiredEquipment=["router"], windowEnd="18:00")
        for number in (1, 2)
    ]
    travel = make_travel(tasks)
    plan = service.build_plan(
        make_dataset(tasks, [make_engineer(equipment=["router"])]), travel
    )
    assert plan.validation.errors == []

    monkeypatch.setattr(settings, "equipment_capacity", {"car": 1})

    result = validator.validate(plan, travel)
    assert not result.ok
    assert any("2 ед. оборудования" in error for error in result.errors)


# --- обед (блок 79) ------------------------------------------------------------------


@pytest.mark.parametrize("algorithm", [Algorithm.BASELINE, Algorithm.OPTIMIZED])
def test_plan_with_lunch_passes_and_a_moved_lunch_is_caught(
    algorithm, make_request, make_engineer, make_dataset, make_travel
):
    """Обед есть у каждого, кому обязателен, валидатор пересчитывает его сам и ловит обед,
    сдвинутый в плане."""
    requests = [
        make_request("1", serviceDurationMin=240, windowEnd="13:00"),
        make_request(
            "2", serviceDurationMin=240, windowStart="13:00", windowEnd="18:00"
        ),
    ]
    engineer = make_engineer(lunchStart="13:00", lunchEnd="16:00", shiftEnd="22:00")
    travel = make_travel(requests)
    plan = service.build_plan(make_dataset(requests, [engineer]), travel, algorithm)

    assert plan.validation.ok, plan.validation.errors
    (route,) = plan.routes
    assert route.lunch is not None
    assert (route.lunch.start, route.lunch.end, route.lunch.after) == (
        "13:10",
        "14:10",
        "1",
    )

    moved = route.model_copy(
        update={
            "lunch": route.lunch.model_copy(update={"start": "15:00", "end": "16:00"})
        }
    )
    broken = plan.model_copy(update={"routes": [moved]})
    assert _says(validator.validate(broken, travel), "обед в плане")

    # Обед обязателен между работами (блок 82): план без него не проходит.
    missing = plan.model_copy(
        update={"routes": [route.model_copy(update={"lunch": None})]}
    )
    assert _says(validator.validate(missing, travel), "обед в плане — нет")


def test_lunch_before_the_first_work_is_caught(
    make_request, make_engineer, make_dataset, make_travel
):
    """Обед бывает только после работы (блок 82): обед, подставленный в план одной работы,
    валидатор ловит — по его пересчёту обеда нет."""
    task = make_request("1", windowStart="14:00", windowEnd="15:00")
    engineer = make_engineer(lunchStart="13:00", lunchEnd="16:00")
    travel = make_travel([task])
    plan = service.build_plan(make_dataset([task], [engineer]), travel)

    assert plan.validation.ok, plan.validation.errors
    (route,) = plan.routes
    assert route.lunch is None

    fake = route.model_copy(
        update={"lunch": Lunch(start="13:00", end="14:00", after=None)}
    )
    broken = plan.model_copy(update={"routes": [fake]})
    assert _says(validator.validate(broken, travel), "по пересчёту — нет")
