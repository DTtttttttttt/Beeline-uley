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
from itertools import pairwise

import httpx

from ..models import Plan, Route
from . import valhalla
from .matrices import Travel, haversine, point_id, start_id, turn_id
from .valhalla import ROUTE_PROFILE_BY_TRANSPORT, Point

log = logging.getLogger(__name__)

# Наименьший `service_limits.*.max_locations` из valhalla/custom_files/valhalla.json:
# у `auto` он 20, у `bicycle` и `pedestrian` — 50. Держим один предел на все профили:
# разный лимит на профиль означал бы разную разбивку одного и того же маршрута.
MAX_LOCATIONS = 20


def _points(plan: Plan) -> dict[str, tuple[str, list[str], set[int]]]:
    """Маршрут бригады точками: транспорт, точки линии по порядку и участки, которые
    продолжают плечо (от точки разворота). По ним линия и рисуется, и узнаётся неизменной."""
    transports = {engineer.id: engineer.transport for engineer in plan.input.engineers}
    # Линия начинается там же, откуда бригада выехала: офис или её дом (PLAN 2.4).
    starts = {
        engineer.id: start_id(plan.input.office, engineer)
        for engineer in plan.input.engineers
    }
    # Разворот на аварию (блок 74): линия идёт по брошенному плечу до точки разворота и
    # оттуда к аварии — обычной линией, без отдельного рисунка.
    turns = {
        engineer.id: {
            turn.emergency_id: turn_id(turn.lat, turn.lon)
            for turn in engineer.dispatched
            if turn.lat is not None and turn.lon is not None
        }
        for engineer in plan.input.engineers
    }
    result = {}
    for route in plan.routes:
        ids, inner = [starts[route.engineer_id]], set()
        for stop in route.stops:
            turn = turns[route.engineer_id].get(stop.request_id)
            if turn is not None:
                inner.add(len(ids))  # участок от точки разворота — продолжение плеча
                ids.append(turn)
            ids.append(point_id(stop.request_id))
        result[route.engineer_id] = (transports[route.engineer_id], ids, inner)
    return result


def fill(plan: Plan, travel: Travel, previous: Plan | None = None) -> None:
    """Проставляет линии всем маршрутам плана. Считается по готовому плану, поэтому
    базовый вариант и оптимизация проходят здесь одинаково.

    Недоступная Valhalla спрашивается **один раз на план**: дальше маршруты рисуются
    прямыми без запросов. Иначе расчёт вставал бы на таймаут по разу на бригаду — а
    километры к этому моменту уже посчитаны и лежат в кэше матриц, то есть обещание
    README «Valhalla для расчёта не нужна» держалось бы только на живой Valhalla.

    `previous` — версия, от которой посчитан план (часы дня, блок 82): маршрут с теми же
    транспортом и точками по порядку берёт её линию без запроса. Часы закрывают работы, а
    порядок почти не трогают, и скачок на вечер иначе спрашивал бы Valhalla на каждую из
    десятков версий.
    """
    points = _points(plan)
    known = _points(previous) if previous else {}
    drawn = {route.engineer_id: route for route in previous.routes} if previous else {}
    reachable = True
    for route in plan.routes:
        transport, ids, inner = points[route.engineer_id]
        before = drawn.get(route.engineer_id)
        if (
            before is not None
            and before.geometry
            and known.get(route.engineer_id) == (transport, ids, inner)
        ):
            route.geometry = before.geometry
            route.leg_starts = before.leg_starts
            continue
        coordinates = _coordinates(ids, travel)
        if len(coordinates) < 2:
            continue  # пустой маршрут рисовать нечем, и Valhalla зря не тревожим
        legs: list[int] = list(range(len(coordinates) - 1))
        geometry = None
        if reachable:
            try:
                geometry, legs = line(
                    ROUTE_PROFILE_BY_TRANSPORT[transport],
                    coordinates,
                )
            except httpx.TransportError as error:
                log.warning(
                    "Valhalla недоступна (%s), остальные маршруты рисуем прямыми", error
                )
                reachable = False
        route.geometry = _flip(coordinates) if geometry is None else geometry
        route.leg_starts = (
            [start for leg, start in enumerate(legs) if leg not in inner]
            if legs
            else []
        )


def line(profile: str, coordinates: list[Point]) -> tuple[list[list[float]], list[int]]:
    """Линия через точки маршрута по порядку, `[lon, lat]` для карты (PLAN 3.6), и
    индексы начала каждого участка в ней — по одному на стоп (проигрывание дня, 6.11).

    Отказ связи наружу **не ловится**: недоступна вся Valhalla, а не этот маршрут, и
    решение принимает `fill` — иначе каждый маршрут ждал бы свой таймаут.
    """
    joined, starts = _join(_shapes(coordinates, profile))
    # Участков меньше, чем плеч, — Valhalla склеила плечи, и по индексам маркер ехал бы
    # не по своему участку. Без них карта ведёт его по прямой между точками.
    return _flip(joined), starts if len(starts) == len(coordinates) - 1 else []


def leg_line(route: Route, index: int) -> list[Point] | None:
    """Линия переезда к стопу `index` в `(lat, lon)`; без участков — `None` (PLAN 6.11)."""
    starts = route.leg_starts
    if len(starts) != len(route.stops) or len(route.geometry) < 2:
        return None
    end = starts[index + 1] if index + 1 < len(starts) else len(route.geometry) - 1
    return [(lat, lon) for lon, lat in route.geometry[starts[index] : end + 1]]


def position_at(line: list[Point], share: float) -> Point:
    """Где на линии бригада, прошедшая долю `share` её длины (блок 74).

    Доля — прошедшего времени переезда, равномерно по длине: так же ведёт инженера
    проигрывание дня (`playback.positionAt`), и точка разворота совпадает с маркером на карте.
    """
    lengths = [haversine(a, b) for a, b in pairwise(line)]
    left = sum(lengths) * min(max(share, 0.0), 1.0)
    for (a, b), length in zip(pairwise(line), lengths, strict=True):
        if left <= length and length > 0:
            part = left / length
            return (a[0] + (b[0] - a[0]) * part, a[1] + (b[1] - a[1]) * part)
        left -= length
    return line[-1]


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
    straight = [list(leg) for leg in pairwise(coordinates)]  # по отрезку на плечо
    try:
        shapes = valhalla.route(coordinates, profile)
    except (httpx.HTTPStatusError, LookupError, TypeError, ValueError) as error:
        log.warning("Valhalla не построила линию (%s), рисуем прямыми", error)
        return straight
    # Ответ без участков — тоже не линия. Без этого маршрут со стопами получил бы пустую
    # `geometry`: на карте нет линии, в логе нет ошибки, и молчание не отличить от нормы.
    return shapes or straight


def _join(shapes: Iterable[list[Point]]) -> tuple[list[Point], list[int]]:
    """Склейка встык: конец участка — начало следующего, общую точку не повторяем.
    Вторым — индекс, с которого в склейке начинается каждый участок."""
    joined: list[Point] = []
    starts: list[int] = []
    for shape in shapes:
        starts.append(max(len(joined) - 1, 0))
        joined += shape[1:] if joined else shape
    return joined, starts
