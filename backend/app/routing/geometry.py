"""Линии маршрутов для карты (PLAN 6.11).

По одному запросу `/route` на маршрут «офис → заявки по порядку», при превышении
`max_locations` — по частям. Наружу отдаётся `[lon, lat]`: это порядок MapGL, и переворот
делается один раз, здесь.

Отказы разделены по области действия. Отказ на одном запросе (код ответа, неразобранное
тело) — беда этого куска линии: он рисуется прямыми, остальные считаются как обычно.
Отказ связи — состояние всей Valhalla, и решает его `fill` один раз на план.
"""

import logging
from collections.abc import Iterable

import httpx

from ..models import Plan
from . import valhalla
from .matrices import Travel, point_id, start_id
from .valhalla import ROUTE_PROFILE_BY_TRANSPORT, Point

log = logging.getLogger(__name__)

# Наименьший `service_limits.*.max_locations` из valhalla/custom_files/valhalla.json:
# у `auto` он 20, у `bicycle` и `pedestrian` — 50. Держим один предел на все профили:
# разный лимит на профиль означал бы разную разбивку одного и того же маршрута.
MAX_LOCATIONS = 20


def fill(plan: Plan, travel: Travel) -> None:
    """Проставляет линии всем маршрутам плана. Считается по готовому плану, поэтому
    базовый вариант и оптимизация проходят здесь одинаково.

    Недоступная Valhalla спрашивается **один раз на план**: дальше маршруты рисуются
    прямыми без запросов. Иначе расчёт вставал бы на таймаут по разу на бригаду — а
    километры к этому моменту уже посчитаны и лежат в кэше матриц, то есть обещание
    README «Valhalla для расчёта не нужна» держалось бы только на живой Valhalla.
    """
    transports = {engineer.id: engineer.transport for engineer in plan.input.engineers}
    # Линия начинается там же, откуда бригада выехала: офис или её дом (PLAN 2.4).
    starts = {
        engineer.id: start_id(plan.input.office, engineer)
        for engineer in plan.input.engineers
    }
    reachable = True
    for route in plan.routes:
        coordinates = _coordinates(
            [starts[route.engineer_id]]
            + [point_id(stop.request_id) for stop in route.stops],
            travel,
        )
        if len(coordinates) < 2:
            continue  # пустой маршрут рисовать нечем, и Valhalla зря не тревожим
        if reachable:
            try:
                route.geometry = line(
                    ROUTE_PROFILE_BY_TRANSPORT[transports[route.engineer_id]],
                    coordinates,
                )
                continue
            except httpx.TransportError as error:
                log.warning(
                    "Valhalla недоступна (%s), остальные маршруты рисуем прямыми", error
                )
                reachable = False
        route.geometry = _flip(coordinates)


def line(profile: str, coordinates: list[Point]) -> list[list[float]]:
    """Линия через точки маршрута по порядку, `[lon, lat]` для карты (PLAN 3.6).

    Отказ связи наружу **не ловится**: недоступна вся Valhalla, а не этот маршрут, и
    решение принимает `fill` — иначе каждый маршрут ждал бы свой таймаут.
    """
    return _flip(_join(_shapes(coordinates, profile)))


def _coordinates(point_ids: list[str], travel: Travel) -> list[Point]:
    return [travel.points[travel.index[pid]][1:] for pid in point_ids]


def _flip(coordinates: Iterable[Point]) -> list[list[float]]:
    """Единственный переворот `(lat, lon)` → `[lon, lat]` на весь проект."""
    return [[lon, lat] for lat, lon in coordinates]


def _shapes(coordinates: list[Point], profile: str) -> Iterable[list[Point]]:
    """Участки линии по порядку: по запросу на каждый кусок не длиннее `MAX_LOCATIONS`."""
    step = MAX_LOCATIONS - 1  # куски перекрываются одной точкой, чтобы линия не рвалась
    for start in range(0, len(coordinates) - 1, step):
        yield from _chunk(coordinates[start : start + MAX_LOCATIONS], profile)


def _chunk(coordinates: list[Point], profile: str) -> list[list[Point]]:
    """Участки одного куска; при отказе на запросе — прямые отрезки (PLAN 6.11).

    Ошибка 4xx здесь не поднимается наверх, в отличие от таблиц переездов: те дают
    километры и времена плана, а это картинка. Уронить весь расчёт из-за того, что одна
    точка не села на дорогу, нельзя — на карте прямой отрезок и так виден.

    По той же причине ловится и неразобранный ответ: 200 с чужим телом (страница прокси,
    ошибка Valhalla в другом формате) падает уже при разборе — `LookupError` на `trip`,
    `ValueError` на теле не-JSON, — и терять из-за этого посчитанный план тем более нельзя.
    Ответ, разобранный в пустоту, идёт туда же: пустая линия — единственный отказ, который
    ничем себя не выдаёт, ни ошибкой, ни записью в логе.
    """
    try:
        shapes = valhalla.route(coordinates, profile)
    except (httpx.HTTPStatusError, LookupError, TypeError, ValueError) as error:
        log.warning("Valhalla не построила линию (%s), рисуем прямыми", error)
        return [coordinates]
    # Ответ без участков — тоже не линия. Без этого маршрут со стопами получил бы пустую
    # `geometry`: на карте нет линии, в логе нет ошибки, и молчание не отличить от нормы.
    return shapes or [coordinates]


def _join(shapes: Iterable[list[Point]]) -> list[Point]:
    """Склейка встык: конец участка — начало следующего, общую точку не повторяем."""
    joined: list[Point] = []
    for shape in shapes:
        joined += shape[1:] if joined else shape
    return joined
