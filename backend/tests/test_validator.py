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
from app.models import Plan
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
    ],
)
def test_metrics_must_match(plan, parts, field, value, text):
    setattr(plan.metrics, field, value)
    assert _says(check(plan, parts), text)


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
