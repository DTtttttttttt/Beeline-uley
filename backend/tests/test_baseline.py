"""Базовый вариант распределения (PLAN 6.4, блок 7.5).

Фикстуры общие (tests/conftest.py): смена 09:00–18:00, переезд 10 минут и 5 км, заявка —
60 минут работы. Ни Mongo, ни Valhalla здесь не нужны.
"""

from app.models import Input
from app.planning import baseline

OFFICE = {"address": "офис", "lat": 55.70, "lon": 37.77}


def make_input(requests, engineers) -> Input:
    return Input(office=OFFICE, requests=requests, engineers=engineers)


def test_visiting_order_matches_assignment_order(
    make_request, make_engineer, make_travel
):
    """Заявка добавляется в конец маршрута, поэтому порядок посещения — порядок входа."""
    requests = [make_request("1"), make_request("2"), make_request("3")]
    engineer = make_engineer()
    routes, unassigned = baseline.assign(
        make_input(requests, [engineer]), make_travel(requests)
    )

    assert [request.id for request in routes["brigade-1"]] == ["1", "2", "3"]
    assert unassigned == []


def test_first_suitable_engineer_in_input_order_takes_the_request(
    make_request, make_engineer, make_travel
):
    """Дословно п. 2.3 ТЗ: перебор бригад идёт в исходном порядке и останавливается на первой."""
    requests = [make_request("1")]
    engineers = [make_engineer("brigade-1"), make_engineer("brigade-2")]
    routes, _ = baseline.assign(make_input(requests, engineers), make_travel(requests))

    assert [request.id for request in routes["brigade-1"]] == ["1"]
    assert routes["brigade-2"] == []


def test_request_goes_to_the_next_engineer_when_the_first_does_not_fit(
    make_request, make_engineer, make_travel
):
    requests = [make_request("1", requiredEquipment=["router"])]
    engineers = [
        make_engineer("brigade-1"),
        make_engineer("brigade-2", equipment=["router"]),
    ]
    routes, unassigned = baseline.assign(
        make_input(requests, engineers), make_travel(requests)
    )

    assert routes["brigade-1"] == []
    assert [request.id for request in routes["brigade-2"]] == ["1"]
    assert unassigned == []


def test_request_without_a_suitable_engineer_stays_unassigned(
    make_request, make_engineer, make_travel
):
    requests = [make_request("1", skill="emergency")]
    engineers = [make_engineer("brigade-1")]  # навык local
    routes, unassigned = baseline.assign(
        make_input(requests, engineers), make_travel(requests)
    )

    assert routes["brigade-1"] == []
    assert [request.id for request in unassigned] == ["1"]


def test_request_that_does_not_fit_the_shift_stays_unassigned(
    make_request, make_engineer, make_travel
):
    """Ресурсы подходят, но работа заканчивается за концом смены (PLAN 6.2, SHIFT_END)."""
    requests = [make_request("1", windowStart="17:30", windowEnd="18:00")]
    engineers = [make_engineer("brigade-1")]
    routes, unassigned = baseline.assign(
        make_input(requests, engineers), make_travel(requests)
    )

    assert routes["brigade-1"] == []
    assert [request.id for request in unassigned] == ["1"]


def test_unreachable_request_stays_unassigned(make_request, make_engineer, make_travel):
    """Valhalla вернула `null`: пути нет, назначать нельзя (PLAN 6.2, NO_ROUTE)."""
    requests = [make_request("1")]
    engineers = [make_engineer("brigade-1")]
    travel = make_travel(requests, legs={("office", "1"): None})
    routes, unassigned = baseline.assign(make_input(requests, engineers), travel)

    assert routes["brigade-1"] == []
    assert [request.id for request in unassigned] == ["1"]


def test_second_request_of_a_full_route_moves_on(
    make_request, make_engineer, make_travel
):
    """Маршрут проверяется целиком: вторая заявка не должна ломать уже набранную."""
    requests = [
        make_request("1"),
        make_request("2", windowStart="09:00", windowEnd="09:30"),
    ]
    engineers = [make_engineer("brigade-1"), make_engineer("brigade-2")]
    routes, unassigned = baseline.assign(
        make_input(requests, engineers), make_travel(requests)
    )

    # «2» не встаёт после «1» (освободится в 10:10, окно до 09:30) — уходит второй бригаде.
    assert [request.id for request in routes["brigade-1"]] == ["1"]
    assert [request.id for request in routes["brigade-2"]] == ["2"]
    assert unassigned == []
