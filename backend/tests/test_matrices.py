"""Таблицы переездов (PLAN 3.4, 5.3, блок 5).

Valhalla не вызывается: разбор ответа проверяется на httpx.MockTransport, остальное — на
подмене `valhalla.matrix`. Кэш проверяется на живой Mongo в отдельной базе `planner-test`.
"""

import json
from datetime import UTC, datetime

import httpx
import pytest

from app.config import settings
from app.data import seed
from app.dictionaries import Transport
from app.models import Dataset
from app.routing import matrices, valhalla

OFFICE = {"address": "офис", "lat": 55.70, "lon": 37.77}
REQUEST = {
    "id": "1",
    "address": "адрес",
    "lat": 55.71,
    "lon": 37.78,
    "serviceDurationMin": 70,
    "baseNormMin": 90,
    "windowStart": "09:00",
    "windowEnd": "11:00",
    "workPriority": "regular",
    "skill": "local",
}
ENGINEER = {
    "id": "brigade-1",
    "name": "Бригада 1",
    "startLat": 55.70,
    "startLon": 37.77,
    "shiftStart": "09:00",
    "shiftEnd": "18:00",
    "skills": ["local"],
    "transport": "car",
    "equipment": [],
}


def dataset(requests=1, transports=("car",), dataset_id="test") -> Dataset:
    return Dataset(
        id=dataset_id,
        name="набор",
        created_at=datetime.now(UTC),
        office=OFFICE,
        requests=[
            REQUEST | {"id": str(number), "lat": 55.71 + number / 100}
            for number in range(1, requests + 1)
        ],
        engineers=[
            ENGINEER | {"id": f"brigade-{number}", "transport": transport}
            for number, transport in enumerate(transports, start=1)
        ],
    )


class FakeValhalla:
    """Возвращает предсказуемые значения и считает запросы: секунды = 100, метры = 1000."""

    def __init__(self, unreachable: set[tuple[int, int]] = frozenset()):
        self.calls: list[tuple[int, int, str]] = []
        self.unreachable = unreachable

    def __call__(self, sources, targets, costing):
        self.calls.append((len(sources), len(targets), costing))
        durations, distances = [], []
        for i in range(len(sources)):
            durations.append(
                [
                    None if (i, j) in self.unreachable else 100
                    for j in range(len(targets))
                ]
            )
            distances.append(
                [
                    None if (i, j) in self.unreachable else 1000
                    for j in range(len(targets))
                ]
            )
        return durations, distances


@pytest.fixture
def fake(monkeypatch):
    stub = FakeValhalla()
    monkeypatch.setattr(valhalla, "matrix", stub)
    return stub


# --- точки и профили ---------------------------------------------------------


def test_points_office_first_then_requests_in_order():
    assert [pid for pid, _, _ in matrices.points(dataset(requests=2))] == [
        "office",
        "req:1",
        "req:2",
    ]


def test_profiles_only_from_engineers_transports():
    assert matrices.profiles(dataset(transports=("bicycle", "car"))) == [
        "bicycle",
        "auto",
    ]


def test_public_transport_is_a_profile_of_its_own():
    """Ответ 14: общественный транспорт — свой профиль, усреднённое время (PLAN 3.3)."""
    data = dataset(transports=("public_transport",))
    assert matrices.profiles(data) == ["public_transport"]
    # Valhalla считает пешехода и автомобиль: из них и выводится общественный транспорт,
    # даже если машин в наборе нет.
    assert matrices.base_profiles(matrices.profiles(data)) == ["pedestrian", "auto"]


def test_points_hash_changes_with_coordinates():
    moved = dataset()
    before = matrices.points_hash(matrices.points(moved))
    moved.requests[0].lat += 0.001
    assert matrices.points_hash(matrices.points(moved)) != before


# --- построение --------------------------------------------------------------


def test_build_applies_traffic_factor_to_car_only(fake):
    travel = matrices.build(dataset(transports=("car", "bicycle")))
    assert travel.matrices["auto"].durations[0][1] == round(
        100 * settings.car_traffic_factor
    )
    assert travel.matrices["bicycle"].durations[0][1] == 100


def test_build_makes_one_request_per_profile_over_all_points(fake):
    matrices.build(dataset(requests=2, transports=("car",)))
    assert fake.calls == [(3, 3, "auto")]


def test_build_keeps_integers(fake):
    travel = matrices.build(dataset())
    seconds, meters = travel.travel(Transport.CAR, "office", "req:1")
    assert isinstance(seconds, int) and isinstance(meters, int)


def test_unreachable_pair_stays_none(monkeypatch):
    monkeypatch.setattr(valhalla, "matrix", FakeValhalla(unreachable={(0, 1)}))
    travel = matrices.build(dataset(transports=("bicycle",)))
    assert travel.matrices["bicycle"].durations[0][1] is None
    assert travel.travel(Transport.BICYCLE, "office", "req:1") is None


def test_travel_reads_pedestrian_matrix_for_public_transport(fake):
    travel = matrices.build(dataset(transports=("public_transport",)))
    assert travel.travel(Transport.PUBLIC_TRANSPORT, "office", "req:1") == (100, 1000)


# --- разбор ответа Valhalla --------------------------------------------------


def test_matrix_parses_response_and_converts_units(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/sources_to_targets"
        return httpx.Response(
            200,
            json={
                "sources_to_targets": {
                    "durations": [[0, 812.4], [810.6, None]],
                    "distances": [[0, 6.14], [6.1, None]],
                }
            },
        )

    monkeypatch.setattr(
        valhalla,
        "client",
        httpx.Client(
            transport=httpx.MockTransport(handler), base_url="http://valhalla:8002"
        ),
    )
    durations, distances = valhalla.matrix(
        [(55.70, 37.77), (55.71, 37.78)], [(55.70, 37.77), (55.71, 37.78)], "auto"
    )
    assert durations == [[0, 812], [811, None]]
    assert distances == [[0, 6140], [6100, None]]  # километры -> целые метры


# --- досчёт точки ------------------------------------------------------------


def test_add_point_adds_row_and_column_with_two_requests(fake, mongo):
    data = dataset(requests=1)
    travel = matrices.get(data)
    fake.calls.clear()
    new = dataset(requests=2).requests[1]
    travel.add_point(matrices.point_id(new.id), new.lat, new.lon)

    assert fake.calls == [(1, 3, "auto"), (2, 1, "auto")]  # новая -> все, все -> новая
    matrix = travel.matrices["auto"]
    assert matrix.point_ids == ["office", "req:1", "req:2"]
    assert len(matrix.durations) == 3 and all(len(row) == 3 for row in matrix.durations)
    assert travel.travel(Transport.CAR, "office", "req:2") == (130, 1000)
    assert travel.travel(Transport.CAR, "req:2", "office") == (130, 1000)

    # Кэш описывает точки набора, срочной заявки в нём нет: досчёт его не трогает.
    document = mongo.matrices.find_one({"datasetId": "test", "profile": "auto"})
    assert document["pointIds"] == ["office", "req:1"]
    assert document["pointsHash"] == matrices.points_hash(matrices.points(data))


def test_only_the_failed_profile_is_approximate(monkeypatch):
    """Отказ одного профиля не делает приблизительными остальные (PLAN 5.6)."""
    working = FakeValhalla()

    def half_broken(sources, targets, profile):
        if profile == "bicycle":
            raise httpx.ReadTimeout("не дождались")
        return working(sources, targets, profile)

    monkeypatch.setattr(valhalla, "matrix", half_broken)
    travel = matrices.build(dataset(transports=("car", "bicycle")))

    assert travel.approximate_profiles == {"bicycle"}
    # Автомобиль остался настоящим: 100 с от Valhalla с коэффициентом пробок, а не прямая.
    assert travel.travel(Transport.CAR, "office", "req:1") == (
        round(100 * settings.car_traffic_factor),
        1000,
    )


def test_add_point_rejects_existing_point(fake):
    travel = matrices.build(dataset(requests=1))
    with pytest.raises(ValueError):
        travel.add_point("req:1", 55.7, 37.7)


# --- запасной расчёт ---------------------------------------------------------


def test_fallback_marks_plan_approximate_and_uses_straight_line(monkeypatch):
    def unavailable(*args, **kwargs):
        raise httpx.ConnectError("нет соединения")

    monkeypatch.setattr(valhalla, "matrix", unavailable)
    travel = matrices.build(dataset(transports=("bicycle",)))

    # Профиль называется поимённо: по нему план объясняет диспетчеру, чьи цифры — оценка.
    assert travel.approximate_profiles == {"bicycle"}
    assert travel.approximate is True
    seconds, meters = travel.travel(Transport.BICYCLE, "office", "req:1")
    (_, office_lat, office_lon), (_, lat, lon) = travel.points
    straight = matrices.haversine((office_lat, office_lon), (lat, lon))
    assert meters == round(straight * matrices.DETOUR)
    assert seconds == round(meters / (matrices.FALLBACK_SPEED_KMH["bicycle"] / 3.6))


def test_a_single_failure_is_retried_instead_of_going_straight(monkeypatch):
    """Разовый обрыв связи переживается повтором: профиль остаётся настоящим (PLAN 5.6)."""
    working = FakeValhalla()
    calls = []

    def flaky(sources, targets, profile):
        calls.append(profile)
        if len(calls) == 1:
            raise httpx.ConnectError("контейнер перезапускается")
        return working(sources, targets, profile)

    monkeypatch.setattr(valhalla, "matrix", flaky)
    travel = matrices.build(dataset())

    assert calls == ["auto", "auto"]  # спросили дважды, второй раз удачно
    assert travel.approximate_profiles == set()


def test_straight_line_comes_only_after_the_retry_also_fails(monkeypatch):
    calls = []

    def unavailable(sources, targets, profile):
        calls.append(profile)
        raise httpx.ConnectError("нет соединения")

    monkeypatch.setattr(valhalla, "matrix", unavailable)
    travel = matrices.build(dataset())

    assert len(calls) == matrices.RETRIES + 1
    assert travel.approximate is True


def test_bad_request_is_not_retried(monkeypatch):
    """4xx — наш неверный запрос: повтор даст тот же ответ, падать надо сразу."""
    calls = []

    def bad_request(sources, targets, profile):
        calls.append(profile)
        request = httpx.Request("POST", "http://valhalla:8002/sources_to_targets")
        raise httpx.HTTPStatusError(
            "ошибка", request=request, response=httpx.Response(400, request=request)
        )

    monkeypatch.setattr(valhalla, "matrix", bad_request)
    with pytest.raises(httpx.HTTPStatusError):
        matrices.build(dataset())

    assert len(calls) == 1


def test_fallback_is_not_cached(monkeypatch, mongo):
    def unavailable(*args, **kwargs):
        raise httpx.ConnectError("нет соединения")

    monkeypatch.setattr(valhalla, "matrix", unavailable)
    matrices.get(dataset())
    assert mongo.matrices.count_documents({}) == 0


def test_server_error_falls_back_but_bad_request_raises(monkeypatch):
    def answer(code):
        def raise_status(*args, **kwargs):
            request = httpx.Request("POST", "http://valhalla:8002/sources_to_targets")
            raise httpx.HTTPStatusError(
                "ошибка",
                request=request,
                response=httpx.Response(code, request=request),
            )

        return raise_status

    # 503 — сервис не может ответить, это и есть «Valhalla недоступна».
    monkeypatch.setattr(valhalla, "matrix", answer(503))
    assert matrices.build(dataset()).approximate is True

    # 400 — превышен лимит пар или дальность профиля: чинить надо запрос, а не прятать прямой.
    monkeypatch.setattr(valhalla, "matrix", answer(400))
    with pytest.raises(httpx.HTTPStatusError):
        matrices.build(dataset())


# --- кэш ---------------------------------------------------------------------


def test_get_builds_once_and_then_reads_cache(fake, mongo):
    data = dataset(requests=2)
    matrices.get(data)
    assert fake.calls == [(3, 3, "auto")]

    document = mongo.matrices.find_one({"datasetId": "test", "profile": "auto"})
    assert document["pointIds"] == ["office", "req:1", "req:2"]
    assert document["pointsHash"] == matrices.points_hash(matrices.points(data))

    fake.calls.clear()
    travel = matrices.get(data)
    assert fake.calls == []  # второй раз Valhalla не тревожим
    assert travel.travel(Transport.CAR, "office", "req:2") == (130, 1000)


def test_changed_points_invalidate_cache(fake, mongo):
    data = dataset()
    matrices.get(data)
    fake.calls.clear()

    data.requests[0].lat += 0.01  # набор пересобрали под тем же datasetId
    matrices.get(data)
    assert fake.calls == [(2, 2, "auto")]
    # Прежняя таблица описывает другие точки: под новый отпечаток она не найдётся, и
    # новому набору соответствует ровно один документ.
    digest = matrices.points_hash(matrices.points(data))
    assert mongo.matrices.count_documents({"pointsHash": digest}) == 1


def test_same_points_under_another_dataset_id_reuse_the_cache(fake, mongo):
    """Импорт того же файла заводит набор с новым id — матрицы берутся готовые (PLAN 5.4).

    На этом держится предпосчёт примеров: таблицы посчитаны заранее, а идентификатор у
    загруженного набора каждый раз новый.
    """
    matrices.get(dataset(requests=2, dataset_id="first"))
    fake.calls.clear()

    travel = matrices.get(dataset(requests=2, dataset_id="second"))

    assert fake.calls == []
    assert travel.travel(Transport.CAR, "office", "req:2") == (130, 1000)
    assert mongo.matrices.count_documents({}) == 1  # одна копия на одинаковые точки


def test_example_matrices_are_seeded_from_files(fake, mongo, monkeypatch, tmp_path):
    """`data/examples/*-matrices.json` попадают в кэш при старте (PLAN 5.7)."""
    prepared = dataset(requests=2, dataset_id="example:набор")
    travel = matrices.build(prepared)
    digest = matrices.points_hash(travel.points)
    documents = [
        matrices.to_document(prepared.id, digest, matrix)
        for matrix in travel.matrices.values()
    ]
    examples = tmp_path / "examples"
    examples.mkdir()
    (examples / "набор-matrices.json").write_text(
        json.dumps(documents, ensure_ascii=False), "utf-8"
    )
    monkeypatch.setattr(settings, "data_dir", tmp_path)

    seed.seed_matrices()
    fake.calls.clear()
    travel = matrices.get(dataset(requests=2, dataset_id="загружен-заново"))

    assert fake.calls == []  # Valhalla не нужна: таблицы посчитаны заранее
    assert travel.approximate is False


def test_seed_puts_builtin_matrices_into_cache(mongo):
    # Набор уже лежит в базе (том Mongo с прошлых блоков), а матриц в ней ещё нет:
    # если класть их только вместе с набором, демо молча осталось бы без кэша.
    mongo.datasets.insert_one(seed.to_document(seed.load_builtin()))

    seed.ensure_seeded()
    profiles = mongo.matrices.distinct("profile", {"datasetId": seed.BUILTIN_ID})
    assert set(profiles) == {"auto", "bicycle", "pedestrian", "public_transport"}


@pytest.mark.parametrize("example", ["yugo-vostok", "yugotsentr"])
def test_examples_are_imported_without_valhalla(monkeypatch, mongo, example):
    """Пример загружается без Valhalla: таблицы из `data/examples` подходят его точкам.

    Набор при импорте получает новый идентификатор, поэтому проверяется именно поиск по
    отпечатку. Если пример пересобрали без `--matrices`, отпечаток не совпадёт, и тест
    поймает это раньше, чем импорт на защите уедет в многоминутный расчёт.
    """
    raw = json.loads(
        (settings.data_dir / "examples" / f"{example}.json").read_text("utf-8")
    )
    imported = Dataset(id="загружен-заново", created_at=datetime.now(UTC), **raw)

    def forbidden(*args, **kwargs):
        raise AssertionError(
            "Valhalla не должна понадобиться: матрицы лежат в data/examples"
        )

    seed.ensure_seeded()
    monkeypatch.setattr(valhalla, "matrix", forbidden)
    travel = matrices.get(imported)

    assert travel.approximate is False
    assert set(travel.matrices) == set(matrices.profiles(imported))


def test_builtin_dataset_is_planned_without_valhalla(monkeypatch, mongo):
    """Обещание «демо работает без Valhalla»: матрицы из data/seed подходят к набору.

    Если dataset.json пересобрали без `--matrices`, отпечаток не совпадёт, и этот тест
    поймает расхождение раньше, чем демо молча уедет в расчёт по прямой.
    """

    def forbidden(*args, **kwargs):
        raise AssertionError(
            "Valhalla не должна понадобиться: матрицы лежат в data/seed"
        )

    seed.ensure_seeded()
    monkeypatch.setattr(valhalla, "matrix", forbidden)
    travel = matrices.get(seed.load_builtin())
    assert travel.approximate is False
    # Пешехода среди них нет: это служебный профиль, из которого выводится общественный
    # транспорт (PLAN 3.3), а транспортом бригады он не бывает.
    assert set(travel.matrices) == {"auto", "bicycle", "public_transport"}


# --- стартовые точки бригад (PLAN 2.4, блок 24) ------------------------------


def home(data: Dataset, engineer_id: str, lat: float, lon: float) -> Dataset:
    """Селит бригаду вне офиса — «дом» в удалённом городе (ответ 13)."""
    for engineer in data.engineers:
        if engineer.id == engineer_id:
            engineer.start_lat, engineer.start_lon = lat, lon
    return data


def test_engineers_from_the_office_do_not_add_points():
    """Пока все стартуют из офиса, состав точек и отпечаток набора прежние (PLAN 5.7)."""
    data = dataset(requests=2, transports=("car", "bicycle"))
    assert [pid for pid, _, _ in matrices.points(data)] == ["office", "req:1", "req:2"]


def test_two_engineers_with_one_home_share_a_point():
    data = dataset(requests=1, transports=("car", "car", "bicycle"))
    home(data, "brigade-1", 54.84, 38.19)
    home(data, "brigade-2", 54.84, 38.19)

    table = matrices.points(data)

    assert [pid for pid, _, _ in table] == [
        "office",
        "start:54.840000,38.190000",
        "req:1",
    ]
    assert table[1][1:] == (54.84, 38.19)


def test_home_changes_the_points_hash():
    """Дом — новая точка таблицы, и предпосчитанная матрица под старые точки не подойдёт."""
    before = matrices.points_hash(matrices.points(dataset(requests=1)))
    moved = home(dataset(requests=1), "brigade-1", 54.84, 38.19)
    assert matrices.points_hash(matrices.points(moved)) != before


def test_build_counts_the_home_leg(fake):
    travel = matrices.build(home(dataset(requests=1), "brigade-1", 54.84, 38.19))

    assert fake.calls == [(3, 3, "auto")]  # офис, дом, заявка
    assert travel.travel(Transport.CAR, "start:54.840000,38.190000", "req:1") == (
        round(100 * settings.car_traffic_factor),
        1000,
    )


# --- общественный транспорт (PLAN 3.3, ответ 14) -----------------------------


def test_transit_walks_a_short_leg(monkeypatch):
    """Плечо короче TRANSIT_WALK_M идётся пешком — ни ожидания, ни пересадок."""
    monkeypatch.setattr(settings, "transit_walk_m", 1500)
    walk = matrices.Matrix("pedestrian", ["a", "b"], [[0, 800]], [[0, 1000]])
    car = matrices.Matrix("auto", ["a", "b"], [[0, 120]], [[0, 1200]])

    transit = matrices.transit(walk, car, ["a", "b"])

    assert transit.durations == [[0, 800]]
    assert transit.distances == [[0, 1000]]


def test_transit_rides_a_long_leg(monkeypatch):
    """Длинное плечо — время автомобиля с множителем плюс ожидание и пересадки."""
    monkeypatch.setattr(settings, "transit_walk_m", 1500)
    monkeypatch.setattr(settings, "transit_factor", 1.6)
    monkeypatch.setattr(settings, "transit_wait_sec", 720)
    walk = matrices.Matrix("pedestrian", ["a", "b"], [[0, 7200]], [[0, 10_000]])
    car = matrices.Matrix("auto", ["a", "b"], [[0, 600]], [[0, 11_000]])

    transit = matrices.transit(walk, car, ["a", "b"])

    assert transit.durations == [
        [0, round(600 * 1.6 + 720)]
    ]  # 1 680 с против 7 200 пешком
    assert transit.distances == [[0, 11_000]]  # километры того способа, чьё время взяли


def test_transit_is_never_slower_than_walking(monkeypatch):
    """Пробка не делает общественный транспорт медленнее пешехода: берём лучшее."""
    monkeypatch.setattr(settings, "transit_walk_m", 1500)
    walk = matrices.Matrix("pedestrian", ["a", "b"], [[0, 1800]], [[0, 2000]])
    car = matrices.Matrix("auto", ["a", "b"], [[0, 3600]], [[0, 2100]])

    transit = matrices.transit(walk, car, ["a", "b"])

    assert transit.durations == [[0, 1800]]


def test_transit_falls_back_to_walking_where_a_car_cannot_go():
    walk = matrices.Matrix("pedestrian", ["a", "b"], [[0, 9000]], [[0, 12_000]])
    car = matrices.Matrix("auto", ["a", "b"], [[0, None]], [[0, None]])

    transit = matrices.transit(walk, car, ["a", "b"])

    assert transit.durations == [[0, 9000]]
    assert transit.distances == [[0, 12_000]]


def test_transit_asks_for_auto_even_without_cars(fake):
    """Машин в наборе нет, но автомобильный профиль нужен как основа (PLAN 3.3)."""
    travel = matrices.build(dataset(transports=("public_transport",)))

    assert [costing for _, _, costing in fake.calls] == ["pedestrian", "auto"]
    # Вспомогательный профиль в таблицах не остаётся: им никто не ездит.
    assert set(travel.matrices) == {"public_transport"}


def test_transit_inherits_approximate_from_its_base(monkeypatch):
    """Пешеход посчитан по прямой — значит и общественный транспорт приблизителен."""
    working = FakeValhalla()

    def half_broken(sources, targets, profile):
        if profile == "pedestrian":
            raise httpx.ConnectError("нет соединения")
        return working(sources, targets, profile)

    monkeypatch.setattr(valhalla, "matrix", half_broken)
    travel = matrices.build(dataset(transports=("public_transport",)))

    assert travel.approximate_profiles == {"public_transport"}


# --- правка набора: extend (PLAN 6.19, блок 24.5) ----------------------------


def test_extend_counts_only_the_new_point_and_caches_it(fake, mongo):
    data = dataset(requests=1)
    travel = matrices.get(data)
    fake.calls.clear()

    grown = dataset(requests=2)
    extended = matrices.extend(travel, grown)

    # Два запроса на профиль: новая точка ко всем и все к ней — а не пересчёт таблицы.
    assert fake.calls == [(1, 3, "auto"), (2, 1, "auto")]
    assert [pid for pid, _, _ in extended.points] == ["office", "req:1", "req:2"]
    document = mongo.matrices.find_one(
        {"pointsHash": matrices.points_hash(matrices.points(grown)), "profile": "auto"}
    )
    assert document is not None
    assert document["pointIds"] == ["office", "req:1", "req:2"]


def test_extend_keeps_the_order_of_points_when_a_home_appears(monkeypatch, mongo):
    """Дом бригады встаёт между офисом и заявками, поэтому таблица переставляется.

    Чтобы перестановка была проверяемой, плечо зависит от координат цели: перепутанные
    строки и столбцы дали бы чужие числа, а не просто другую форму таблицы.
    """

    def by_target(sources, targets, costing):
        values = [[round((lat - 54) * 1000) for lat, _ in targets] for _ in sources]
        return values, [list(row) for row in values]

    monkeypatch.setattr(valhalla, "matrix", by_target)
    data = dataset(requests=2)
    travel = matrices.get(data)
    before = travel.travel(Transport.CAR, "office", "req:2")
    moved = home(dataset(requests=2), "brigade-1", 54.84, 38.19)

    extended = matrices.extend(travel, moved)

    assert [pid for pid, _, _ in extended.points] == [
        "office",
        "start:54.840000,38.190000",
        "req:1",
        "req:2",
    ]
    assert extended.matrices["auto"].point_ids == [pid for pid, _, _ in extended.points]
    assert extended.travel(Transport.CAR, "office", "req:2") == before
    assert extended.travel(Transport.CAR, "office", "start:54.840000,38.190000") == (
        round(840 * settings.car_traffic_factor),
        840,
    )


def test_extend_drops_a_removed_point(fake, mongo):
    data = dataset(requests=2)
    travel = matrices.get(data)
    fake.calls.clear()

    extended = matrices.extend(travel, dataset(requests=1))

    assert fake.calls == []  # убрать точку — не повод спрашивать Valhalla
    assert [pid for pid, _, _ in extended.points] == ["office", "req:1"]


# --- профиль, которого не было в наборе (PLAN 6.19, блок 24) -----------------


def test_ensure_profiles_counts_a_transport_nobody_used(fake):
    """Бригада вышла на транспорте, которым в наборе никто не ездил (ответ 13, 6.19).

    Раньше первый же её переезд ронял расчёт `KeyError` в 500: профиля в таблицах нет.
    """
    data = dataset(requests=1, transports=("car",))
    travel = matrices.build(data)
    assert set(travel.matrices) == {"auto"}
    fake.calls.clear()

    joined = data.model_copy(
        update={
            "engineers": [
                *data.engineers,
                data.engineers[0].model_copy(
                    update={"id": "brigade-9", "transport": Transport.BICYCLE}
                ),
            ]
        }
    )
    matrices.ensure_profiles(travel, joined)

    assert fake.calls == [(2, 2, "bicycle")]  # один запрос на недостающий профиль
    assert set(travel.matrices) == {"auto", "bicycle"}
    assert travel.travel(Transport.BICYCLE, "office", "req:1") == (100, 1000)


def test_ensure_profiles_derives_public_transport(fake):
    """Производный профиль досчитывается из своих основ, а не спрашивается у Valhalla."""
    data = dataset(requests=1, transports=("car",))
    travel = matrices.build(data)
    fake.calls.clear()

    joined = data.model_copy(
        update={
            "engineers": [
                *data.engineers,
                data.engineers[0].model_copy(
                    update={"id": "brigade-9", "transport": Transport.PUBLIC_TRANSPORT}
                ),
            ]
        }
    )
    matrices.ensure_profiles(travel, joined)

    # Пешеход считается, автомобиль берётся из уже готовой таблицы.
    assert [costing for _, _, costing in fake.calls] == ["pedestrian"]
    assert set(travel.matrices) == {"auto", "public_transport"}


def test_ensure_profiles_does_nothing_when_all_are_there(fake):
    data = dataset(requests=1, transports=("car",))
    travel = matrices.build(data)
    fake.calls.clear()

    matrices.ensure_profiles(travel, data)

    assert fake.calls == []


# --- переехавшая точка (ревью блока 28) --------------------------------------


def test_moved_point_is_recounted_instead_of_kept(fake):
    """Правка адреса: идентификатор прежний, координаты другие — плечи обязаны пересчитаться.

    `req:<id>` от адреса не зависит, и пропуск «точка уже есть» оставлял в таблице плечи до
    прежнего адреса, сохраняя их под отпечатком нового (PLAN 6.19).
    """
    data = dataset(requests=2)
    travel = matrices.build(data)
    fake.calls.clear()

    data.requests[0].lat += 1.0  # заявка уехала на сотню километров
    travel.sync_point("req:1", data.requests[0].lat, data.requests[0].lon)

    assert travel.points[travel.index["req:1"]] == (
        "req:1",
        data.requests[0].lat,
        data.requests[0].lon,
    )
    # Точка встала в конец, и столбцы таблиц переставились вместе с ней.
    assert [pid for pid, _, _ in travel.points] == ["office", "req:2", "req:1"]
    for matrix in travel.matrices.values():
        assert matrix.point_ids == ["office", "req:2", "req:1"]
        assert len(matrix.durations) == len(matrix.distances) == 3
        assert all(len(row) == 3 for row in matrix.durations)
    assert fake.calls  # Valhalla спрашивали заново


def test_point_on_the_same_place_is_not_recounted(fake):
    travel = matrices.build(dataset())
    fake.calls.clear()

    _, lat, lon = travel.points[travel.index["req:1"]]
    travel.sync_point("req:1", lat, lon)

    assert fake.calls == []
