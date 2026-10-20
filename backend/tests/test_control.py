"""День людей: сопоставление контрольной выборки и её расписание (блок 51, PLAN 6.10)."""

import json

import pytest

from app.config import settings
from app.data import seed
from app.models import Input
from app.planning.control import people_day
from scripts import prepare_data

NAMES = {"vostok": "Восток", "yugo-vostok": "Юго-восток", "yugotsentr": "Югоцентр"}


# --- сопоставление строк ------------------------------------------------------


@pytest.mark.parametrize("name", NAMES.values())
def test_every_control_row_finds_its_synthetic_request(name):
    """Строки идут в одном порядке: ключ совпал у каждой, в день людей попали все строки с
    бригадой, кроме отменённых."""
    rows, _ = prepare_data.read_csv(
        prepare_data.RAW / prepare_data.CSV_NAME.format(name)
    )
    control = prepare_data.read_control(name)

    assignment = prepare_data.control_assignment(name, control)

    expected = [
        (row["Заявка"].strip(), fact["Бригада"].strip())
        for row, fact in zip(rows, control, strict=True)
        if fact["Бригада"].strip() and fact["Статус BK"].strip() != "Отменена"
    ]
    assert list(assignment.items()) == expected
    assert len(expected) < len(control)  # отменённые на всех участках есть


def test_shifted_control_row_is_an_error():
    """Ключ не совпал — ошибка с номером строки, а не догадка, чья это заявка."""
    control = prepare_data.read_control("Восток")
    control[0], control[1] = control[1], control[0]

    with pytest.raises(ValueError, match="строка 1"):
        prepare_data.control_assignment("Восток", control)


# --- расписание дня людей -----------------------------------------------------


@pytest.fixture
def day(make_request, make_dataset, make_travel):
    """День людей на заданных заявках одной бригады; плечо между заявками — 10 мин и 5 км,
    из точки в неё же — 0, как на диагонали настоящей таблицы."""

    def make(*requests):
        legs = {(r.id, r.id): (0, 0) for r in requests}
        data = make_dataset(list(requests), [])
        return people_day(
            data,
            {r.id: "Бригада Соколов" for r in requests},
            make_travel(list(requests), legs=legs),
        )

    return make


def test_road_to_the_first_request_is_not_counted(day, make_request):
    run = day(
        make_request("1", windowStart="10:00", windowEnd="12:00"),
        make_request("2", windowStart="12:00", windowEnd="14:00"),
    )

    assert (run.assigned, run.km, run.late, run.over_shift) == (2, 5.0, 0, 0)


@pytest.mark.parametrize("rule", ["start_in_window", "fit_in_window"])
def test_unreachable_window_counts_as_late(day, make_request, monkeypatch, rule):
    """Вторая работа того же окна начнётся в 11:10 при окне до 10:30. Опоздание — по ТЗ
    (начало позже конца окна), от `WINDOW_RULE` нашего расчёта оно не зависит: первая
    работа кончается в 11:00, после окна, но опозданием не считается ни при каком правиле."""
    monkeypatch.setattr(settings, "window_rule", rule)
    run = day(
        make_request("1", windowStart="10:00", windowEnd="10:30"),
        make_request("2", windowStart="10:00", windowEnd="10:30"),
    )

    assert (run.engineers, run.late) == (1, 1)


def test_work_after_23_counts_as_over_shift(day, make_request):
    run = day(make_request("1", windowStart="22:30", windowEnd="23:00"))

    assert (run.late, run.over_shift) == (0, 1)


def test_requests_go_by_window_end_not_by_file(day, make_request):
    """Авария на весь день не встаёт первой: иначе утренняя заявка опоздала бы из-за нашего
    порядка, а не из-за людей."""
    run = day(
        make_request("1", windowStart="00:01", windowEnd="23:59"),
        make_request("2", windowStart="09:30", windowEnd="10:00"),
    )

    assert run.late == 0


# --- файл и наборы ------------------------------------------------------------


def test_control_file_matches_the_control_sample():
    """data/seed/control.json не устарел: пересчёт по контрольным CSV и готовым таблицам
    даёт то же на всех участках. Сводка — справка, а не вход: в `Input` её нет."""
    stored = json.loads(
        (settings.data_dir / "seed" / "control.json").read_text("utf-8")
    )

    assert set(stored) == set(seed.DEMO_REQUESTS)
    for requests_id, name in NAMES.items():
        source = settings.data_dir / seed.DEMO_REQUESTS[requests_id]
        assert prepare_data.control_summary(name, source) == stored[requests_id]
        assert stored[requests_id]["engineers"] == prepare_data.control_brigades(name)
    assert "control" not in Input.model_fields


@pytest.mark.parametrize("crew_id", seed.DEMO_CREWS)
def test_demo_pair_carries_the_control_of_its_requests(crew_id):
    """Контрольный день — про заявки участка, а не про состав."""
    for requests_id in seed.DEMO_REQUESTS:
        control = seed.demo_raw(requests_id, crew_id)["control"]
        assert control is not None
        assert control == seed.load_control(requests_id)
    assert seed.load_control("нет-такого") is None


def test_control_file_in_the_old_flat_format_fails_loudly(tmp_path, monkeypatch):
    """Прежний плоский файл не должен молча убрать строку у всех наборов."""
    from pydantic import ValidationError

    (tmp_path / "seed").mkdir()
    (tmp_path / "seed" / "control.json").write_text(
        json.dumps({"engineers": 12, "total": 66}), "utf-8"
    )
    monkeypatch.setattr(settings, "data_dir", tmp_path)

    with pytest.raises(ValidationError):
        seed.load_control("vostok")


def test_edited_demo_pair_gets_the_control_summary(mongo):
    """Правленая пара, как и встроенный набор, получает сводку участка при старте; её
    правки при этом не трогаются."""
    seed.load_demo("yugo-vostok", "reduced")
    pair = "demo:yugo-vostok:reduced"
    mongo.datasets.update_one(
        {"_id": pair},
        {"$set": {"control": None, "updatedAt": "2026-10-13", "name": "правлено"}},
    )

    seed.ensure_seeded()

    stored = seed.by_id(pair)
    assert stored.control == seed.load_control("yugo-vostok")
    assert stored.name == "правлено"
