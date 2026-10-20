"""Вложения ассистента (блок 60): разбор файла, сводка, отказы и хранение."""

import pytest
from fastapi.testclient import TestClient

from app import attachments, db
from app.config import settings
from app.main import app

CSV = "Заявка;Адрес;Окно\n101;Москва, Тверская, 1;10:00–12:00\n102;Кашира, Ленина, 2;14:00–16:00\n"


def test_plain_csv_is_read_with_its_rows():
    read = attachments.read("список.csv", CSV.encode("utf-8"))

    assert read["kind"] == "csv"
    assert read["text"] == CSV
    assert read["summary"] == "CSV, 3 строки"


def test_cp1251_is_read_like_excel_saves_it():
    read = attachments.read("выгрузка.CSV", CSV.encode("cp1251"))

    assert "Кашира" in read["text"]


def test_json_and_text():
    assert attachments.read("a.json", b'{"x": 1}')["summary"] == "JSON"
    text = attachments.read("заметки.txt", "Соколов\nПопов\n".encode())
    assert (text["kind"], text["summary"]) == ("txt", "текст, 2 строки")


def test_upload_formats_are_recognized():
    folder = settings.data_dir / "examples"
    requests = attachments.read(
        "r.csv", (folder / "yugotsentr-requests.csv").read_bytes()
    )
    engineers = attachments.read(
        "e.csv", (folder / "yugotsentr-engineers.csv").read_bytes()
    )
    dataset = attachments.read("d.json", (folder / "yugo-vostok.json").read_bytes())

    assert requests["summary"].startswith("56 заявок формата загрузки")
    assert engineers["summary"].startswith("11 инженеров формата загрузки")
    assert dataset["summary"].startswith("83 заявки и 12 инженеров формата загрузки")


@pytest.mark.parametrize(
    ("name", "data", "status"),
    [
        ("план.xlsx", b"PK\x03\x04", 415),
        ("фото.png", b"\x89PNG", 415),
        ("без расширения", b"text", 415),
        ("большой.csv", b"a" * (attachments.MAX_BYTES + 1), 413),
        # «Текст Юникод» из Excel: cp1251 разобрал бы его молча в мусор.
        ("юникод.txt", "Соколов".encode("utf-16"), 415),
        ("двоичный.csv", b"a\x00b", 415),
    ],
    ids=["xlsx", "png", "no-suffix", "too-big", "utf-16", "binary"],
)
def test_other_files_are_refused(name, data, status):
    with pytest.raises(attachments.Refused) as refused:
        attachments.read(name, data)

    assert refused.value.status == status


def test_long_text_is_cut_and_says_so(monkeypatch):
    monkeypatch.setattr(settings, "assistant_attachment_chars", 10)

    read = attachments.read("длинный.txt", ("строка\n" * 5).encode())

    assert read["text"] == "строка\nстр\n…обрезано"
    assert read["summary"].endswith("модели — первые 10 знаков")


def test_upload_stores_the_text_for_a_day(mongo):
    response = TestClient(app).post(
        "/api/assistant/attachments",
        files={"file": ("список.csv", CSV.encode("utf-8"), "text/csv")},
    )

    assert response.status_code == 200
    body = response.json()
    assert (body["name"], body["kind"], body["summary"]) == (
        "список.csv",
        "csv",
        "CSV, 3 строки",
    )
    stored = mongo["attachments"].find_one({"_id": body["id"]})
    assert stored["text"] == CSV
    db.ensure_indexes()
    ttl = [
        index
        for index in mongo["attachments"].list_indexes()
        if index.get("expireAfterSeconds") is not None
    ]
    assert [(i["key"], i["expireAfterSeconds"]) for i in ttl] == [
        ({"createdAt": 1}, 24 * 3600)
    ]


def test_upload_refusal_answers_with_its_status(mongo):
    response = TestClient(app).post(
        "/api/assistant/attachments",
        files={"file": ("план.xlsx", b"PK", "application/octet-stream")},
    )

    assert response.status_code == 415
    assert "CSV, JSON и TXT" in response.json()["detail"]
    assert mongo["attachments"].count_documents({}) == 0


def test_partial_header_is_not_the_upload_format():
    read = attachments.read("частичный.csv", "id;address\n1;Москва\n".encode())

    assert read["summary"] == "CSV, 2 строки"


def test_trailing_empty_column_from_excel_still_is_the_upload_format():
    folder = settings.data_dir / "examples"
    lines = (folder / "yugotsentr-engineers.csv").read_text("utf-8").splitlines()
    text = "\n".join([lines[0] + ";", *lines[1:]])

    read = attachments.read("e.csv", text.encode())

    assert read["summary"] == "11 инженеров формата загрузки"
