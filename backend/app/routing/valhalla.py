"""Запросы к своему Valhalla (PLAN 3.2, 3.3). Наружу — целые секунды и целые метры."""

import httpx
import polyline

from ..config import settings
from ..dictionaries import Transport

# Клиент модульный: тесты подменяют его на httpx.MockTransport, внешние сервисы в тестах не зовём.
# trust_env=False: Valhalla всегда своя — на localhost или в сети compose. Системный прокси
# (в Windows он задаётся в реестре и подхватывается httpx) до неё не достучится и вернёт 503.
client = httpx.Client(
    base_url=settings.valhalla_url,
    timeout=settings.valhalla_timeout,
    trust_env=False,
)

# Профиль транспорта в таблицах переездов. `public_transport` — **производный**: своего
# костинга у Valhalla для него нет, время считается из пешехода и автомобиля усреднённой
# оценкой (PLAN 3.3, ответ 14). Спрашивать Valhalla с таким costing нельзя — она ответит 4xx.
TRANSIT = "public_transport"
PROFILE_BY_TRANSPORT = {
    Transport.CAR: "auto",
    Transport.BICYCLE: "bicycle",
    Transport.PUBLIC_TRANSPORT: TRANSIT,
}
# Из чего считается производный профиль: пешком и на автомобиле по тем же дорогам.
TRANSIT_BASE = ("pedestrian", "auto")

# Чем рисуется линия маршрута (PLAN 6.11). Общественный транспорт рисуется пешеходным
# маршрутом: линия — картинка, у Valhalla костинга для него нет, а времена и километры
# плана берутся не отсюда, а из таблиц переездов.
ROUTE_PROFILE_BY_TRANSPORT = PROFILE_BY_TRANSPORT | {
    Transport.PUBLIC_TRANSPORT: "pedestrian"
}

Point = tuple[float, float]  # (lat, lon)
Row = list[int | None]


def matrix(
    sources: list[Point], targets: list[Point], costing: str
) -> tuple[list[Row], list[Row]]:
    """Времена (с) и расстояния (м) для всех пар. `None` — пути нет (PLAN 5.2)."""
    response = client.post(
        "/sources_to_targets",
        json={
            "sources": [{"lat": lat, "lon": lon} for lat, lon in sources],
            "targets": [{"lat": lat, "lon": lon} for lat, lon in targets],
            "costing": costing,
            "units": "kilometers",
            # verbose: false отдаёт только durations и distances — остальное нам не нужно.
            "verbose": False,
        },
    )
    response.raise_for_status()
    answer = response.json()["sources_to_targets"]
    durations = [[_round(value, 1) for value in row] for row in answer["durations"]]
    distances = [[_round(value, 1000) for value in row] for row in answer["distances"]]
    return durations, distances


def route(locations: list[Point], costing: str) -> list[list[Point]]:
    """Участки линии маршрута через все точки по порядку: каждый — список (lat, lon).

    `directions_type: none` убирает текстовые указания: от ответа нам нужна только геометрия.
    Склейку участков и переворот в `[lon, lat]` делает `geometry.py` — там же, где разбиение
    по `max_locations`, поэтому границы участков и границ кусков склеиваются одним правилом.
    """
    response = client.post(
        "/route",
        json={
            "locations": [{"lat": lat, "lon": lon} for lat, lon in locations],
            "costing": costing,
            "directions_options": {"units": "kilometers", "directions_type": "none"},
        },
    )
    response.raise_for_status()
    # Точность 6 — у Valhalla она именно такая, с пятёркой линия уедет на сотни метров.
    return [polyline.decode(leg["shape"], 6) for leg in response.json()["trip"]["legs"]]


def _round(value: float | None, factor: int) -> int | None:
    """Секунды как есть, километры — в метры; недостижимую пару оставляем `None`."""
    return None if value is None else round(value * factor)
