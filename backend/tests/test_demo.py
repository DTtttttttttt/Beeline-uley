"""Демо-наборы: заявки и составы инженеров по отдельности (PLAN 6.20).

Valhalla в этих тестах запрещена: обещание демо — любая пара считается по готовым таблицам.
Mongo нужна только тестам эндпоинтов.
"""

import csv
import json
import random
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.data import seed
from app.dictionaries import (
    GENERATED_SHIFTS,
    SHIFT_PRESETS,
    Skill,
    Tool,
    Transport,
    WorkPriority,
)
from app.main import app
from app.models import Dataset
from app.routing import matrices, valhalla
from scripts import prepare_data

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


def test_shortage_is_a_training_crew_of_five():
    """Учебный состав (блок 55): первые пять того же списка, помечен — чек-лист и замеры
    README его пропускают, а окно «Новый датасет» подписывает."""
    crews = {crew.id: crew for crew in seed.demo_catalog().crews}

    assert crews["shortage"].training
    assert crews["shortage"].count == 5
    assert not any(crew.training for key, crew in crews.items() if key != "shortage")
    assert demo("vostok", "shortage").engineers == demo("vostok", "staff").engineers[:5]


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


def test_every_engineer_has_a_home_at_an_address_of_the_requests():
    """Дом у каждого (блок 73): адрес — адрес заявки участка, без запросов к геокодеру.

    Заселённый в удалённый город стартует из дома; остальные живут рядом с офисом и, как
    раньше, стартуют из офиса — точки пары и готовые таблицы переездов не меняются.
    """
    for requests_id in seed.DEMO_REQUESTS:
        pair = demo(requests_id, "extended")
        office = (pair.office.lat, pair.office.lon)
        places = {(r.address, r.lat, r.lon) for r in pair.requests}
        for engineer in pair.engineers:
            home = engineer.home
            assert (home.address, home.lat, home.lon) in places, engineer.id
            start = (engineer.start_lat, engineer.start_lon)
            if start != office:
                assert start == (home.lat, home.lon)
            else:
                assert matrices.haversine(office, (home.lat, home.lon)) <= seed.REMOTE_M


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
        ("shortage", 5),
    ]
    # Учебный состав чек-лист не проходит нарочно: общественного транспорта у первых пяти нет.
    assert [crew["training"] for crew in catalog["crews"]] == [False] * 3 + [True]
    for crew in catalog["crews"][:3]:
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
    # Составы берут начало списка, остальные — резерв «Рейтинга в приоритете» (блок 71).
    assert len(pool) == seed.CREW_POOL > max(seed.DEMO_CREWS.values())


def test_generated_shifts_follow_the_answer():
    """Смены — по ответу организаторов 29.09 (PLAN 2.4): генератор раздаёт 5/2 и основную 2/2.

    Проверяются все файлы, где лежат сгенерированные бригады: список составов, встроенный
    набор и оба примера для импорта, включая CSV. 09:30–23:00 — только пресет формы, и в
    данных его быть не должно.
    """
    allowed = {
        (SHIFT_PRESETS[preset]["start"], SHIFT_PRESETS[preset]["end"])
        for preset in GENERATED_SHIFTS
    }
    data = settings.data_dir
    shifts = {
        "crews.json": [(e["shiftStart"], e["shiftEnd"]) for e in seed.load_crew_pool()],
    }
    for name in (
        "seed/dataset.json",
        "examples/yugo-vostok.json",
        "examples/yugotsentr.json",
    ):
        engineers = json.loads((data / name).read_text("utf-8"))["engineers"]
        shifts[name] = [(e["shiftStart"], e["shiftEnd"]) for e in engineers]
    with (data / "examples" / "yugotsentr-engineers.csv").open(
        encoding="utf-8"
    ) as file:
        rows = list(csv.DictReader(file, delimiter=";"))
    shifts["yugotsentr-engineers.csv"] = [
        (r["shiftStart"], r["shiftEnd"]) for r in rows
    ]

    for name, found in shifts.items():
        assert found and set(found) <= allowed, name


def row(bk: str, hd: str) -> dict:
    return {
        "Заявка": "1",
        "Тип заявки BK": bk,
        "Тип заявки HD": hd,
        "Начало": "17.08.2026 10:00",
        "Окончание": "17.08.2026 12:00",
        "Адрес": "Москва, Тверская ул., 1",
    }


@pytest.mark.parametrize("emergency_by", ["bk", "hd"])
def test_emergency_by_hd_keeps_hd_emergencies(emergency_by):
    """«Глобальная проблема» с HD «Авария» — авария при любом флаге (ответ 29.09)."""
    request = prepare_data.map_request(
        row("Глобальная проблема", "Авария"), random.Random(1), emergency_by
    )
    assert request["workPriority"] == WorkPriority.EMERGENCY
    assert request["skill"] == Skill.EMERGENCY


def test_emergency_by_hd_turns_other_global_problems_local():
    """По HD «Глобальная проблема» без аварии в HD — локальная заявка, по BK — авария."""
    source = row("Глобальная проблема", "Информация")
    by_bk = prepare_data.map_request(source, random.Random(1), "bk")
    by_hd = prepare_data.map_request(source, random.Random(1), "hd")

    assert (by_bk["skill"], by_bk["workPriority"], by_bk["serviceDurationMin"]) == (
        Skill.EMERGENCY,
        WorkPriority.EMERGENCY,
        80,
    )
    assert by_bk["requiredTransport"] == Transport.CAR
    assert (by_hd["skill"], by_hd["workPriority"], by_hd["serviceDurationMin"]) == (
        Skill.LOCAL,
        WorkPriority.REGULAR,
        30,
    )
    assert by_hd["baseNormMin"] == 50
    assert by_hd["requiredTransport"] is None
    assert by_hd["requiredTools"] == [Tool.CABLE_TESTER]
    assert by_hd["hdType"] == "Информация"


@pytest.mark.parametrize(
    ("name", "emergencies"),
    [("Восток", (8, 3)), ("Юго-восток", (12, 12)), ("Югоцентр", (1, 0))],
)
def test_emergency_by_hd_changes_only_relabelled_requests(name, emergencies):
    """Флаг меняет только переразмеченные заявки: офис, точки, оборудование и бригады
    разыгрываются тем же потоком, поэтому таблицы переездов и составы прежние. Числа аварий —
    те, что названы в README («Замеры»)."""
    cache = json.loads(prepare_data.CACHE_PATH.read_text("utf-8"))

    def geocode(address):  # только кэш: 2ГИС в тестах не вызывается
        return tuple(cache[address])

    brigades = prepare_data.control_brigades(name)
    by_bk = prepare_data.build(name, geocode, brigades, 1, "bk")
    by_hd = prepare_data.build(name, geocode, brigades, 1, "hd")

    assert by_hd["office"] == by_bk["office"]
    assert by_hd["engineers"] == by_bk["engineers"]
    relabel = {
        "skill": Skill.LOCAL,
        "workPriority": WorkPriority.REGULAR,
        "serviceDurationMin": 30,
        "baseNormMin": 50,
        "requiredTransport": None,
        "requiredTools": [Tool.CABLE_TESTER],
    }
    for a, b in zip(by_bk["requests"], by_hd["requests"], strict=True):
        if a != b:
            assert a["hdType"] != "Авария" and b == a | relabel
    assert (
        tuple(
            sum(
                r["workPriority"] == WorkPriority.EMERGENCY for r in dataset["requests"]
            )
            for dataset in (by_bk, by_hd)
        )
        == emergencies
    )


def test_saved_demo_pair_follows_its_files(mongo):
    """Демо-пара в базе обновляется на старте, как встроенный набор: пересборка составов
    (блок 47) иначе дошла бы до неё только при повторном выборе. Правленую пару не трогаем."""
    stale = to_document_of("yugotsentr", "reduced")
    stale["engineers"][0]["shiftEnd"] = "19:00"
    edited = to_document_of("yugotsentr", "staff")
    edited["engineers"][0]["shiftEnd"] = "19:00"
    edited["updatedAt"] = datetime.now(UTC)
    mongo.datasets.insert_many([stale, edited])

    seed.ensure_seeded()

    fresh = seed.by_id("demo:yugotsentr:reduced")
    assert fresh.engineers == demo("yugotsentr", "reduced").engineers
    assert seed.by_id("demo:yugotsentr:staff").engineers[0].shift_end == "19:00"


def to_document_of(requests_id: str, crew_id: str) -> dict:
    return seed.to_document(demo(requests_id, crew_id))
