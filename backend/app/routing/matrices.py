"""Таблицы переездов набора: построение, кэш, досчёт точки, запасной расчёт (PLAN 3.4, 5.3).

Точки — `office`, стартовые точки бригад (`start:<lat>,<lon>`) и `req:<id>` в порядке набора.
Времена целые секунды, расстояния целые метры.
"""

import logging
from dataclasses import dataclass, field
from hashlib import sha1
from math import asin, cos, radians, sin, sqrt
from time import sleep

import httpx

from .. import db
from ..config import settings
from ..dictionaries import Transport
from ..models import Dataset, Engineer, Input, Office
from . import valhalla
from .valhalla import PROFILE_BY_TRANSPORT, TRANSIT, TRANSIT_BASE, Point, Row

log = logging.getLogger(__name__)

OFFICE = "office"

# Запасной расчёт (PLAN 3.4): расстояние по прямой × 1.3 и городская скорость профиля.
# Скорость уже городская, поэтому коэффициент пробок к ней второй раз не применяется.
DETOUR = 1.3
FALLBACK_SPEED_KMH = {
    "auto": 30.0,
    "bicycle": 15.0,
    "pedestrian": 5.0,
    TRANSIT: 15.0,
}
EARTH_RADIUS_M = 6_371_000
# Повтор запроса (PLAN 5.6): разовый сбой связи — перезапуск контейнера, оборванное
# соединение — не должен превращать весь профиль в прямую. Пауза даёт Valhalla подняться.
# Цена: если Valhalla не отвечает совсем, профиль ждёт два таймаута вместо одного.
RETRIES = 1
RETRY_PAUSE_SEC = 2.0


def point_id(request_id: str) -> str:
    return f"req:{request_id}"


def start_id(office: Office, engineer: Engineer) -> str:
    """Стартовая точка бригады: офис или её «дом» в удалённом городе (PLAN 2.4, ответ 13).

    Бригада, стоящая на координатах офиса, даёт точку `office`, а одинаковые дома дают один
    идентификатор. Дедупликация не косметика: пока все стартуют из офиса, состав точек и
    отпечаток набора остаются прежними, и предпосчитанные таблицы (PLAN 5.7) продолжают
    находиться в кэше.
    """
    point = (round(engineer.start_lat, 6), round(engineer.start_lon, 6))
    if point == (round(office.lat, 6), round(office.lon, 6)):
        return OFFICE
    return f"start:{point[0]:.6f},{point[1]:.6f}"


def points(data: Input) -> list[tuple[str, float, float]]:
    """Офис, дома бригад, заявки в исходном порядке — этот порядок и есть индекс матрицы."""
    table = [(OFFICE, data.office.lat, data.office.lon)]
    seen = {OFFICE}
    for engineer in data.engineers:
        pid = start_id(data.office, engineer)
        if pid not in seen:
            seen.add(pid)
            table.append((pid, engineer.start_lat, engineer.start_lon))
    return table + [
        (point_id(request.id), request.lat, request.lon) for request in data.requests
    ]


def points_hash(points: list[tuple[str, float, float]]) -> str:
    """Отпечаток точек набора. Встроенный набор живёт под постоянным datasetId, и его
    содержимое меняется при reset и пересборке prepare_data.py: без отпечатка кэш молча
    описывал бы точки прошлого набора (PLAN 5.4)."""
    joined = ";".join(f"{pid},{lat},{lon}" for pid, lat, lon in points)
    return sha1(joined.encode()).hexdigest()


def profiles(data: Input) -> list[str]:
    """Профили транспорта бригад набора — то, что лежит в таблицах (PLAN 3.4)."""
    return list(
        dict.fromkeys(
            PROFILE_BY_TRANSPORT[engineer.transport] for engineer in data.engineers
        )
    )


def base_profiles(wanted: list[str]) -> list[str]:
    """Что из этого списка считает Valhalla: общественный транспорт заменяется своими основами.

    Набор с бригадой на общественном транспорте требует `auto`, даже если машин в нём нет:
    производный профиль считается из пешехода и автомобиля (PLAN 3.3).
    """
    needed: list[str] = []
    for profile in wanted:
        for base in TRANSIT_BASE if profile == TRANSIT else (profile,):
            if base not in needed:
                needed.append(base)
    return needed


@dataclass
class Matrix:
    profile: str
    point_ids: list[str]
    durations: list[Row]  # секунды
    distances: list[Row]  # метры


@dataclass
class Travel:
    """Таблицы переездов одного набора — все профили сразу."""

    dataset_id: str
    points: list[tuple[str, float, float]]
    matrices: dict[str, Matrix]
    # Профили, посчитанные по прямой: Valhalla не ответила. Хранится список, а не «да/нет»:
    # план и помечается `approximate`, и называет диспетчеру транспорт, чьи километры и
    # времена — оценка, а не дороги (PLAN 5.6).
    approximate_profiles: set[str] = field(default_factory=set)
    index: dict[str, int] = field(init=False)

    def __post_init__(self) -> None:
        self.index = {pid: i for i, (pid, _, _) in enumerate(self.points)}

    @property
    def approximate(self) -> bool:
        return bool(self.approximate_profiles)

    def travel(
        self, transport: Transport, from_id: str, to_id: str
    ) -> tuple[int, int] | None:
        """Секунды и метры переезда. `None` — пути нет, ехать нельзя (PLAN 5.2, 5.8)."""
        matrix = self.matrices[PROFILE_BY_TRANSPORT[transport]]
        i, j = self.index[from_id], self.index[to_id]
        seconds, meters = matrix.durations[i][j], matrix.distances[i][j]
        return None if seconds is None or meters is None else (seconds, meters)

    def sync_point(self, pid: str, lat: float, lon: float) -> None:
        """Точка есть в таблицах и стоит на этих координатах (PLAN 6.19).

        Новую считает, **переехавшую пересчитывает**. Идентификатор заявки (`req:<id>`)
        от адреса не зависит, поэтому правка адреса меняет координаты, не меняя ключа:
        без этой проверки в таблице остались бы плечи до прежнего адреса, а точка набора
        уже описывала бы новый — и километры, метрики и независимая проверка дружно
        сошлись бы на неправильных числах.
        """
        current = self.index.get(pid)
        if current is not None:
            _, old_lat, old_lon = self.points[current]
            # Сравниваем так же, как строится идентификатор стартовой точки (`start_id`).
            if (round(old_lat, 6), round(old_lon, 6)) == (round(lat, 6), round(lon, 6)):
                return
            self.drop_point(pid)
        self.add_point(pid, lat, lon)

    def drop_point(self, pid: str) -> None:
        """Убирает точку из всех таблиц: её плечи устарели и переиспользовать их нельзя."""
        position = self.index[pid]
        del self.points[position]
        for matrix in self.matrices.values():
            del matrix.point_ids[position]
            del matrix.durations[position]
            del matrix.distances[position]
            for durations, distances in zip(
                matrix.durations, matrix.distances, strict=True
            ):
                del durations[position]
                del distances[position]
        self.index = {point[0]: i for i, point in enumerate(self.points)}

    def add_point(self, new_id: str, lat: float, lon: float) -> None:
        """Строка и столбец новой точки: два запроса на профиль, а не пересчёт всего (PLAN 5.5).

        Точка может быть и заявкой из события, и стартом бригады, вышедшей в течение дня.
        Общественный транспорт своего запроса не делает: его плечи выводятся из пешеходных
        и автомобильных, посчитанных здесь же.
        """
        if new_id in self.index:
            raise ValueError(f"точка {new_id} уже есть в таблице")

        new = (lat, lon)
        old = [(point[1], point[2]) for point in self.points]
        legs: dict[str, tuple[Row, Row, Row, Row, bool]] = {}
        for profile in base_profiles(list(self.matrices)):
            row_sec, row_m, from_new = _block([new], old + [new], profile)
            col_sec, col_m, to_new = _block(old, [new], profile)
            legs[profile] = (
                row_sec[0],
                row_m[0],
                [row[0] for row in col_sec],
                [row[0] for row in col_m],
                from_new or to_new,
            )

        for profile, matrix in self.matrices.items():
            row_sec, row_m, col_sec, col_m, approximate = (
                _transit_legs(legs) if profile == TRANSIT else legs[profile]
            )
            if approximate:
                self.approximate_profiles.add(profile)
            for i in range(len(old)):
                matrix.durations[i].append(col_sec[i])
                matrix.distances[i].append(col_m[i])
            matrix.durations.append(row_sec)
            matrix.distances.append(row_m)
            matrix.point_ids.append(new_id)

        self.points.append((new_id, lat, lon))
        self.index[new_id] = len(self.points) - 1
        # В кэш досчитанная таблица не пишется: кэш описывает точки набора, а заявки из
        # события в наборе нет. Её отпечаток не совпал бы с набором, и следующий `get` всё
        # равно пересчитал бы всё заново, затерев по дороге готовые матрицы встроенного
        # набора. Повторить досчёт стоит два запроса на профиль — дешевле, чем это.
        # Точку, которая меняет сам набор (PLAN 6.19), сохраняет `extend`, а не этот метод.


def build(dataset: Dataset) -> Travel:
    """Считает все нужные профили через Valhalla. Без Mongo — этим же пользуется prepare_data.py.

    Сначала считаются профили, которые умеет Valhalla, затем из пешехода и автомобиля
    выводится общественный транспорт (PLAN 3.3). Вспомогательный профиль, которым в наборе
    никто не ездит, в таблицах не остаётся: он не нужен ни расчёту, ни кэшу.
    """
    table = points(dataset)
    coordinates = [(lat, lon) for _, lat, lon in table]
    ids = [pid for pid, _, _ in table]
    wanted = profiles(dataset)
    travel = Travel(dataset_id=dataset.id, points=table, matrices={})
    for profile in base_profiles(wanted):
        durations, distances, approximate = _block(coordinates, coordinates, profile)
        travel.matrices[profile] = Matrix(profile, list(ids), durations, distances)
        if approximate:
            travel.approximate_profiles.add(profile)

    if TRANSIT in wanted:
        walk, car = (travel.matrices[base] for base in TRANSIT_BASE)
        travel.matrices[TRANSIT] = transit(walk, car, ids)
        if travel.approximate_profiles & set(TRANSIT_BASE):
            travel.approximate_profiles.add(TRANSIT)

    travel.matrices = {name: travel.matrices[name] for name in wanted}
    travel.approximate_profiles &= set(wanted)
    return travel


def transit(walk: Matrix, car: Matrix, ids: list[str]) -> Matrix:
    """Общественный транспорт из пешехода и автомобиля — усреднённой оценкой (PLAN 3.3)."""
    durations: list[Row] = []
    distances: list[Row] = []
    for walk_sec, walk_m, car_sec, car_m in zip(
        walk.durations, walk.distances, car.durations, car.distances, strict=True
    ):
        legs = [
            _transit_leg(*values)
            for values in zip(walk_sec, walk_m, car_sec, car_m, strict=True)
        ]
        durations.append([seconds for seconds, _ in legs])
        distances.append([meters for _, meters in legs])
    return Matrix(TRANSIT, list(ids), durations, distances)


def _transit_leg(
    walk_sec: int | None, walk_m: int | None, car_sec: int | None, car_m: int | None
) -> tuple[int | None, int | None]:
    """Одно плечо: короткое идётся пешком, длинное — тем, что быстрее (PLAN 3.3, ответ 14).

    Расстояние берётся у того плеча, чьё время выбрали: километры и время должны описывать
    один и тот же способ добраться, иначе метрики плана перестанут сходиться с расписанием.
    """
    walk = (walk_sec, walk_m)
    if (
        walk_sec is not None
        and walk_m is not None
        and walk_m <= settings.transit_walk_m
    ):
        return walk
    if car_sec is None or car_m is None:  # машиной не проехать — остаётся пешком
        return walk
    ride = round(car_sec * settings.transit_factor + settings.transit_wait_sec)
    if walk_sec is not None and walk_sec <= ride:
        return walk
    return ride, car_m


def _transit_legs(
    legs: dict[str, tuple[Row, Row, Row, Row, bool]],
) -> tuple[Row, Row, Row, Row, bool]:
    """То же для строки и столбца досчитанной точки (`add_point`)."""
    walk_row_sec, walk_row_m, walk_col_sec, walk_col_m, walk_approximate = legs[
        TRANSIT_BASE[0]
    ]
    car_row_sec, car_row_m, car_col_sec, car_col_m, car_approximate = legs[
        TRANSIT_BASE[1]
    ]
    row = [
        _transit_leg(*values)
        for values in zip(walk_row_sec, walk_row_m, car_row_sec, car_row_m, strict=True)
    ]
    column = [
        _transit_leg(*values)
        for values in zip(walk_col_sec, walk_col_m, car_col_sec, car_col_m, strict=True)
    ]
    return (
        [seconds for seconds, _ in row],
        [meters for _, meters in row],
        [seconds for seconds, _ in column],
        [meters for _, meters in column],
        walk_approximate or car_approximate,
    )


def get(dataset: Dataset) -> Travel:
    """Таблицы набора: из кэша, иначе считаем и кэшируем. Точка входа для расчёта плана.

    Кэш ищется по отпечатку точек (PLAN 5.4): таблица описывает точки, а не набор. Поэтому
    повторный импорт того же файла берёт готовое, и предпосчитанные таблицы примеров
    (`data/examples/*-matrices.json`) подходят набору с новым идентификатором.
    """
    table = points(dataset)
    digest = points_hash(table)
    cached: dict[str, Matrix] = {}
    for profile in profiles(dataset):
        document = db.matrices.find_one({"pointsHash": digest, "profile": profile})
        if document is None:
            break
        cached[profile] = from_document(document)
    else:
        return Travel(dataset.id, table, cached)

    travel = build(dataset)
    if not travel.approximate:
        save(travel)
    return travel


def ensure_profiles(travel: Travel, data: Input) -> None:
    """Досчитывает профили, которых в таблицах нет (PLAN 6.19).

    Случай один: бригада вышла в течение дня на транспорте, которым в наборе никто не
    ездил, — в таблицах такого профиля просто нет, и первый же переезд ронял бы расчёт
    `KeyError` в 500. Считается он на уже известных точках, одним запросом на профиль;
    если Valhalla не ответила, профиль уходит по прямой и план честно помечается
    `approximate`, как и везде (PLAN 5.6).

    Правится сам `travel`: и здесь, и в `add_point` таблицы дополняются по месту, а не
    пересобираются, — иначе досчитанные точки события потерялись бы.
    """
    missing = [profile for profile in profiles(data) if profile not in travel.matrices]
    if not missing:
        return

    coordinates = [(lat, lon) for _, lat, lon in travel.points]
    ids = [pid for pid, _, _ in travel.points]
    computed: dict[str, Matrix] = {}
    approximate: set[str] = set()
    for base in base_profiles(missing):
        if base in travel.matrices:
            computed[base] = travel.matrices[base]
            if base in travel.approximate_profiles:
                approximate.add(base)
            continue
        durations, distances, straight = _block(coordinates, coordinates, base)
        computed[base] = Matrix(base, list(ids), durations, distances)
        if straight:
            approximate.add(base)

    for profile in missing:
        if profile == TRANSIT:
            walk, car = (computed[base] for base in TRANSIT_BASE)
            travel.matrices[TRANSIT] = transit(walk, car, ids)
            straight = bool(approximate & set(TRANSIT_BASE))
        else:
            travel.matrices[profile] = computed[profile]
            straight = profile in approximate
        if straight:
            travel.approximate_profiles.add(profile)


def extend(travel: Travel, dataset: Dataset) -> Travel:
    """Таблицы набора после его правки (PLAN 6.19). `travel` — таблицы набора **до** правки.

    Отличие от `add_point`: там точка живёт только в плане и в кэш не попадает, здесь
    изменился сам набор, поэтому таблица обязана пережить перезапуск. Пересчёта через
    Valhalla не происходит — считаются строка и столбец новой точки, остальное берётся из
    прежней таблицы и переставляется под новый порядок точек. Точка, которой в наборе больше
    нет, из таблицы выпадает.

    Транспорт, которым в наборе раньше никто не ездил, — единственный случай, когда таблицы
    считаются целиком: строки для его профиля просто нет.
    """
    table = points(dataset)
    if set(profiles(dataset)) - set(travel.matrices):
        return get(dataset)

    for pid, lat, lon in table:
        # Не `add_point`: заявка могла не появиться, а переехать — идентификатор тот же,
        # координаты другие (PLAN 6.19, правка адреса).
        travel.sync_point(pid, lat, lon)

    order = [travel.index[pid] for pid, _, _ in table]
    ids = [pid for pid, _, _ in table]
    extended = Travel(
        dataset_id=dataset.id,
        points=table,
        matrices={
            profile: Matrix(
                profile,
                list(ids),
                [[matrix.durations[i][j] for j in order] for i in order],
                [[matrix.distances[i][j] for j in order] for i in order],
            )
            for profile, matrix in travel.matrices.items()
        },
        approximate_profiles=set(travel.approximate_profiles),
    )
    if not extended.approximate:
        save(extended)
    return extended


def save(travel: Travel) -> None:
    """Пишет по отпечатку точек: набор, которому таблица досталась первым, в ключ не входит."""
    digest = points_hash(travel.points)
    for matrix in travel.matrices.values():
        db.matrices.replace_one(
            {"pointsHash": digest, "profile": matrix.profile},
            to_document(travel.dataset_id, digest, matrix),
            upsert=True,
        )


def to_document(dataset_id: str, digest: str, matrix: Matrix) -> dict:
    return {
        "datasetId": dataset_id,
        "profile": matrix.profile,
        "pointsHash": digest,
        "pointIds": matrix.point_ids,
        "durationsSec": matrix.durations,
        "distancesM": matrix.distances,
    }


def from_document(document: dict) -> Matrix:
    return Matrix(
        document["profile"],
        document["pointIds"],
        document["durationsSec"],
        document["distancesM"],
    )


def _block(
    sources: list[Point], targets: list[Point], profile: str
) -> tuple[list[Row], list[Row], bool]:
    """Кусок таблицы источников на цели, при нужде — несколькими запросами.

    Valhalla отклоняет запрос больше `max_matrix_location_pairs` пар, а набор в сотню
    заявок его превышает (PLAN 5.6). Режем по источникам: по целям упёрлись бы только
    на десяти тысячах адресов, столько геокодировать всё равно нечем.

    Если Valhalla отказала хотя бы на одном куске, по прямой считается **вся** таблица.
    Смесь настоящих и прямых плеч хуже целиком приблизительной: прямая систематически
    короче дороги, и оптимизатор сваливал бы работу на бригады из неудавшегося куска.
    """
    step = max(1, settings.valhalla_max_matrix_pairs // max(1, len(targets)))
    durations: list[Row] = []
    distances: list[Row] = []
    approximate = False
    for start in range(0, len(sources), step):
        chunk = sources[start : start + step]
        chunk_durations, chunk_distances, chunk_approximate = _ask(
            chunk, targets, profile
        )
        durations += chunk_durations
        distances += chunk_distances
        approximate = approximate or chunk_approximate
    if approximate and len(sources) > step:
        return *_straight(sources, targets, profile), True
    return durations, distances, approximate


def _ask(
    sources: list[Point], targets: list[Point], profile: str
) -> tuple[list[Row], list[Row], bool]:
    """Запрос с повтором: секунды, метры и признак «считали по прямой».

    По прямой считаем, только когда Valhalla не может ответить: не подняли, не дождались,
    5xx. Ответ 4xx — это наш неверный запрос (превышен лимит пар или дальность профиля из
    `valhalla.json`), и он обязан упасть: молчаливая прямая на весь профиль спрятала бы
    ошибку, которую надо чинить запросом, а не приближением (PLAN 3.4, риски 10).

    Перед тем как сдаться, запрос повторяется `RETRIES` раз: одиночный обрыв связи или
    перезапуск контейнера переживаются сами собой, и целый профиль из-за них в прямую не
    уходит. Повторяется и таймаут — по нему не отличить зависшую Valhalla от медленного,
    но идущего расчёта (PLAN 5.6).
    """
    for attempt in range(RETRIES + 1):
        try:
            durations, distances = valhalla.matrix(sources, targets, profile)
        except httpx.HTTPStatusError as error:
            if error.response.status_code < 500:
                raise
            unavailable = error
        except httpx.TransportError as error:
            unavailable = error
        else:
            return _traffic(profile, durations), distances, False

        if attempt < RETRIES:
            log.warning(
                "Valhalla не ответила (%s), профиль «%s», повторяем запрос",
                unavailable,
                profile,
            )
            sleep(RETRY_PAUSE_SEC)

    log.warning(
        "Valhalla недоступна (%s), профиль «%s» считаем по прямой", unavailable, profile
    )
    return *_straight(sources, targets, profile), True


def _traffic(profile: str, durations: list[Row]) -> list[Row]:
    """У Valhalla нет пробок, для автомобиля добавляем их коэффициентом (PLAN 3.3)."""
    if profile != "auto":
        return durations
    factor = settings.car_traffic_factor
    return [
        [None if value is None else round(value * factor) for value in row]
        for row in durations
    ]


def _straight(
    sources: list[Point], targets: list[Point], profile: str
) -> tuple[list[Row], list[Row]]:
    speed = FALLBACK_SPEED_KMH[profile] * 1000 / 3600  # м/с
    distances = [
        [round(haversine(source, target) * DETOUR) for target in targets]
        for source in sources
    ]
    durations = [[round(meters / speed) for meters in row] for row in distances]
    return durations, distances


def haversine(a: Point, b: Point) -> float:
    """Расстояние по прямой в метрах."""
    lat1, lon1, lat2, lon2 = (radians(value) for value in (*a, *b))
    chord = (
        sin((lat2 - lat1) / 2) ** 2
        + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * asin(sqrt(chord))
