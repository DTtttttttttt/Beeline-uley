"""Ручное переназначение (PLAN 6.16, блок 19).

Valhalla и 2ГИС не вызываются: таблицы переездов собираются фикстурами, линия — подменой
`valhalla.route`. Mongo нужна только тестам эндпоинта.

Базовый день фикстуры `day`: три заявки и две одинаковые бригады, поэтому перебор ТЗ
отдаёт все три первой, а вторая простаивает — есть куда переносить. Переезд 10 минут и
5 км между любыми точками, работа 60 минут: выезды 09:00, 10:10 и 11:20.
"""

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.data import seed
from app.main import app
from app.models import (
    Algorithm,
    CancelRequestEvent,
    Change,
    DeferRequestEvent,
    EngineerUnavailableEvent,
    ReplanMode,
)
from app.planning import manual, replan, service, validator
from app.routing import matrices, valhalla

# Окно на весь день: блок 19 проверяет перенос и отказы, а не границы окна — их разбирает
# test_schedule.py в обоих режимах WINDOW_RULE. Узкое окно ставится там, где оно и нужно.
WIDE = {"windowStart": "09:00", "windowEnd": "17:00"}


@pytest.fixture
def day(make_request, make_engineer, make_dataset, make_travel):
    """Набор, таблицы и базовый план по ним."""

    def make(brigades=None, requests=None, legs=None):
        requests = requests or [
            make_request(str(number), **WIDE) for number in (1, 2, 3)
        ]
        brigades = brigades or [make_engineer("brigade-1"), make_engineer("brigade-2")]
        dataset = make_dataset(requests, brigades)
        travel = make_travel(requests, legs=legs, engineers=brigades)
        return dataset, travel, service.build_plan(dataset, travel)

    return make


def moved(dataset, travel, parent, request_id, engineer_id="brigade-2", position=None):
    """Ручная правка одним вызовом: состояние дня, перенос, сборка новой версии."""
    state = manual.day(parent, travel)
    edit = manual.move(state, travel, request_id, engineer_id, position)
    return service.build_plan(dataset, travel, Algorithm.MANUAL, state, None, edit)


def refusal(dataset, travel, parent, request_id, engineer_id, position=None):
    with pytest.raises(replan.ReplanError) as error:
        moved(dataset, travel, parent, request_id, engineer_id, position)
    return error.value


def ids(plan, engineer_id):
    route = next(item for item in plan.routes if item.engineer_id == engineer_id)
    return [stop.request_id for stop in route.stops]


# --- успешный перенос (PLAN 6.16, шаги 2, 5) ---------------------------------


def test_request_moves_to_the_chosen_brigade(day):
    """Новая версия: заявка у выбранной бригады, закрепление записано, план валиден."""
    dataset, travel, parent = day()

    plan = moved(dataset, travel, parent, "2")

    assert ids(parent, "brigade-1") == ["1", "2", "3"]
    assert ids(plan, "brigade-1") == ["1", "3"]
    assert ids(plan, "brigade-2") == ["2"]
    assert plan.assignments["2"] == "brigade-2"
    assert plan.pinned == {"2": "brigade-2"}
    assert plan.algorithm is Algorithm.MANUAL
    assert plan.parent_plan_id == parent.id
    assert plan.validation.ok


def test_move_is_visible_in_the_diff(day):
    """Правка сравнивается с родителем тем же кодом, что и событие (PLAN 6.13)."""
    dataset, travel, parent = day()

    plan = moved(dataset, travel, parent, "2")

    changes = {item.request_id: item for item in plan.diff.requests}
    assert changes["2"].change is Change.REASSIGNED
    assert (changes["2"].from_engineer, changes["2"].to_engineer) == (
        "brigade-1",
        "brigade-2",
    )


def test_position_puts_the_stop_exactly_there(day):
    """Позиция считается по маршруту на экране и без самой переносимой заявки."""
    dataset, travel, parent = day()

    plan = moved(dataset, travel, parent, "3", "brigade-1", position=1)

    assert ids(plan, "brigade-1") == ["1", "3", "2"]
    assert plan.validation.ok


def test_best_position_is_taken_without_a_position(day):
    """`position: null` — лучшая вставка: при равном приросте это более ранняя (PLAN 6.2)."""
    dataset, travel, parent = day()

    plan = moved(dataset, travel, parent, "3", "brigade-1")

    assert ids(plan, "brigade-1") == ["3", "1", "2"]


def test_unassign_leaves_the_request_without_a_brigade(day):
    """`engineerId: null` — снять. Закреплять заявку не за кем, ключ уходит из `pinned`."""
    dataset, travel, parent = day()
    pinned = moved(dataset, travel, parent, "2")

    plan = moved(dataset, travel, pinned, "2", None)

    assert ids(plan, "brigade-2") == []
    assert plan.assignments["2"] is None
    assert [item.request_id for item in plan.unassigned] == ["2"]
    assert plan.pinned == {}
    assert plan.validation.ok


# --- отказы по ограничениям (PLAN 6.16, шаг 4) -------------------------------


def test_refusal_by_skill(day, make_engineer):
    dataset, travel, parent = day(
        brigades=[
            make_engineer("brigade-1"),
            make_engineer("brigade-2", skills=["connection"]),
        ]
    )

    error = refusal(dataset, travel, parent, "2", "brigade-2")

    assert error.status == 409
    assert str(error) == "Нельзя: Бригада 2 — нет навыка «Локальные работы»"


def test_refusal_by_equipment(day, make_request, make_engineer):
    dataset, travel, parent = day(
        requests=[
            make_request("1", **WIDE),
            make_request("2", requiredEquipment=["router"], **WIDE),
            make_request("3", **WIDE),
        ],
        brigades=[
            make_engineer("brigade-1", equipment=["router"]),
            make_engineer("brigade-2"),
        ],
    )

    error = refusal(dataset, travel, parent, "2", "brigade-2")

    assert error.status == 409
    assert "нет оборудования «Роутер»" in str(error)


def test_refusal_by_tool(day, make_request, make_engineer):
    dataset, travel, parent = day(
        requests=[
            make_request("1", **WIDE),
            make_request("2", requiredTools=["crimping_tool"], **WIDE),
            make_request("3", **WIDE),
        ],
        brigades=[
            make_engineer("brigade-1", tools=["crimping_tool"]),
            make_engineer("brigade-2"),
        ],
    )

    error = refusal(dataset, travel, parent, "2", "brigade-2")

    assert error.status == 409
    assert "Бригада 2 — нет инструмента «Обжимной инструмент»" in str(error)


def test_refusal_by_transport(day, make_request, make_engineer):
    dataset, travel, parent = day(
        requests=[
            make_request("1", **WIDE),
            make_request("2", requiredTransport="bicycle", **WIDE),
            make_request("3", **WIDE),
        ],
        brigades=[
            make_engineer("brigade-1", transport="bicycle"),
            make_engineer("brigade-2", transport="car"),
        ],
    )

    error = refusal(dataset, travel, parent, "2", "brigade-2")

    assert error.status == 409
    assert "транспорт «Автомобиль»" in str(error)
    assert "нужен «Велосипед»" in str(error)


def test_refusal_by_shift_names_the_hour(day, make_engineer):
    """Смена кончается раньше, чем работа: в тексте — обе цифры, других мест нет."""
    dataset, travel, parent = day(
        brigades=[
            make_engineer("brigade-1"),
            make_engineer("brigade-2", shiftStart="09:00", shiftEnd="10:00"),
        ]
    )

    error = refusal(dataset, travel, parent, "2", "brigade-2")

    assert error.status == 409
    assert str(error).startswith("Нельзя: Бригада 2 — ")
    assert "закончилась бы в 10:10, смена до 10:00" in str(error)
    # Мест в её маршруте нет вовсе, и приписка про них была бы шумом (PLAN 6.16, шаг 4).
    assert "Поставить можно" not in str(error)


def test_refusal_by_window_lists_allowed_spots(day, make_request):
    """Вставка в конец ломает окно, а в начало — нет: отказ называет и причину, и место."""
    dataset, travel, parent = day(
        requests=[
            make_request("8", windowStart="09:00", windowEnd="09:30"),
            make_request("9", **WIDE),
        ]
    )
    # Сначала уводим «9» второй бригаде, чтобы у неё было куда и перед чем вставлять.
    with_route = moved(dataset, travel, parent, "9")

    error = refusal(dataset, travel, with_route, "8", "brigade-2", position=1)

    assert error.status == 409
    assert "заявка №8 началась бы в 10:20, окно до 09:30" in str(error)
    assert "Поставить можно: перед №9" in str(error)


def test_refusal_when_the_old_route_falls_apart(day):
    """Снятие среднего стопа может оставить прежнюю бригаду без пути (PLAN 3.4)."""
    dataset, travel, parent = day(legs={("1", "3"): None})

    error = refusal(dataset, travel, parent, "2", "brigade-2")

    assert error.status == 409
    assert "без заявки №2 маршрут не строится — Бригада 1" in str(error)
    assert "до заявки №3 не добраться" in str(error)


def test_unknown_request_and_engineer_are_rejected(day):
    dataset, travel, parent = day()

    assert refusal(dataset, travel, parent, "нет такой", "brigade-2").status == 400
    assert refusal(dataset, travel, parent, "2", "brigade-нет").status == 400
    # Снимать то, что и так не назначено, — пустая версия плана.
    plan = moved(dataset, travel, parent, "2", None)
    assert refusal(dataset, travel, plan, "2", None).status == 400


# --- прожитый день: закреплённое и перенесённое (PLAN 6.16, шаг 1) -----------


@pytest.fixture
def after_event(day):
    """День после события в 09:30: первая заявка закреплена, остальные свободны.

    Выезды базового дня — 09:00, 10:10 и 11:20, поэтому к 09:30 бригада успела выехать
    ровно к одной заявке.
    """

    def make(event=None):
        dataset, travel, parent = day()
        event = event or CancelRequestEvent(
            type="cancel_request", time="09:30", request_id="3"
        )
        state = replan.prepare(parent, event, ReplanMode.FROM_EVENT, travel)
        return (
            dataset,
            travel,
            service.build_plan(dataset, travel, parent.algorithm, state),
        )

    return make


def test_committed_request_cannot_be_moved(after_event):
    dataset, travel, parent = after_event()

    error = refusal(dataset, travel, parent, "1", "brigade-2")

    assert error.status == 409
    assert "её выполняет Бригада 1" in str(error)


def test_position_inside_the_committed_prefix_is_rejected(after_event):
    """Закреплённые стопы перестановке не подлежат: перед ними вставить нельзя."""
    dataset, travel, parent = after_event()

    error = refusal(dataset, travel, parent, "2", "brigade-1", position=0)

    assert error.status == 409
    assert "закреплены" in str(error)


def test_deferred_request_cannot_be_moved(after_event):
    dataset, travel, parent = after_event(
        DeferRequestEvent(type="defer_request", time="09:30", request_id="3")
    )

    error = refusal(dataset, travel, parent, "3", "brigade-2")

    assert error.status == 409
    assert "перенесена на следующий день" in str(error)


def test_committed_part_survives_the_manual_edit(after_event):
    """Правка идёт поверх прожитого дня: закреплённый стоп и его времена не двигаются."""
    dataset, travel, parent = after_event()
    was = parent.routes[0].stops[0]

    plan = moved(dataset, travel, parent, "2", "brigade-2")

    stop = plan.routes[0].stops[0]
    assert ids(plan, "brigade-1") == ["1"]
    assert ids(plan, "brigade-2") == ["2"]
    assert stop.committed
    assert (stop.departure, stop.start, stop.end) == (
        was.departure,
        was.start,
        was.end,
    )
    assert plan.event is parent.event and plan.replan_mode is parent.replan_mode
    assert plan.validation.ok


def test_full_day_recalculation_is_not_shifted_to_the_event(day):
    """В режиме `full` день считается от начала смен: момента события в нём нет.

    Взять `T` из события такого плана значило бы сдвинуть на него все маршруты разом, а
    независимая проверка считает их от начала смены — она это и поймает (PLAN 6.12).
    """
    dataset, travel, parent = day()
    state = replan.prepare(
        parent,
        CancelRequestEvent(type="cancel_request", time="10:00", request_id="3"),
        ReplanMode.FULL,
        travel,
    )
    whole = service.build_plan(dataset, travel, parent.algorithm, state)

    plan = moved(dataset, travel, whole, "2", "brigade-2")

    assert plan.replan_mode is ReplanMode.FULL
    assert plan.routes[0].stops[0].departure == "09:00"
    assert plan.validation.ok


def test_refusal_survives_a_culprit_without_a_route(day, make_engineer):
    """Заявка и не по навыку, и недостижима: наружу уходит отказ, а не падение расчёта.

    `first_violation` отдаёт более ранний отказ по ресурсу, но маршрут при этом оборван
    непроходимым переездом — судить о временах по коду нарушения нельзя (PLAN 6.2).
    """
    dataset, travel, parent = day(
        brigades=[
            make_engineer("brigade-1"),
            make_engineer("brigade-2", skills=["connection"]),
        ],
        legs={("office", "2"): None},
    )

    error = refusal(dataset, travel, parent, "2", "brigade-2")

    assert error.status == 409
    assert "нет навыка" in str(error)


def test_unavailable_brigade_refuses_by_its_own_reason(day):
    """Выбывшей бригаде нельзя ничего, и причина у этого своя — не «не помещается в окно»."""
    dataset, travel, parent = day()
    left = replanned(
        dataset,
        travel,
        parent,
        EngineerUnavailableEvent(
            type="engineer_unavailable", time="09:30", engineer_id="brigade-2"
        ),
    )

    error = refusal(dataset, travel, left, "3", "brigade-2")

    assert error.status == 409
    assert str(error) == (
        "Нельзя: Бригада 2 — с 09:30 не работает, новых заявок не берёт"
    )


# --- закрепление при перепланировании (PLAN 6.16, п. 6; пункт 19.3) ----------


def replanned(dataset, travel, parent, event, algorithm=Algorithm.BASELINE):
    state = replan.prepare(parent, event, ReplanMode.FROM_EVENT, travel)
    return service.build_plan(dataset, travel, algorithm, state)


def test_pinned_request_stays_with_its_brigade(day):
    """Без закрепления перебор ТЗ вернул бы заявку первой бригаде — с ним не возвращает."""
    dataset, travel, parent = day()
    event = CancelRequestEvent(type="cancel_request", time="08:00", request_id="3")

    free = replanned(dataset, travel, parent, event)
    pinned = replanned(dataset, travel, moved(dataset, travel, parent, "2"), event)

    assert free.assignments["2"] == "brigade-1"
    assert pinned.assignments["2"] == "brigade-2"
    assert pinned.pinned == {"2": "brigade-2"}
    assert pinned.validation.ok


def test_pinned_request_stays_with_its_brigade_in_the_optimizer(day, monkeypatch):
    """Закрепление доезжает и до модели OR-Tools — через домен переменной бригады."""
    monkeypatch.setattr(settings, "replan_time_limit_sec", 1)
    dataset, travel, parent = day()

    plan = replanned(
        dataset,
        travel,
        moved(dataset, travel, parent, "2"),
        CancelRequestEvent(type="cancel_request", time="08:00", request_id="3"),
        algorithm=Algorithm.OPTIMIZED,
    )

    assert plan.assignments["2"] == "brigade-2"
    assert plan.validation.ok


def test_pin_is_released_when_the_brigade_leaves(day):
    """Выбывшая бригада снимает закрепление: работать по нему некому (PLAN 6.16, п. 6)."""
    dataset, travel, parent = day()

    plan = replanned(
        dataset,
        travel,
        moved(dataset, travel, parent, "2"),
        EngineerUnavailableEvent(
            type="engineer_unavailable", time="08:00", engineer_id="brigade-2"
        ),
    )

    assert plan.assignments["2"] == "brigade-1"
    assert plan.pinned == {}
    assert {item.request_id: item.change for item in plan.diff.requests}["2"] is (
        Change.REASSIGNED
    )


def test_pin_is_dropped_when_nobody_performed_it(day, make_engineer):
    """Закрепление, которое не сработало, в плане не остаётся: диспетчеру не врут."""
    dataset, travel, parent = day(
        brigades=[
            make_engineer("brigade-1"),
            make_engineer("brigade-2", shiftStart="09:00", shiftEnd="18:00"),
        ]
    )
    pinned = moved(dataset, travel, parent, "2")
    # Бригада на месте, но день у неё теперь занят: заявка остаётся без исполнителя.
    short = [
        item.model_copy(update={"shift_end": "09:30"})
        if item.id == "brigade-2"
        else item
        for item in dataset.engineers
    ]
    pinned.input.engineers = short

    plan = replanned(
        dataset,
        travel,
        pinned,
        CancelRequestEvent(type="cancel_request", time="08:00", request_id="3"),
    )

    assert plan.assignments["2"] is None
    assert plan.pinned == {}


def test_manual_version_is_replanned_by_the_previous_algorithm(day, mongo, monkeypatch):
    """`manual` — отметка правки, а не способ расчёта: событие считает алгоритм предка."""
    dataset, travel, _ = day()
    monkeypatch.setattr(matrices, "get", lambda _: travel)
    monkeypatch.setattr(valhalla, "route", lambda locations, costing: [list(locations)])

    parent = service.create_plan(dataset)
    edited = service.manual_plan(parent, dataset, "2", "brigade-2", None)
    plan = service.replan_plan(
        edited,
        dataset,
        CancelRequestEvent(type="cancel_request", time="08:00", request_id="3"),
        ReplanMode.FROM_EVENT,
    )

    assert edited.algorithm is Algorithm.MANUAL
    assert plan.algorithm is Algorithm.BASELINE
    assert plan.assignments["2"] == "brigade-2"


# --- независимая проверка (PLAN 6.7) -----------------------------------------


def test_validator_catches_a_pin_nobody_honoured(day):
    """Подделка: в плане написано «закреплена за второй», а работает по ней первая."""
    _, travel, parent = day()
    parent.pinned = {"1": "brigade-2"}

    result = validator.validate(parent, travel)

    assert not result.ok
    assert any("закреплена за brigade-2" in error for error in result.errors)


# --- эндпоинт (PLAN 5.4) -----------------------------------------------------


@pytest.fixture
def client(mongo, day, monkeypatch):
    """Приложение с маленьким текущим набором, без Valhalla и без встроенных данных."""
    dataset, travel, _ = day()
    monkeypatch.setattr(seed, "current", lambda: dataset)
    monkeypatch.setattr(seed, "by_id", lambda _: dataset)
    monkeypatch.setattr(matrices, "get", lambda _: travel)
    monkeypatch.setattr(valhalla, "route", lambda locations, costing: [list(locations)])
    return TestClient(app)


def plan_id(client) -> str:
    return client.post("/api/plans", json={"algorithm": "baseline"}).json()["id"]


def test_manual_endpoint_returns_a_new_version(client):
    response = client.post(
        f"/api/plans/{plan_id(client)}/manual",
        json={"requestId": "2", "engineerId": "brigade-2"},
    )

    assert response.status_code == 200
    plan = response.json()
    assert plan["algorithm"] == "manual"
    assert plan["pinned"] == {"2": "brigade-2"}
    assert plan["validation"]["ok"]
    assert service.get_plan(plan["id"]) is not None


def test_manual_endpoint_reports_the_refusal(client):
    response = client.post(
        f"/api/plans/{plan_id(client)}/manual",
        json={"requestId": "2", "engineerId": "brigade-2", "position": 99},
    )

    assert response.status_code == 400
    assert "позиции" in response.json()["detail"]


def test_manual_endpoint_checks_the_plan_and_the_body(client):
    assert (
        client.post("/api/plans/нет-такого/manual", json={"requestId": "2"}).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/plans/{plan_id(client)}/manual",
            json={"requestId": "2", "engineerId": "brigade-2", "лишнее": 1},
        ).status_code
        == 422
    )


def test_refusal_by_equipment_capacity(day, make_request, make_engineer, monkeypatch):
    """Вид есть, но места нет: отказ называет вместимость, а не отсутствие роутера."""
    monkeypatch.setattr(settings, "equipment_capacity", {"public_transport": 0})
    dataset, travel, parent = day(
        requests=[
            make_request(str(number), requiredEquipment=["router"], **WIDE)
            for number in (1, 2, 3)
        ],
        brigades=[
            make_engineer("brigade-1", equipment=["router"]),
            make_engineer(
                "brigade-2", transport="public_transport", equipment=["router"]
            ),
        ],
    )

    error = refusal(dataset, travel, parent, "2", "brigade-2")

    assert error.status == 409
    assert str(error) == (
        "Нельзя: Бригада 2 — оборудования в маршруте стало бы больше 0 ед. — столько "
        "на транспорте «Пешеход / общественный транспорт» за день не унести"
    )
