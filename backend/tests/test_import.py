"""Загрузка файлов и геокодирование (PLAN 5.4, блок 12).

Внешние сервисы не вызываются: 2ГИС — `httpx.MockTransport` на `geocoder.client` либо
подстановка вместо `geocode`, Valhalla — подмена `valhalla.matrix`. Mongo нужна кэшу
геокодера и тестам эндпоинта.
"""

import json
import shutil

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.data import geocoder, importer, seed
from app.main import app
from app.models import Input
from app.routing import matrices, valhalla

HEADER = (
    "id;address;lat;lon;serviceDurationMin;baseNormMin;windowStart;windowEnd;"
    "workPriority;skill;requiredTransport;requiredEquipment;urgent\n"
)
REQUESTS_CSV = HEADER + (
    "1;Москва, улица Первая, дом 1;55.71;37.78;70;90;09:00;12:00;"
    "new_connection;connection;;router,alice;false\n"
    "2;Москва, улица Вторая, дом 2;55.72;37.79;30;50;10:00;14:00;regular;local;;;\n"
)
ENGINEERS_CSV = (
    "id;name;startLat;startLon;shiftStart;shiftEnd;skills;transport;equipment\n"
    "brigade-1;Бригада 1;55.70;37.77;09:00;18:00;local,connection;car;router,alice\n"
)
OFFICE_ADDRESS = "Москва, улица Юных Ленинцев, дом 83 строение 4"
# Офис едет последней строкой CSV заявок, как в исходных файлах кейса (PLAN 5.4).
OFFICE_LINE = f"Адрес офиса;{OFFICE_ADDRESS}\n"
POINT = (55.70, 37.77)


class FakeGeocoder:
    """Возвращает одну и ту же точку и помнит, о чём спрашивали."""

    def __init__(self, point=POINT, misses=()):
        self.point = point
        self.misses = misses
        self.calls = []

    def __call__(self, address):
        self.calls.append(address)
        return None if address in self.misses else self.point


def forbidden(address):
    raise AssertionError(f"геокодер не должен вызываться: {address}")


def dataset_json(**changes) -> bytes:
    raw = {
        "name": "Набор из файла",
        "office": {"address": OFFICE_ADDRESS, "lat": 55.70, "lon": 37.77},
        "requests": [
            {
                "id": "1",
                "address": "Москва, улица Первая, дом 1",
                "lat": 55.71,
                "lon": 37.78,
                "serviceDurationMin": 70,
                "baseNormMin": 90,
                "windowStart": "09:00",
                "windowEnd": "12:00",
                "workPriority": "new_connection",
                "skill": "connection",
                "requiredEquipment": ["router"],
            }
        ],
        "engineers": [
            {
                "id": "brigade-1",
                "name": "Бригада 1",
                "startLat": 55.70,
                "startLon": 37.77,
                "shiftStart": "09:00",
                "shiftEnd": "18:00",
                "skills": ["connection"],
                "transport": "car",
                "equipment": ["router"],
            }
        ],
    }
    raw.update(changes)
    return json.dumps(raw, ensure_ascii=False).encode()


def csv_dataset(requests=REQUESTS_CSV, engineers=ENGINEERS_CSV, encoding="utf-8"):
    return importer.from_csv(
        (requests + OFFICE_LINE).encode(encoding), engineers.encode(encoding)
    )


# --- разбор CSV --------------------------------------------------------------


def test_csv_pair_and_office_address_make_a_dataset():
    geocode = FakeGeocoder()
    dataset = importer.build(csv_dataset(), geocode)

    assert [request.id for request in dataset.requests] == ["1", "2"]
    assert dataset.engineers[0].skills == ["local", "connection"]
    assert dataset.office.address == OFFICE_ADDRESS
    assert (dataset.office.lat, dataset.office.lon) == POINT
    # Координаты заявок в файле есть — геокодер спрашивали только про офис.
    assert geocode.calls == [OFFICE_ADDRESS]


def test_csv_lists_split_by_comma_and_empty_cells_fall_back_to_defaults():
    dataset = importer.build(csv_dataset(), FakeGeocoder())
    first, second = dataset.requests

    assert first.required_equipment == ["router", "alice"]
    assert second.required_equipment == []
    assert second.required_transport is None
    assert second.urgent is False  # пустая ячейка — умолчание модели, а не ошибка


def test_csv_tool_columns_are_lists_and_optional():
    """Колонки инструментов — списки через запятую; файл без них, записанный до блока
    27.8, по-прежнему загружается, и инструментов тогда нет (PLAN 5.4)."""
    requests = (
        HEADER.replace(";urgent", ";requiredTools;urgent")
        + "1;Москва, улица Первая, дом 1;55.71;37.78;70;90;09:00;12:00;"
        "new_connection;connection;;router;cable_tester,crimping_tool;false\n"
    )
    engineers = ENGINEERS_CSV.replace(";equipment\n", ";equipment;tools\n", 1).replace(
        ";router,alice\n", ";router,alice;laptop\n"
    )
    dataset = importer.build(csv_dataset(requests, engineers), FakeGeocoder())

    assert dataset.requests[0].required_tools == ["cable_tester", "crimping_tool"]
    assert dataset.engineers[0].tools == ["laptop"]

    old = importer.build(csv_dataset(), FakeGeocoder())
    assert old.requests[0].required_tools == []
    assert old.engineers[0].tools == []


def test_file_in_an_unknown_encoding_is_a_load_error_not_a_crash():
    # Выбрали .xlsx вместо .csv - это ошибка загрузки, а не 500.
    with pytest.raises(importer.ImportProblem) as error:
        # начало .xlsx: байт 0x98 не разбирается ни в UTF-8, ни в cp1251
        importer.from_csv(
            bytes([0x50, 0x4B, 0x03, 0x04, 0x98]),
            ENGINEERS_CSV.encode(),
        )
    assert "не читается" in str(error.value)


def test_file_with_only_a_header_is_not_an_empty_dataset():
    # Разбирается без ошибок, но набор пустой: молча сделать такой текущим нельзя.
    with pytest.raises(importer.ImportProblem) as error:
        importer.build(csv_dataset(requests=HEADER), FakeGeocoder())
    assert "нет заявок" in str(error.value)


def test_csv_in_cp1251_is_read():
    dataset = importer.build(csv_dataset(encoding="cp1251"), FakeGeocoder())
    assert dataset.requests[0].address == "Москва, улица Первая, дом 1"


def test_office_line_is_read_in_any_case_and_before_trailing_blank_lines():
    # Так пишет Excel: «Адрес Офиса» с большой буквы и хвост из пустых ячеек.
    raw = importer.from_csv(
        (REQUESTS_CSV + f"Адрес Офиса;{OFFICE_ADDRESS};;\n;;;\n\n").encode("cp1251"),
        ENGINEERS_CSV.encode(),
    )
    assert raw["office"] == {"address": OFFICE_ADDRESS}
    assert [fields["id"] for _, fields in raw["requests"]] == ["1", "2"]


def test_requests_without_office_line_are_refused():
    with pytest.raises(importer.ImportProblem) as error:
        importer.from_csv(REQUESTS_CSV.encode(), ENGINEERS_CSV.encode())
    assert "Адрес офиса" in str(error.value)


def test_office_line_in_the_middle_is_not_the_office():
    # Офис — только последняя строка: иначе номера строк в ошибках разошлись бы с файлом.
    lines = REQUESTS_CSV.splitlines(keepends=True)
    moved = lines[0] + OFFICE_LINE + "".join(lines[1:])
    with pytest.raises(importer.ImportProblem) as error:
        importer.from_csv(moved.encode(), ENGINEERS_CSV.encode())
    assert "последней строкой" in str(error.value)


def test_hd_type_column_is_optional():
    with_hd = HEADER.replace(";urgent", ";urgent;hdType")
    rows = REQUESTS_CSV.removeprefix(HEADER).splitlines()
    requests = with_hd + rows[0] + ";Нет линка\n" + rows[1] + ";\n"
    dataset = importer.build(csv_dataset(requests=requests), FakeGeocoder())
    assert [request.hd_type for request in dataset.requests] == ["Нет линка", None]
    assert importer.build(csv_dataset(), FakeGeocoder()).requests[0].hd_type is None


def test_csv_start_point_of_engineer_defaults_to_office():
    # Все бригады стартуют из офиса, а его координаты диспетчеру заранее неизвестны.
    engineers = (
        "id;name;startLat;startLon;shiftStart;shiftEnd;skills;transport;equipment\n"
        "brigade-1;Бригада 1;;;09:00;18:00;local;car;\n"
    )
    dataset = importer.build(csv_dataset(engineers=engineers), FakeGeocoder())
    engineer = dataset.engineers[0]
    assert (engineer.start_lat, engineer.start_lon) == POINT


def test_csv_bad_value_names_the_line_and_the_field():
    broken = REQUESTS_CSV.replace("09:00;12:00", "09:00;24:61")
    with pytest.raises(importer.ImportProblem) as error:
        importer.build(csv_dataset(requests=broken), FakeGeocoder())

    assert "заявки, строка 2" in str(error.value)
    assert "windowEnd" in str(error.value)


def test_csv_missing_required_column_is_reported_for_every_row():
    without_window = HEADER.replace("windowEnd;", "") + (
        "1;Москва, улица Первая, дом 1;55.71;37.78;70;90;09:00;"
        "new_connection;connection;;;false\n"
    )
    with pytest.raises(importer.ImportProblem) as error:
        importer.build(csv_dataset(requests=without_window), FakeGeocoder())
    assert "windowEnd" in str(error.value)


def test_csv_row_with_extra_values_is_rejected():
    broken = (
        REQUESTS_CSV
        + "3;адрес;55.7;37.7;30;50;09:00;12:00;regular;local;;;false;лишнее\n"
    )
    with pytest.raises(importer.ImportProblem) as error:
        importer.build(csv_dataset(requests=broken), FakeGeocoder())
    assert "больше, чем колонок" in str(error.value)


def test_csv_collects_problems_from_both_files():
    with pytest.raises(importer.ImportProblem) as error:
        csv_dataset(
            requests=REQUESTS_CSV
            + "3;адрес;55.7;37.7;30;50;09:00;12:00;a;b;;;false;x\n",
            engineers=ENGINEERS_CSV
            + "brigade-2;Бригада 2;;;09:00;18:00;local;car;;лишнее\n",
        )
    assert "заявки, строка 4" in str(error.value)
    assert "инженеры, строка 3" in str(error.value)


def test_duplicate_request_ids_are_rejected():
    twice = REQUESTS_CSV + REQUESTS_CSV.splitlines()[1] + "\n"
    with pytest.raises(importer.ImportProblem) as error:
        importer.build(csv_dataset(requests=twice), FakeGeocoder())
    assert "идентификаторы заявок не уникальны" in str(error.value)


def test_unknown_column_is_not_swallowed():
    with_extra = (
        HEADER.rstrip("\n")
        + ";комментарий\n"
        + ("1;адрес;55.71;37.78;70;90;09:00;12:00;regular;local;;;false;что-то\n")
    )
    with pytest.raises(importer.ImportProblem) as error:
        importer.build(csv_dataset(requests=with_extra), FakeGeocoder())
    assert "комментарий" in str(error.value)


# --- разбор JSON -------------------------------------------------------------


def test_json_dataset_keeps_its_name_and_gets_a_new_id():
    first = importer.build(importer.from_json(dataset_json()), forbidden)
    second = importer.build(importer.from_json(dataset_json()), forbidden)

    assert first.name == "Набор из файла"
    assert first.built_in is False
    assert first.id != second.id


def test_name_from_the_form_wins_over_the_name_inside_the_file():
    # Иначе переименовать набор при загрузке нельзя, а два импорта одного примера
    # не отличить друг от друга.
    assert importer.build(
        importer.from_json(dataset_json()), forbidden, "Своё имя"
    ).name == ("Своё имя")
    raw = importer.from_json(dataset_json(name=None))
    assert importer.build(raw, forbidden).name == importer.DEFAULT_NAME


def test_name_inside_the_file_wins_over_the_file_name():
    # Имя файла — последняя попытка: «Юго-восток» из набора понятнее, чем «yugo-vostok»
    # из названия файла, а в интерфейсе (блок 14) имя стоит на видном месте.
    assert importer.build(
        importer.from_json(dataset_json()), forbidden, None, "yugo-vostok"
    ).name == ("Набор из файла")
    raw = importer.from_json(dataset_json(name=None))
    assert importer.build(raw, forbidden, None, "yugo-vostok").name == "yugo-vostok"


def test_request_without_coordinates_is_geocoded():
    request = json.loads(dataset_json())["requests"][0]
    request.pop("lat"), request.pop("lon")
    geocode = FakeGeocoder(point=(55.75, 37.60))

    dataset = importer.build(
        importer.from_json(dataset_json(requests=[request])), geocode
    )

    assert (dataset.requests[0].lat, dataset.requests[0].lon) == (55.75, 37.60)
    assert geocode.calls == ["Москва, улица Первая, дом 1"]


def test_address_not_found_names_the_place():
    request = json.loads(dataset_json())["requests"][0]
    request.pop("lat"), request.pop("lon")
    geocode = FakeGeocoder(misses={"Москва, улица Первая, дом 1"})

    with pytest.raises(importer.ImportProblem) as error:
        importer.build(importer.from_json(dataset_json(requests=[request])), geocode)
    assert "заявка 1" in str(error.value)
    assert "не найден" in str(error.value)


def test_broken_json_is_reported_as_a_file_problem():
    with pytest.raises(importer.ImportProblem) as error:
        importer.from_json(b"{ not json")
    assert "не разбирается как JSON" in str(error.value)


def test_json_without_office_is_rejected():
    with pytest.raises(importer.ImportProblem) as error:
        importer.from_json(b'{"requests": [], "engineers": []}')
    assert "office" in str(error.value)


def test_examples_from_the_repository_are_importable():
    """`data/examples` — примеры формата загрузки, и они обязаны им оставаться."""
    path = settings.data_dir / "examples"
    json_dataset = importer.build(
        importer.from_json((path / "yugo-vostok.json").read_bytes()), forbidden
    )
    csv_dataset_ = importer.build(
        importer.from_csv(
            (path / "yugotsentr-requests.csv").read_bytes(),
            (path / "yugotsentr-engineers.csv").read_bytes(),
        ),
        FakeGeocoder(),
    )
    assert len(json_dataset.requests) == 83
    assert len(csv_dataset_.requests) == 56
    # Офис примера — тот же, что в его JSON: пример не разошёлся с набором.
    same = json.loads((path / "yugotsentr.json").read_text("utf-8"))
    assert csv_dataset_.office.address == same["office"]["address"]
    assert csv_dataset_.requests[0].hd_type == same["requests"][0]["hdType"]


# --- геокодер ----------------------------------------------------------------


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setattr(settings, "dgis_api_key", "тестовый-ключ")


def answers(monkeypatch, *responses):
    """Подменяет клиент 2ГИС: ответы отдаются по очереди, запросы запоминаются."""
    asked = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.params["q"])
        return responses[min(len(asked) - 1, len(responses) - 1)]

    monkeypatch.setattr(
        geocoder,
        "client",
        httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="https://catalog.api.2gis.com",
        ),
    )
    return asked


def found(lat=55.702293, lon=37.77392) -> httpx.Response:
    return httpx.Response(
        200, json={"result": {"items": [{"point": {"lat": lat, "lon": lon}}]}}
    )


def test_lookup_returns_the_best_match(monkeypatch, key):
    answers(monkeypatch, found())
    assert geocoder.lookup(OFFICE_ADDRESS) == (55.702293, 37.77392)


def test_lookup_returns_none_when_the_address_is_unknown(monkeypatch, key):
    answers(monkeypatch, httpx.Response(404, json={}))
    assert geocoder.lookup("нет такого адреса") is None


def test_point_outside_the_osm_extract_is_not_a_match(monkeypatch, key):
    # Владивосток: Valhalla до него не доедет, подставлять такую точку в план нельзя.
    answers(monkeypatch, found(lat=43.115, lon=131.885))
    assert geocoder.lookup("улица Первая") is None


def test_error_status_is_not_an_unknown_address(monkeypatch, key):
    # Просроченный ключ и исчерпанный лимит отвечают так на каждый адрес.
    answers(monkeypatch, httpx.Response(403, text="limit exceeded"))
    with pytest.raises(geocoder.GeocodeError):
        geocoder.lookup(OFFICE_ADDRESS)


def test_without_a_key_nothing_is_asked(monkeypatch):
    monkeypatch.setattr(settings, "dgis_api_key", "")
    asked = answers(monkeypatch, found())
    with pytest.raises(geocoder.GeocodeError):
        geocoder.lookup(OFFICE_ADDRESS)
    assert asked == []


def test_geocode_retries_without_the_building_part(monkeypatch, key, mongo):
    asked = answers(monkeypatch, httpx.Response(404, json={}), found())
    assert geocoder.geocode("Москва, улица Первая, дом 1 корпус 5") is not None
    assert asked == [
        "Москва, улица Первая, дом 1 корпус 5",
        "Москва, улица Первая, дом 1",
    ]


def test_geocode_normalizes_the_address_before_asking(monkeypatch, key, mongo):
    asked = answers(monkeypatch, found())
    geocoder.geocode("г.Город Москва, ул.Юных Ленинцев, д.83с 4, кв.12")
    assert asked == ["Москва, улица Юных Ленинцев, дом 83 строение 4"]


def test_found_address_is_cached_in_mongo(monkeypatch, key, mongo):
    asked = answers(monkeypatch, found())
    first = geocoder.geocode(OFFICE_ADDRESS)
    second = geocoder.geocode(OFFICE_ADDRESS)

    assert first == second
    assert len(asked) == 1  # второй раз ключ не тратим
    assert mongo.geocodes.count_documents({}) == 1


def test_miss_is_not_cached(monkeypatch, key, mongo):
    answers(monkeypatch, httpx.Response(404, json={}))
    assert geocoder.geocode("нет такого адреса") is None
    assert mongo.geocodes.count_documents({}) == 0


# --- эндпоинты ---------------------------------------------------------------


@pytest.fixture
def client(monkeypatch, mongo):
    """Приложение на пустой тестовой базе со встроенным набором и без Valhalla."""

    def matrix(sources, targets, costing):
        return (
            [[100] * len(targets) for _ in sources],
            [[1000] * len(targets) for _ in sources],
        )

    monkeypatch.setattr(valhalla, "matrix", matrix)
    seed.ensure_seeded()
    return TestClient(app)


def test_import_makes_the_uploaded_dataset_current(client, monkeypatch, mongo):
    monkeypatch.setattr(geocoder, "geocode", FakeGeocoder())
    response = client.post(
        "/api/dataset/import",
        files={
            "requests": (
                "requests.csv",
                (REQUESTS_CSV + OFFICE_LINE).encode(),
                "text/csv",
            ),
            "engineers": ("engineers.csv", ENGINEERS_CSV.encode(), "text/csv"),
        },
        data={"name": "Загруженный"},
    )
    assert response.status_code == 200
    imported = response.json()
    assert imported["name"] == "Загруженный"

    assert client.get("/api/dataset").json()["id"] == imported["id"]
    # Таблицы переездов посчитаны сразу: план по набору считается без похода в Valhalla.
    assert mongo.matrices.count_documents({"datasetId": imported["id"]}) == 1
    # Встроенный набор остался, и `reset` снова делает текущим его.
    assert client.post("/api/dataset/reset").json()["id"] == seed.BUILTIN_ID
    assert client.get("/api/dataset").json()["id"] == seed.BUILTIN_ID


# --- факт того же дня (PLAN 2.1, 26.3) -------------------------------------


def test_builtin_dataset_carries_the_control_summary(client):
    """Сводка контрольной выборки едет в наборе и переживает правку диспетчера."""
    expected = json.loads(
        (settings.data_dir / "seed" / "control.json").read_text("utf-8")
    )

    assert client.get("/api/dataset").json()["control"] == expected
    edited = client.post("/api/dataset/requests", json=NEW_REQUEST).json()
    assert edited["control"] == expected


def test_control_summary_reaches_a_builtin_already_in_the_base(mongo):
    """Том Mongo с прошлых блоков: набор уже лежит, а сводку ему всё равно дописывают."""
    seed.ensure_seeded()
    mongo.datasets.update_one({"_id": seed.BUILTIN_ID}, {"$unset": {"control": ""}})

    seed.ensure_seeded()

    assert seed.by_id(seed.BUILTIN_ID).control == seed.load_builtin().control


def test_stale_builtin_in_the_base_is_refreshed_from_the_file(mongo):
    """Том Mongo с прошлых блоков: набор лежит без инструментов (блок 27.8), а файл уже
    пересобран. Неправленый набор обновляется при старте; `createdAt` не трогается —
    иначе встроенный стал бы текущим поверх загруженного позже."""
    seed.ensure_seeded()
    created = mongo.datasets.find_one({"_id": seed.BUILTIN_ID})["createdAt"]
    mongo.datasets.update_one(
        {"_id": seed.BUILTIN_ID}, {"$unset": {"engineers.$[].tools": ""}}
    )
    assert not any(engineer.tools for engineer in seed.by_id(seed.BUILTIN_ID).engineers)

    seed.ensure_seeded()

    stored = seed.by_id(seed.BUILTIN_ID)
    assert stored.engineers == seed.load_builtin().engineers
    assert mongo.datasets.find_one({"_id": seed.BUILTIN_ID})["createdAt"] == created


def test_edited_builtin_keeps_the_dispatcher_edits(client, mongo):
    """Правки диспетчера важнее файла: вернуть файл — это reset, а не перезапуск."""
    client.post("/api/dataset/requests", json=NEW_REQUEST)

    seed.ensure_seeded()

    ids = [request.id for request in seed.by_id(seed.BUILTIN_ID).requests]
    assert NEW_REQUEST["id"] in ids


def test_removed_control_file_removes_the_stale_summary(mongo, tmp_path, monkeypatch):
    """Файл сводки убрали — строка факта не должна остаться в базе устаревшей."""
    seed.ensure_seeded()
    shutil.copytree(settings.data_dir / "seed", tmp_path / "seed")
    (tmp_path / "seed" / "control.json").unlink()
    monkeypatch.setattr(settings, "data_dir", tmp_path)

    seed.ensure_seeded()

    assert seed.by_id(seed.BUILTIN_ID).control is None


def test_control_file_matches_the_control_sample():
    """data/seed/control.json не устарел: пересчёт по контрольному CSV даёт то же самое.

    Сводка — справка, а не вход: в `Input`, а значит и в расчёт, её нет.
    """
    from scripts import prepare_data

    stored = json.loads(
        (settings.data_dir / "seed" / "control.json").read_text("utf-8")
    )

    assert prepare_data.control_summary("Восток") == stored
    assert stored["engineers"] == prepare_data.control_brigades("Восток")
    assert "control" not in Input.model_fields


def test_import_json_example(client):
    path = settings.data_dir / "examples" / "yugo-vostok.json"
    response = client.post(
        "/api/dataset/import",
        files={"file": ("yugo-vostok.json", path.read_bytes(), "application/json")},
    )
    assert response.status_code == 200
    assert len(response.json()["requests"]) == 83
    # Имя формой не задано, поэтому берётся своё имя набора, а не «yugo-vostok»
    # из названия файла: в интерфейсе (блок 14) оно стоит на видном месте.
    assert response.json()["name"] == "Юго-восток"


def test_broken_file_is_answered_by_lines_and_changes_nothing(
    client, monkeypatch, mongo
):
    monkeypatch.setattr(geocoder, "geocode", FakeGeocoder())
    before = mongo.datasets.count_documents({})
    response = client.post(
        "/api/dataset/import",
        files={
            "requests": (
                "requests.csv",
                (
                    REQUESTS_CSV.replace("09:00;12:00", "09:00;24:61") + OFFICE_LINE
                ).encode(),
                "text/csv",
            ),
            "engineers": ("engineers.csv", ENGINEERS_CSV.encode(), "text/csv"),
        },
    )
    assert response.status_code == 400
    assert "строка 2" in response.json()["detail"]
    assert mongo.datasets.count_documents({}) == before


def test_csv_without_office_is_refused(client):
    response = client.post(
        "/api/dataset/import",
        files={
            "requests": ("requests.csv", REQUESTS_CSV.encode(), "text/csv"),
            "engineers": ("engineers.csv", ENGINEERS_CSV.encode(), "text/csv"),
        },
    )
    assert response.status_code == 400
    assert "офис" in response.json()["detail"]


def test_empty_upload_is_refused(client):
    assert client.post("/api/dataset/import").status_code == 400


def test_geocode_endpoint(client, monkeypatch, key):
    answers(monkeypatch, found())
    response = client.post("/api/geocode", json={"address": OFFICE_ADDRESS})
    assert response.status_code == 200
    assert response.json() == {"lat": 55.702293, "lon": 37.77392}


def test_geocode_endpoint_answers_404_and_503(client, monkeypatch, key):
    answers(monkeypatch, httpx.Response(404, json={}))
    assert (
        client.post("/api/geocode", json={"address": "нет такого"}).status_code == 404
    )

    monkeypatch.setattr(settings, "dgis_api_key", "")
    assert client.post("/api/geocode", json={"address": "Москва"}).status_code == 503


# --- подсказки адресов (блок 34) ---------------------------------------------


@pytest.fixture
def suggest_on(monkeypatch, key):
    """Подсказки включены, состояние модуля — окно, отказ и кэш — с чистого листа."""
    monkeypatch.setattr(settings, "dgis_suggest_per_min", 5)
    monkeypatch.setattr(settings, "dgis_suggest_interval_sec", 0)
    monkeypatch.setattr(geocoder, "_sent", geocoder.deque())
    monkeypatch.setattr(geocoder, "_refused", None)
    geocoder._suggest.cache_clear()
    yield
    geocoder._suggest.cache_clear()


def suggested(*items) -> httpx.Response:
    return httpx.Response(200, json={"result": {"items": list(items)}})


HOUSE = {
    "type": "building",
    "full_name": "Москва, Волгоградский проспект, 128 к5",
    "point": {"lat": 55.7051, "lon": 37.7632},
}
STREET = {
    "type": "street",
    "full_name": "Москва, Волгоградский проспект",
    "point": {"lat": 55.72, "lon": 37.71},
}


def test_suggest_gives_a_point_only_to_a_house(monkeypatch, suggest_on):
    far = {**HOUSE, "full_name": "Владивосток, …", "point": {"lat": 43.1, "lon": 131.9}}
    answers(monkeypatch, suggested(HOUSE, STREET, far, {"type": "building"}))

    # Улица — только текст: её точка — середина улицы, заявка встала бы не туда. Точка вне
    # вырезки OSM и подсказка без названия отбрасываются.
    assert geocoder.suggest("Волгоградский 128") == [
        ("Москва, Волгоградский проспект, 128 к5", 55.7051, 37.7632),
        ("Москва, Волгоградский проспект", None, None),
    ]


def test_suggest_asks_the_same_text_once(monkeypatch, suggest_on):
    asked = answers(monkeypatch, suggested(HOUSE))
    geocoder.suggest("Волгоградский  128")
    geocoder.suggest("волгоградский 128 ")
    assert asked == ["волгоградский 128"]


def test_suggest_stops_at_the_limit_per_minute(monkeypatch, suggest_on):
    asked = answers(monkeypatch, suggested(HOUSE))
    for number in range(5):
        geocoder.suggest(f"адрес {number}")
    with pytest.raises(geocoder.SuggestLimited):
        geocoder.suggest("адрес 5")
    assert len(asked) == 5  # шестой запрос до 2ГИС не дошёл


def test_suggest_keeps_an_interval_between_requests(monkeypatch, suggest_on):
    """Ранний запрос ждёт остаток интервала; кэш и поздний запрос не ждут."""
    clock = {"now": 100.0}
    slept = []

    def sleep(seconds):
        slept.append(round(seconds, 3))
        clock["now"] += seconds

    fake = type("Clock", (), {"monotonic": lambda: clock["now"], "sleep": sleep})
    monkeypatch.setattr(geocoder, "time", fake)
    monkeypatch.setattr(settings, "dgis_suggest_interval_sec", 1.0)
    asked = answers(monkeypatch, suggested(HOUSE))

    geocoder.suggest("адрес 1")
    clock["now"] += 0.3
    geocoder.suggest("адрес 2")  # через 0,3 с — ждёт ещё 0,7 с
    geocoder.suggest("адрес 2")  # кэш: 2ГИС не спрашивают и не ждут
    clock["now"] += 5
    geocoder.suggest("адрес 3")  # интервал давно прошёл

    assert slept == [0.7]
    assert len(asked) == 3


def test_suggest_goes_quiet_after_a_refusal(monkeypatch, suggest_on):
    # Исчерпанный лимит ключа: дальше не спрашиваем вовсе — повторы после 429 и жгут ключ.
    asked = answers(monkeypatch, httpx.Response(429, text="too many"), suggested(HOUSE))
    with pytest.raises(geocoder.GeocodeError):
        geocoder.suggest("Волгоградский")
    with pytest.raises(geocoder.GeocodeError):
        geocoder.suggest("Юных Ленинцев")
    assert asked == ["волгоградский"]


def test_suggest_nothing_found_is_not_a_refusal(monkeypatch, suggest_on):
    # 404 каталог отдаёт на «ничего не найдено» — опечатка не должна выключать подсказки.
    asked = answers(monkeypatch, httpx.Response(404, json={}), suggested(HOUSE))
    assert geocoder.suggest("Волгоградскиййй") == []
    assert geocoder.suggest("Волгоградский") != []
    assert len(asked) == 2


def test_suggest_refusal_does_not_echo_the_answer(monkeypatch, suggest_on):
    answers(monkeypatch, httpx.Response(403, text="key=тестовый-ключ is expired"))
    with pytest.raises(geocoder.GeocodeError) as error:
        geocoder.suggest("Волгоградский")
    assert "тестовый-ключ" not in str(error.value)


def test_suggest_network_failure_is_not_a_refusal(client, monkeypatch, suggest_on):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectTimeout("нет связи")
        return suggested(HOUSE)

    monkeypatch.setattr(
        geocoder,
        "client",
        httpx.Client(transport=httpx.MockTransport(handler), base_url="https://x"),
    )
    # 502, а не 503: по 503 форма перестаёт спрашивать, а сеть — сбой разовый.
    url = "/api/geocode/suggest"
    assert client.get(url, params={"q": "Волгоградский"}).status_code == 502
    assert client.get(url, params={"q": "Волгоградский"}).status_code == 200


def test_suggest_unexpected_body_is_a_passing_failure(monkeypatch, suggest_on):
    answers(monkeypatch, httpx.Response(200, text="<html>портал Wi-Fi</html>"))
    with pytest.raises(geocoder.SuggestFailed):
        geocoder.suggest("Волгоградский")


@pytest.mark.parametrize("change", [("dgis_suggest_per_min", 0), ("dgis_api_key", "")])
def test_suggest_is_off_without_a_limit_or_a_key(monkeypatch, suggest_on, change):
    monkeypatch.setattr(settings, *change)
    asked = answers(monkeypatch, suggested(HOUSE))
    with pytest.raises(geocoder.GeocodeError):
        geocoder.suggest("Волгоградский")
    assert asked == []


def test_suggest_endpoint(client, monkeypatch, suggest_on):
    answers(monkeypatch, suggested(HOUSE, STREET))
    response = client.get("/api/geocode/suggest", params={"q": "Волгоградский"})
    assert response.status_code == 200
    assert response.json() == [
        {
            "address": "Москва, Волгоградский проспект, 128 к5",
            "lat": 55.7051,
            "lon": 37.7632,
        },
        {"address": "Москва, Волгоградский проспект", "lat": None, "lon": None},
    ]

    assert client.get("/api/geocode/suggest", params={"q": "Во"}).status_code == 422


def test_suggest_endpoint_tells_the_rest_of_the_minute(client, monkeypatch, suggest_on):
    """Форма замолкает на последнем разрешённом запросе, а не узнаёт о лимите из 429."""
    answers(monkeypatch, suggested(HOUSE))
    monkeypatch.setattr(settings, "dgis_suggest_per_min", 2)
    url = "/api/geocode/suggest"

    first = client.get(url, params={"q": "адрес 1"})
    assert first.headers["X-Suggest-Remaining"] == "1"
    last = client.get(url, params={"q": "адрес 2"})
    assert last.headers["X-Suggest-Remaining"] == "0"
    assert 1 <= int(last.headers["X-Suggest-Reset"]) <= 60
    # Из кэша ключ не тратится, и остаток не меняется.
    again = client.get(url, params={"q": "адрес 1"})
    assert again.status_code == 200
    assert again.headers["X-Suggest-Remaining"] == "0"


def test_suggest_endpoint_answers_429_and_503(client, monkeypatch, suggest_on):
    answers(monkeypatch, suggested(HOUSE))
    monkeypatch.setattr(settings, "dgis_suggest_per_min", 1)
    assert (
        client.get("/api/geocode/suggest", params={"q": "адрес 1"}).status_code == 200
    )
    limited = client.get(
        "/api/geocode/suggest",
        params={"q": "адрес 2"},
        headers={"Origin": "http://localhost:3000"},
    )
    assert limited.status_code == 429
    # По заголовку форма молчит до конца минуты, а не спрашивает на каждой паузе;
    # браузер отдаст его скрипту, только если CORS его открыл.
    assert 1 <= int(limited.headers["Retry-After"]) <= 60
    assert "retry-after" in limited.headers["access-control-expose-headers"].lower()

    monkeypatch.setattr(settings, "dgis_suggest_per_min", 0)
    assert (
        client.get("/api/geocode/suggest", params={"q": "адрес 3"}).status_code == 503
    )


# --- разбиение матрицы (PLAN 5.6) --------------------------------------------


def test_big_matrix_is_asked_in_chunks(monkeypatch):
    """Набор больше лимита пар считается несколькими запросами, а не падает 4xx."""
    calls = []

    def matrix(sources, targets, costing):
        calls.append((len(sources), len(targets)))
        return (
            [[100] * len(targets) for _ in sources],
            [[1000] * len(targets) for _ in sources],
        )

    monkeypatch.setattr(valhalla, "matrix", matrix)
    monkeypatch.setattr(settings, "valhalla_max_matrix_pairs", 8)

    points = [(55.70 + number / 100, 37.77) for number in range(5)]
    durations, distances, approximate = matrices._block(points, points, "auto")

    assert calls == [
        (1, 5),
        (1, 5),
        (1, 5),
        (1, 5),
        (1, 5),
    ]  # 8 // 5 = 1 источник за раз
    assert len(durations) == len(distances) == 5
    assert all(len(row) == 5 for row in durations)
    assert approximate is False


# --- правка набора диспетчером (PLAN 6.19, блок 24.6) ------------------------

NEW_REQUEST = {
    "id": "новая-1",
    "address": "Москва, Волгоградский проспект, 128 к5",
    "lat": 55.7051,
    "lon": 37.7632,
    "serviceDurationMin": 70,
    "baseNormMin": 90,
    "windowStart": "09:00",
    "windowEnd": "18:00",
    "workPriority": "new_connection",
    "skill": "connection",
    "requiredEquipment": ["router"],
}
NEW_ENGINEER = {
    "id": "brigade-99",
    "name": "Бригада 99",
    "shiftStart": "09:00",
    "shiftEnd": "18:00",
    "skills": ["connection"],
    "transport": "car",
    "equipment": ["router"],
}


def test_added_request_becomes_part_of_the_dataset(client):
    before = len(client.get("/api/dataset").json()["requests"])

    response = client.post("/api/dataset/requests", json=NEW_REQUEST)

    assert response.status_code == 200
    assert len(response.json()["requests"]) == before + 1
    assert response.json()["requests"][-1]["id"] == "новая-1"
    assert response.json()["updatedAt"] is not None
    # Набор в базе тоже изменился: план считается уже по нему.
    assert client.get("/api/dataset").json()["requests"][-1]["id"] == "новая-1"


def test_added_request_is_geocoded_when_it_has_no_point(client, monkeypatch):
    geocode = FakeGeocoder()
    monkeypatch.setattr(geocoder, "geocode", geocode)
    body = {k: v for k, v in NEW_REQUEST.items() if k not in ("lat", "lon")}

    response = client.post("/api/dataset/requests", json=body)

    assert response.status_code == 200
    assert geocode.calls == [NEW_REQUEST["address"]]
    assert response.json()["requests"][-1]["lat"] == POINT[0]


def test_added_request_with_a_taken_id_is_rejected(client):
    taken = client.get("/api/dataset").json()["requests"][0]["id"]

    response = client.post("/api/dataset/requests", json=NEW_REQUEST | {"id": taken})

    assert response.status_code == 409
    assert "уже есть" in response.json()["detail"]


def test_added_engineer_starts_from_the_office_without_coordinates(client):
    office = client.get("/api/dataset").json()["office"]

    response = client.post("/api/dataset/engineers", json=NEW_ENGINEER)

    assert response.status_code == 200
    added = response.json()["engineers"][-1]
    assert (added["startLat"], added["startLon"]) == (office["lat"], office["lon"])


def test_added_engineer_keeps_its_own_home(client):
    response = client.post(
        "/api/dataset/engineers",
        json=NEW_ENGINEER | {"startLat": 54.84, "startLon": 38.19},
    )

    assert response.status_code == 200
    assert response.json()["engineers"][-1]["startLat"] == 54.84


def test_removed_request_disappears_from_the_dataset(client):
    dataset = client.get("/api/dataset").json()
    victim = dataset["requests"][0]["id"]

    response = client.delete(f"/api/dataset/requests/{victim}")

    assert response.status_code == 200
    assert victim not in {item["id"] for item in response.json()["requests"]}
    assert response.status_code == 200
    assert client.delete(f"/api/dataset/requests/{victim}").status_code == 404


def test_the_last_engineer_cannot_be_removed(client, monkeypatch):
    dataset = client.get("/api/dataset").json()
    for engineer in dataset["engineers"][:-1]:
        assert (
            client.delete(f"/api/dataset/engineers/{engineer['id']}").status_code == 200
        )

    response = client.delete(f"/api/dataset/engineers/{dataset['engineers'][-1]['id']}")

    assert response.status_code == 400
    assert "последний инженер" in response.json()["detail"]


def test_reset_undoes_the_edits(client):
    client.post("/api/dataset/requests", json=NEW_REQUEST)

    restored = client.post("/api/dataset/reset").json()

    assert "новая-1" not in {item["id"] for item in restored["requests"]}


def test_plan_counts_the_added_request(client):
    client.post("/api/dataset/requests", json=NEW_REQUEST)

    plan = client.post("/api/plans", json={"algorithm": "baseline"}).json()

    assert plan["validation"]["ok"]
    assert "новая-1" in plan["assignments"]


# --- правка заявки и бригады (PLAN 6.19, блок 28) ----------------------------


def test_edited_request_replaces_the_old_one(client):
    dataset = client.get("/api/dataset").json()
    victim = dataset["requests"][0]["id"]
    before = len(dataset["requests"])

    response = client.put(
        f"/api/dataset/requests/{victim}",
        json=NEW_REQUEST | {"id": victim, "windowStart": "10:00"},
    )

    assert response.status_code == 200
    requests = response.json()["requests"]
    assert len(requests) == before  # заменили, а не добавили
    assert requests[0]["id"] == victim  # порядок набора сохранён (PLAN 6.1)
    assert requests[0]["windowStart"] == "10:00"
    assert requests[0]["address"] == NEW_REQUEST["address"]
    # Точка заявки переехала вместе с адресом: `req:<id>` от адреса не зависит, и без сверки
    # координат в таблицах остались бы плечи до прежнего места (ревью блока 28).
    travel = matrices.get(seed.current())
    assert travel.points[travel.index[f"req:{victim}"]][1:] == (
        NEW_REQUEST["lat"],
        NEW_REQUEST["lon"],
    )
    # План по правленому набору считается сразу.
    assert client.post("/api/plans", json={}).status_code == 200


def test_edited_request_is_geocoded_when_it_has_no_point(client, monkeypatch):
    geocode = FakeGeocoder()
    monkeypatch.setattr(geocoder, "geocode", geocode)
    victim = client.get("/api/dataset").json()["requests"][0]["id"]
    body = {k: v for k, v in NEW_REQUEST.items() if k not in ("lat", "lon")}

    response = client.put(f"/api/dataset/requests/{victim}", json=body | {"id": victim})

    assert response.status_code == 200
    assert geocode.calls == [NEW_REQUEST["address"]]


def test_edited_request_that_is_not_in_the_dataset_is_rejected(client):
    response = client.put("/api/dataset/requests/нет-такой", json=NEW_REQUEST)

    assert response.status_code == 404


def test_edited_engineer_replaces_the_old_one(client):
    dataset = client.get("/api/dataset").json()
    victim = dataset["engineers"][0]["id"]

    response = client.put(
        f"/api/dataset/engineers/{victim}",
        json=NEW_ENGINEER | {"id": victim, "shiftEnd": "21:00"},
    )

    assert response.status_code == 200
    engineers = response.json()["engineers"]
    assert len(engineers) == len(dataset["engineers"])
    assert engineers[0]["id"] == victim
    assert engineers[0]["shiftEnd"] == "21:00"
    # Пустой старт означает офис — то же правило, что у добавления (Дополнения, п. 4).
    assert engineers[0]["startLat"] == dataset["office"]["lat"]


def test_edited_engineer_that_is_not_in_the_dataset_is_rejected(client):
    response = client.put("/api/dataset/engineers/нет-такой", json=NEW_ENGINEER)

    assert response.status_code == 404
