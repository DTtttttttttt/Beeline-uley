"""Линии маршрутов для карты (PLAN 3.2, 6.11, блок 10).

Valhalla не вызывается: разбор ответа проверяется на httpx.MockTransport, остальное — на
подмене `valhalla.route`. Координаты точек в фикстурах раздвинуты: по совпадающим точкам
ни порядок `[lon, lat]`, ни склейку участков не проверить.
"""

import json
from itertools import pairwise

import httpx
import polyline
import pytest

from app.dictionaries import Transport
from app.models import Algorithm
from app.planning import service
from app.routing import geometry, valhalla
from app.routing.geometry import MAX_LOCATIONS
from app.routing.matrices import OFFICE, point_id


@pytest.fixture
def spread():
    """Раздвигает точки таблицы переездов: в conftest они все в одной координате."""

    def apply(travel):
        travel.points = [
            (pid, 55.70 + number / 100, 37.77 + number / 100)
            for number, (pid, _, _) in enumerate(travel.points)
        ]
        return travel

    return apply


class FakeValhalla:
    """Участок на каждый переезд: начало, середина и конец.

    Середина — то, чем построенная линия отличается от прямого отрезка: по ней видно,
    что нарисован ответ Valhalla, а не запасной расчёт.
    """

    def __init__(self, error: Exception | None = None):
        self.calls: list[tuple[list, str]] = []
        self.error = error

    def __call__(self, locations, costing):
        self.calls.append((list(locations), costing))
        if self.error is not None:
            raise self.error
        return [
            [locations[i], _middle(locations[i], locations[i + 1]), locations[i + 1]]
            for i in range(len(locations) - 1)
        ]


def _middle(a, b):
    return ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)


@pytest.fixture
def fake(monkeypatch):
    stub = FakeValhalla()
    monkeypatch.setattr(valhalla, "route", stub)
    return stub


def coordinates(travel) -> list[tuple[float, float]]:
    """Точки маршрута «офис → заявки по порядку» — так их собирает `fill`."""
    return [(lat, lon) for _, lat, lon in travel.points]


# --- разбор ответа Valhalla --------------------------------------------------


def test_route_sends_locations_in_order_and_decodes_polyline6(monkeypatch):
    legs = [[(55.70, 37.77), (55.71, 37.78)], [(55.71, 37.78), (55.72, 37.79)]]
    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/route"
        sent.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "trip": {"legs": [{"shape": polyline.encode(leg, 6)} for leg in legs]}
            },
        )

    monkeypatch.setattr(
        valhalla,
        "client",
        httpx.Client(
            transport=httpx.MockTransport(handler), base_url="http://valhalla:8002"
        ),
    )
    shapes = valhalla.route(
        [(55.70, 37.77), (55.71, 37.78), (55.72, 37.79)], "pedestrian"
    )

    assert sent["costing"] == "pedestrian"
    assert sent["locations"] == [
        {"lat": 55.70, "lon": 37.77},
        {"lat": 55.71, "lon": 37.78},
        {"lat": 55.72, "lon": 37.79},
    ]
    assert shapes == legs


def test_polyline_precision_five_would_move_the_line(monkeypatch):
    """Точность у Valhalla именно 6: с пятёркой линия уезжает за сотни километров."""
    assert polyline.decode(polyline.encode([(55.70, 37.77)], 6), 5) != [(55.70, 37.77)]


# --- линия маршрута ----------------------------------------------------------


def test_line_starts_at_the_office_and_follows_stop_order(
    fake, make_travel, make_request, spread
):
    requests = [make_request("1"), make_request("2")]
    travel = spread(make_travel(requests))

    geometry.line("auto", coordinates(travel))

    locations, costing = fake.calls[0]
    assert costing == "auto"
    assert locations == coordinates(travel)


def test_line_is_lon_lat_for_the_map(fake, make_travel, make_request, spread):
    """Гоуча проекта: в MapGL координата — `[lon, lat]`, переворот делается только здесь."""
    travel = spread(make_travel([make_request("1")]))

    drawn = geometry.line("auto", coordinates(travel))

    assert drawn[0] == [37.77, 55.70]
    assert drawn[-1] == [37.78, 55.71]


def test_legs_are_joined_without_repeating_the_junction(
    fake, make_travel, make_request, spread
):
    requests = [make_request("1"), make_request("2")]
    travel = spread(make_travel(requests))

    drawn = geometry.line("auto", coordinates(travel))

    # Два переезда по три точки: общая точка стыка не повторяется.
    assert len(drawn) == 5
    assert all(a != b for a, b in pairwise(drawn))


def test_empty_route_has_no_line_and_no_request(fake, make_travel, spread):
    travel = spread(make_travel([]))

    assert geometry.line("auto", coordinates(travel)[:1]) == []
    assert fake.calls == []


def test_long_route_is_split_by_max_locations(fake, make_travel, make_request, spread):
    count = MAX_LOCATIONS  # офис плюс столько заявок — на одну точку больше лимита
    requests = [make_request(str(number)) for number in range(1, count + 1)]
    travel = spread(make_travel(requests))

    drawn = geometry.line("auto", coordinates(travel))

    first, second = (locations for locations, _ in fake.calls)
    assert len(first) == MAX_LOCATIONS and len(second) == 2
    assert first[-1] == second[0]  # куски перекрываются точкой, иначе линия порвётся
    assert len(drawn) == 2 * count + 1
    assert all(a != b for a, b in pairwise(drawn))


# --- запасной расчёт ---------------------------------------------------------


def status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://valhalla:8002/route")
    return httpx.HTTPStatusError(
        "ошибка", request=request, response=httpx.Response(code, request=request)
    )


@pytest.mark.parametrize(
    "error",
    [
        # 4xx не поднимается наверх, в отличие от таблиц переездов: линия — картинка,
        # и точка, не севшая на дорогу, не должна ронять расчёт плана целиком.
        status_error(400),
        # 200 с чужим телом падает уже при разборе — терять из-за этого план тем более нельзя.
        KeyError("trip"),
        ValueError("Expecting value: line 1 column 1"),
    ],
    ids=["400", "нет trip в ответе", "тело не JSON"],
)
def test_failed_request_gives_straight_segments(
    monkeypatch, make_travel, make_request, spread, error
):
    monkeypatch.setattr(valhalla, "route", FakeValhalla(error))
    requests = [make_request("1"), make_request("2")]
    travel = spread(make_travel(requests))

    drawn = geometry.line("auto", coordinates(travel))

    assert drawn == [[lon, lat] for lat, lon in coordinates(travel)]


def test_empty_answer_gives_straight_segments_too(
    monkeypatch, make_travel, make_request, spread
):
    """Пустая линия — единственный отказ, который не виден ни ошибкой, ни логом.

    Маршрут со стопами получил бы `geometry: []`, и на карте это молчание не отличить
    от нормы, поэтому ответ без участков считается таким же отказом, как 4xx.
    """
    monkeypatch.setattr(valhalla, "route", lambda locations, costing: [])
    travel = spread(make_travel([make_request("1"), make_request("2")]))

    drawn = geometry.line("auto", coordinates(travel))

    assert drawn == [[lon, lat] for lat, lon in coordinates(travel)]


def test_line_lets_a_connection_failure_through(
    monkeypatch, make_travel, make_request, spread
):
    """Отказ связи — состояние всей Valhalla, и решает его `fill`, а не каждый маршрут."""
    monkeypatch.setattr(valhalla, "route", FakeValhalla(httpx.ConnectError("нет")))
    travel = spread(make_travel([make_request("1")]))

    with pytest.raises(httpx.ConnectError):
        geometry.line("auto", coordinates(travel))


def test_unreachable_valhalla_is_asked_once_per_plan(
    monkeypatch, make_travel, make_request, make_engineer, make_dataset, spread
):
    """Иначе расчёт плана вставал бы на таймаут по разу на бригаду (120 с × 10).

    Километры к этому моменту посчитаны и лежат в кэше матриц, поэтому план обязан
    досчитаться — просто с прямыми линиями.
    """
    stub = FakeValhalla(httpx.ConnectError("нет соединения"))
    monkeypatch.setattr(valhalla, "route", stub)
    requests = [
        make_request("1"),
        make_request("2", windowStart="13:00", windowEnd="17:00"),
    ]
    # Каждой бригаде достанется по заявке: у первой не хватает смены на вторую.
    engineers = [
        make_engineer("brigade-1", shiftEnd="11:00"),
        make_engineer("brigade-2"),
    ]
    dataset = make_dataset(requests, engineers)
    travel = spread(make_travel(requests))

    plan = service.build_plan(dataset, travel, Algorithm.BASELINE)
    geometry.fill(plan, travel)

    drawn = [route for route in plan.routes if route.stops]
    assert len(drawn) == 2
    assert len(stub.calls) == 1  # спросили один раз, дальше рисуем прямыми без запросов
    where = {pid: (lat, lon) for pid, lat, lon in travel.points}
    for route in drawn:
        expected = [where[OFFICE]] + [
            where[point_id(s.request_id)] for s in route.stops
        ]
        assert route.geometry == [[lon, lat] for lat, lon in expected]


def test_straight_line_does_not_make_the_plan_approximate(
    monkeypatch, make_travel, make_request, make_engineer, make_dataset, spread
):
    """`approximate` — про километры и времена, а не про рисунок (PLAN 5.2).

    Матрицы посчитаны Valhalla, значит числа точные; поднять флаг из-за прямой линии
    значило бы сказать диспетчеру неправду о них.
    """
    monkeypatch.setattr(valhalla, "route", FakeValhalla(httpx.ConnectError("нет")))
    requests = [make_request("1")]
    dataset = make_dataset(requests, [make_engineer("brigade-1")])
    travel = spread(make_travel(requests))

    plan = service.build_plan(dataset, travel, Algorithm.BASELINE)
    geometry.fill(plan, travel)

    assert plan.routes[0].geometry
    assert plan.approximate is False


# --- план целиком ------------------------------------------------------------


def test_public_transport_is_drawn_as_pedestrian(
    fake, make_travel, make_request, make_engineer, make_dataset, spread
):
    """Дополнения, п. 14: общественный транспорт считается по пешеходу — и линия тоже."""
    requests = [make_request("1")]
    dataset = make_dataset(
        requests, [make_engineer("brigade-1", transport=Transport.PUBLIC_TRANSPORT)]
    )
    travel = spread(make_travel(requests))

    geometry.fill(service.build_plan(dataset, travel, Algorithm.BASELINE), travel)

    assert [costing for _, costing in fake.calls] == ["pedestrian"]


def test_fill_draws_every_route_by_its_own_transport(
    fake, make_travel, make_request, make_engineer, make_dataset, spread
):
    requests = [make_request("1"), make_request("2")]
    engineers = [
        make_engineer("brigade-1", transport=Transport.BICYCLE),
        # Вторая бригада ничего не получит: базовый вариант отдаёт всё первой подходящей.
        make_engineer("brigade-2"),
    ]
    dataset = make_dataset(requests, engineers)
    travel = spread(make_travel(requests))

    plan = service.build_plan(dataset, travel, Algorithm.BASELINE)
    geometry.fill(plan, travel)

    working, idle = plan.routes
    assert working.geometry and [costing for _, costing in fake.calls] == ["bicycle"]
    # Пустой маршрут линии не даёт и Valhalla не тревожит.
    assert idle.stops == [] and idle.geometry == []


def test_line_follows_the_stops_of_the_plan(
    fake, make_travel, make_request, make_engineer, make_dataset, spread
):
    requests = [make_request("1"), make_request("2")]
    dataset = make_dataset(requests, [make_engineer("brigade-1")])
    travel = spread(make_travel(requests))

    plan = service.build_plan(dataset, travel, Algorithm.BASELINE)
    geometry.fill(plan, travel)

    route = plan.routes[0]
    # Ожидание собирается по стопам плана, а не фильтром по набору: иначе тест не заметил
    # бы линию, нарисованную в порядке входных данных вместо порядка посещения.
    where = {pid: (lat, lon) for pid, lat, lon in travel.points}
    expected = [where[OFFICE]] + [where[point_id(s.request_id)] for s in route.stops]
    locations, _ = fake.calls[0]
    assert locations == expected
    assert route.geometry[0] == [37.77, 55.70]  # линия начинается в офисе


def test_line_starts_at_the_engineer_home(
    fake, make_travel, make_request, make_engineer, make_dataset, spread
):
    """Линия начинается там же, откуда бригада выехала, — в доме, а не в офисе (ответ 13)."""
    requests = [make_request("1", windowEnd="18:00")]
    engineer = make_engineer("brigade-1", startLat=54.84, startLon=38.19)
    travel = spread(make_travel(requests, engineers=[engineer]))
    plan = service.build_plan(make_dataset(requests, [engineer]), travel)

    geometry.fill(plan, travel)

    home = next(
        (lat, lon)
        for pid, lat, lon in travel.points
        if pid == "start:54.840000,38.190000"
    )
    ((locations, _),) = fake.calls
    assert locations[0] == home
    assert plan.routes[0].geometry[0] == [home[1], home[0]]  # [lon, lat] для карты
