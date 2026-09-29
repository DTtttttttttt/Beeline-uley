"""Демо-наборы: заявки и составы инженеров по отдельности (PLAN 6.20).

Valhalla в этих тестах запрещена: обещание демо — любая пара считается по готовым таблицам.
Mongo нужна только тестам эндпоинтов.
"""

import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.data import seed
from app.main import app
from app.models import Dataset
from app.routing import matrices, valhalla

PAIRS = [
    (requests_id, crew_id)
    for requests_id in seed.DEMO_REQUESTS
    for crew_id in seed.DEMO_CREWS
]


def demo(requests_id: str, crew_id: str) -> Dataset:
    return Dataset(
        id=f"demo:{requests_id}:{crew_id}",
        created_at=datetime.now(UTC),
        **seed.demo_raw(requests_id, crew_id),
    )


def test_default_pair_is_the_builtin_dataset():
    """Первые 12 инженеров списка — бригады встроенного набора. Если наборы пересобрали,
    а `--crews` забыли, «Восток» со штатным составом перестал бы быть встроенным."""
    builtin = seed.load_builtin()
    pair = demo(*seed.DEMO_DEFAULT)

    assert pair.engineers == builtin.engineers
    assert pair.requests == builtin.requests
    assert pair.office == builtin.office


def test_crews_are_prefixes_of_one_list():
    """Составы — те же люди в разном числе: сокращённый входит в штатный, штатный — в
    расширенный. Иначе сравнение составов сравнивало бы разных людей."""
    ids = {
        crew_id: [engineer.id for engineer in demo("vostok", crew_id).engineers]
        for crew_id in seed.DEMO_CREWS
    }
    assert ids["staff"][: len(ids["reduced"])] == ids["reduced"]
    assert ids["extended"][: len(ids["staff"])] == ids["staff"]
    assert [len(ids[crew_id]) for crew_id in seed.DEMO_CREWS] == list(
        seed.DEMO_CREWS.values()
    )


@pytest.mark.parametrize(("requests_id", "crew_id"), PAIRS)
def test_every_pair_has_precomputed_travel_tables(requests_id, crew_id):
    """Точки пары совпадают с точками файла заявок, поэтому таблицы переездов посчитаны
    заранее по всем её профилям. Разъедься они (дом не в центре кластера, состав без
    машин) — выбор пары на защите ушёл бы в Valhalla за тысячами пар."""
    pair = demo(requests_id, crew_id)
    ready = {
        (document["pointsHash"], document["profile"])
        for document in seed.load_matrices() + seed.load_example_matrices()
    }
    digest = matrices.points_hash(matrices.points(pair))

    assert {(digest, profile) for profile in matrices.profiles(pair)} <= ready


def test_engineers_start_from_the_office_of_chosen_requests():
    """Состав не привязан к участку: старт — офис выбранных заявок, а не «Востока»."""
    pair = demo("yugotsentr", "extended")

    assert {(e.start_lat, e.start_lon) for e in pair.engineers} == {
        (pair.office.lat, pair.office.lon)
    }


def test_remote_city_gets_homes_in_every_crew():
    """У «Юго-востока» Кашира и Ступино: в любом составе часть машин живёт там (ответ 13)."""
    for crew_id in seed.DEMO_CREWS:
        pair = demo("yugo-vostok", crew_id)
        homes = [
            e
            for e in pair.engineers
            if (e.start_lat, e.start_lon) != (pair.office.lat, pair.office.lon)
        ]
        assert homes, crew_id
        assert all(e.transport == "car" for e in homes)


# --- эндпоинты ---------------------------------------------------------------


@pytest.fixture
def client(monkeypatch, mongo):
    def forbidden(*args, **kwargs):
        raise AssertionError("демо-набор не должен ходить в Valhalla")

    monkeypatch.setattr(valhalla, "matrix", forbidden)
    seed.ensure_seeded()
    return TestClient(app)


def test_catalog_lists_requests_and_crews(client):
    catalog = client.get("/api/dataset/demos").json()

    assert [(item["id"], item["count"]) for item in catalog["requests"]] == [
        ("vostok", 66),
        ("yugo-vostok", 83),
        ("yugotsentr", 56),
    ]
    assert [(item["id"], item["count"]) for item in catalog["crews"]] == [
        ("reduced", 9),
        ("staff", 12),
        ("extended", 16),
    ]
    for crew in catalog["crews"]:
        assert set(crew["transports"]) == {"car", "bicycle", "public_transport"}
        assert set(crew["skills"]) == {"local", "connection", "emergency"}


def test_default_pair_resets_to_the_builtin(client):
    chosen = client.post(
        "/api/dataset/demo", json={"requests": "vostok", "crew": "staff"}
    ).json()

    assert chosen["id"] == seed.BUILTIN_ID
    assert chosen["control"] is not None  # факт того же дня остаётся со встроенным


def test_chosen_pair_becomes_current_and_is_not_duplicated(client, mongo):
    counts = []
    for _ in range(2):
        response = client.post(
            "/api/dataset/demo", json={"requests": "yugotsentr", "crew": "reduced"}
        )
        assert response.status_code == 200
        counts.append(mongo.datasets.count_documents({}))
    chosen = response.json()

    # Повторный выбор перезаписывает пару, а не кладёт рядом копию под новым id.
    assert counts[0] == counts[1]

    assert chosen["id"] == "demo:yugotsentr:reduced"
    assert chosen["name"] == "Югоцентр · сокращённый состав"
    assert (len(chosen["requests"]), len(chosen["engineers"])) == (56, 9)
    assert chosen["builtIn"] is True
    assert client.get("/api/dataset").json()["id"] == chosen["id"]
    # «Вернуть демонстрационный набор» по-прежнему возвращает встроенный.
    assert client.post("/api/dataset/reset").json()["id"] == seed.BUILTIN_ID


def test_chosen_pair_is_planned_without_valhalla(client):
    """Таблицы берутся из кэша: запрещённая Valhalla не вызвана, числа не по прямой."""
    client.post(
        "/api/dataset/demo", json={"requests": "yugo-vostok", "crew": "extended"}
    )

    travel = matrices.get(seed.current())

    assert travel.approximate is False


def test_unknown_pair_is_404(client):
    response = client.post(
        "/api/dataset/demo", json={"requests": "vostok", "crew": "нет-такого"}
    )

    assert response.status_code == 404
    assert "нет-такого" in response.json()["detail"]


def test_crews_file_has_no_start_points():
    """Старт инженеров в `data/seed/crews.json` не записан: его даёт офис выбранных заявок."""
    pool = json.loads((settings.data_dir / "seed" / "crews.json").read_text("utf-8"))

    assert all("startLat" not in engineer for engineer in pool)
    assert len(pool) == max(seed.DEMO_CREWS.values())
