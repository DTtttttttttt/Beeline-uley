"""Что изменилось между версиями плана (PLAN 5.5, 6.13, блок 11).

Планы собираются руками из минимума, который читает `diff`: заявки входа, стопы маршрутов
и метрики. Так каждый вид изменения проверяется отдельно — через расчёт их пришлось бы
вызывать совпадением обстоятельств.
"""

from datetime import UTC, datetime

import pytest

from app.models import Algorithm, Change, Input, Metrics, Plan, Route, Stop
from app.planning import diff

OFFICE = {"address": "офис", "lat": 55.70, "lon": 37.77}


@pytest.fixture
def make_plan(make_request):
    def make(
        requests: list[str],
        routes: dict[str, list[tuple[str, str]]],
        km: dict[str, float] | None = None,
        engineers_used: int = 0,
        total_km: float = 0.0,
        unassigned_count: int = 0,
    ) -> Plan:
        km = km or {}
        return Plan(
            id="plan",
            dataset_id="test",
            algorithm=Algorithm.BASELINE,
            input=Input(
                office=OFFICE,
                requests=[make_request(request_id) for request_id in requests],
                engineers=[],
            ),
            routes=[
                Route(
                    engineer_id=engineer_id,
                    km=km.get(engineer_id, 0.0),
                    stops=[_stop(request_id, start) for request_id, start in stops],
                )
                for engineer_id, stops in routes.items()
            ],
            metrics=Metrics(
                engineers_used=engineers_used,
                total_km=total_km,
                unassigned_count=unassigned_count,
            ),
            created_at=datetime.now(UTC),
        )

    return make


def _stop(request_id: str, start: str) -> Stop:
    return Stop(
        request_id=request_id,
        departure=start,
        arrival=start,
        start=start,
        end=start,
        travel_km=1.0,
        travel_min=1,
    )


def only(changes) -> tuple:
    """Единственное изменение: список должен быть ровно про то, что проверяет тест."""
    assert len(changes) == 1, changes
    return changes[0]


# --- виды изменений по заявкам -----------------------------------------------


def test_cancelled_request(make_plan):
    """Отмена первой заявки поднимает вторую на её место — это тоже изменение маршрута."""
    old = make_plan(["1", "2"], {"brigade-1": [("1", "09:00"), ("2", "10:00")]})
    new = make_plan(["2"], {"brigade-1": [("2", "10:00")]})

    changes = diff.compare(old, new).requests
    assert [(item.request_id, item.change) for item in changes] == [
        ("1", Change.CANCELLED),
        ("2", Change.ORDER_CHANGED),
    ]
    assert (changes[1].old_position, changes[1].new_position) == (2, 1)


def test_added_request_names_its_engineer(make_plan):
    """Кто взял новую заявку — первое, что нужно диспетчеру после события."""
    old = make_plan(["1"], {"brigade-1": [("1", "09:00")]})
    new = make_plan(["1", "9"], {"brigade-1": [("1", "09:00"), ("9", "13:00")]})

    change = only(diff.compare(old, new).requests)
    assert (change.request_id, change.change) == ("9", Change.ADDED)
    assert change.to_engineer == "brigade-1"


def test_request_became_unassigned(make_plan):
    old = make_plan(["1"], {"brigade-1": [("1", "09:00")]})
    new = make_plan(["1"], {"brigade-1": []})

    change = only(diff.compare(old, new).requests)
    assert (change.request_id, change.change) == ("1", Change.BECAME_UNASSIGNED)
    assert change.from_engineer == "brigade-1"


def test_reassigned_request(make_plan):
    old = make_plan(["1"], {"brigade-1": [("1", "09:00")], "brigade-2": []})
    new = make_plan(["1"], {"brigade-1": [], "brigade-2": [("1", "09:00")]})

    change = only(diff.compare(old, new).requests)
    assert (change.request_id, change.change) == ("1", Change.REASSIGNED)
    assert (change.from_engineer, change.to_engineer) == ("brigade-1", "brigade-2")


def test_request_that_found_an_engineer_is_reassigned_from_nobody(make_plan):
    """Отдельного вида «стала назначенной» в PLAN 5.5 нет — его покрывает `from: null`."""
    old = make_plan(["1"], {"brigade-1": []})
    new = make_plan(["1"], {"brigade-1": [("1", "09:00")]})

    change = only(diff.compare(old, new).requests)
    assert (change.request_id, change.change) == ("1", Change.REASSIGNED)
    assert change.from_engineer is None
    assert change.to_engineer == "brigade-1"


def test_order_changed(make_plan):
    old = make_plan(["1", "2"], {"brigade-1": [("1", "09:00"), ("2", "11:00")]})
    new = make_plan(["1", "2"], {"brigade-1": [("2", "09:00"), ("1", "11:00")]})

    changes = diff.compare(old, new).requests
    assert [(item.request_id, item.change) for item in changes] == [
        ("1", Change.ORDER_CHANGED),
        ("2", Change.ORDER_CHANGED),
    ]
    assert (changes[0].old_position, changes[0].new_position) == (1, 2)
    assert changes[0].engineer_id == "brigade-1"


def test_time_changed(make_plan):
    old = make_plan(["1"], {"brigade-1": [("1", "15:00")]})
    new = make_plan(["1"], {"brigade-1": [("1", "15:40")]})

    change = only(diff.compare(old, new).requests)
    assert (change.request_id, change.change) == ("1", Change.TIME_CHANGED)
    assert (change.old_start, change.new_start) == ("15:00", "15:40")


def test_small_time_shift_is_not_a_change(make_plan):
    """Сдвиг на пять минут — шум пересчёта, а не новость для диспетчера (PLAN 6.13)."""
    old = make_plan(["1"], {"brigade-1": [("1", "15:00")]})
    new = make_plan(["1"], {"brigade-1": [("1", "15:05")]})

    assert diff.compare(old, new).requests == []


def test_identical_plans_have_no_changes(make_plan):
    old = make_plan(["1", "2"], {"brigade-1": [("1", "09:00")], "brigade-2": []})
    new = make_plan(["1", "2"], {"brigade-1": [("1", "09:00")], "brigade-2": []})

    assert diff.compare(old, new).requests == []
    assert diff.compare(old, new).engineers == []


# --- бригады и метрики -------------------------------------------------------


def test_engineers_block_lists_only_what_changed(make_plan):
    old = make_plan(
        ["1", "2"],
        {"brigade-1": [("1", "09:00")], "brigade-2": [("2", "09:00")]},
        km={"brigade-1": 12.1, "brigade-2": 7.0},
    )
    new = make_plan(
        ["1", "2"],
        {"brigade-1": [("1", "09:00")], "brigade-2": [("2", "09:00")]},
        km={"brigade-1": 18.4, "brigade-2": 7.0},
    )

    changes = diff.compare(old, new).engineers
    assert len(changes) == 1
    assert (changes[0].engineer_id, changes[0].old_km, changes[0].new_km) == (
        "brigade-1",
        12.1,
        18.4,
    )
    assert (changes[0].old_count, changes[0].new_count) == (1, 1)


def test_metrics_are_reported_as_before_and_after(make_plan):
    old = make_plan(
        ["1"],
        {"brigade-1": [("1", "09:00")]},
        engineers_used=7,
        total_km=187.2,
        unassigned_count=5,
    )
    new = make_plan(
        ["1"],
        {"brigade-1": [("1", "09:00")]},
        engineers_used=8,
        total_km=193.5,
        unassigned_count=5,
    )

    metrics = diff.compare(old, new).metrics
    assert metrics.engineers_used == [7, 8]
    assert metrics.total_km == [187.2, 193.5]
    assert metrics.unassigned_count == [5, 5]
