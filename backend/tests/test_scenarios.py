"""Цепочка событий и история версий (PLAN 6.17, блок 20).

Valhalla и 2ГИС не вызываются: таблицы переездов собираются фикстурами, линия — подменой
`valhalla.route`. Mongo нужна всем: история живёт в базе.

Базовый день фикстуры `day`: три заявки и две одинаковые бригады, поэтому перебор ТЗ
отдаёт все три первой. Переезд 10 минут и 5 км, работа 60 минут: выезды 09:00, 10:10
и 11:20. Событие в 10:30 застаёт третью заявку не начатой, а первую — уже сделанной.
"""

import pytest
from fastapi.testclient import TestClient

from app.data import seed
from app.main import app
from app.models import (
    AddRequestEvent,
    CancelRequestEvent,
    EngineerUnavailableEvent,
)
from app.planning import diff, service
from app.routing import matrices, valhalla

# Окно на весь день: блок 20 проверяет цепочку, а не границы окна — их разбирает
# test_schedule.py в обоих режимах WINDOW_RULE.
WIDE = {"windowStart": "09:00", "windowEnd": "17:00"}


def chain(make_request) -> list:
    """Три события подряд: отмена, выбытие бригады, срочная заявка."""
    return [
        CancelRequestEvent(type="cancel_request", time="10:30", request_id="3"),
        EngineerUnavailableEvent(
            type="engineer_unavailable", time="11:00", engineer_id="brigade-2"
        ),
        AddRequestEvent(
            type="add_request", time="11:30", request=make_request("9", **WIDE)
        ),
    ]


@pytest.fixture
def day(make_request, make_engineer, make_dataset, make_travel):
    """Набор, таблицы и базовый план по ним.

    Точка заявки из события закладывается в таблицы заранее — иначе её считал бы
    `Travel.add_point`, то есть Valhalla.
    """

    def make():
        requests = [make_request(str(number), **WIDE) for number in (1, 2, 3)]
        brigades = [make_engineer("brigade-1"), make_engineer("brigade-2")]
        dataset = make_dataset(requests, brigades)
        travel = make_travel([*requests, make_request("9", **WIDE)], engineers=brigades)
        return dataset, travel, service.build_plan(dataset, travel)

    return make


@pytest.fixture
def stand(mongo, day, monkeypatch):
    """Набор, таблицы и сохранённый первичный план; Valhalla и 2ГИС подменены."""
    dataset, travel, _ = day()
    monkeypatch.setattr(seed, "current", lambda: dataset)
    monkeypatch.setattr(seed, "by_id", lambda _: dataset)
    monkeypatch.setattr(matrices, "get", lambda _: travel)
    monkeypatch.setattr(valhalla, "route", lambda locations, costing: [list(locations)])
    return dataset, travel, service.create_plan(dataset)


@pytest.fixture
def client(stand):
    return TestClient(app)


def apply(client, plan_id: str, events) -> list[dict]:
    """События по очереди, каждое — от версии предыдущего; ответ — все версии."""
    versions = []
    for event in events:
        response = client.post(
            f"/api/plans/{plan_id}/events",
            json={"event": event.model_dump(by_alias=True, mode="json")},
        )
        assert response.status_code == 200, response.text
        versions.append(response.json())
        plan_id = versions[-1]["id"]
    return versions


# --- цепочка событий (PLAN 20.6) ---------------------------------------------


def test_three_events_make_three_versions(client, stand, make_request):
    _, _, plan = stand

    versions = apply(client, plan.id, chain(make_request))

    for version in versions:
        assert version["validation"]["errors"] == []
    # Каждая версия — потомок предыдущей, первая — потомок исходного плана.
    assert [version["parentPlanId"] for version in versions] == [
        plan.id,
        versions[0]["id"],
        versions[1]["id"],
    ]
    # Режим — как у события без выбора диспетчера: обычная заявка встраивается (блок 30).
    assert [version["replanMode"] for version in versions] == [
        "from_event",
        "from_event",
        "insert",
    ]


def test_event_without_mode_takes_the_default_of_its_type(client, stand):
    """API: обычная заявка без `mode` встраивается, отмена пересчитывает остаток."""
    _, _, plan = stand
    body = {
        "type": "add_request",
        "time": "10:30",
        "request": {
            "id": "9",
            "address": "адрес",
            "lat": 55.71,
            "lon": 37.78,
            "serviceDurationMin": 60,
            "baseNormMin": 80,
            **WIDE,
            "workPriority": "regular",
            "skill": "local",
        },
    }

    added = client.post(f"/api/plans/{plan.id}/events", json={"event": body}).json()
    cancelled = client.post(
        f"/api/plans/{plan.id}/events",
        json={"event": {"type": "cancel_request", "time": "10:30", "requestId": "3"}},
    ).json()

    assert added["replanMode"] == "insert"
    assert added["input"]["requests"][-1]["urgent"] is False
    assert cancelled["replanMode"] == "from_event"


def test_event_earlier_than_the_previous_one_is_refused(client, stand):
    """Время назад «разморозило» бы работу, которую прошлая версия объявила начатой (PLAN 20.1)."""
    _, _, plan = stand
    first = client.post(
        f"/api/plans/{plan.id}/events",
        json={"event": {"type": "cancel_request", "time": "10:30", "requestId": "3"}},
    ).json()

    response = client.post(
        f"/api/plans/{first['id']}/events",
        json={
            "event": {
                "type": "engineer_unavailable",
                "time": "09:30",
                "engineerId": "brigade-2",
            }
        },
    )

    assert response.status_code == 400
    assert "09:30" in response.json()["detail"]
    assert "10:30" in response.json()["detail"]


# --- история версий (PLAN 20.2, 20.5) ----------------------------------------


def test_history_of_a_fresh_plan_is_a_single_version(client, stand):
    _, _, plan = stand

    history = client.get(f"/api/plans/{plan.id}/history").json()

    assert [version["id"] for version in history["versions"]] == [plan.id]
    assert history["versions"][0]["event"] is None
    # Сравнивать не с чем: версия одна.
    assert history["diff"] is None


def test_history_walks_up_to_the_first_version(client, stand, make_request):
    _, _, plan = stand
    versions = apply(client, plan.id, chain(make_request))

    history = client.get(f"/api/plans/{versions[-1]['id']}/history").json()

    assert [item["id"] for item in history["versions"]] == [
        plan.id,
        *(version["id"] for version in versions),
    ]
    assert [
        item["event"] and item["event"]["time"] for item in history["versions"]
    ] == [
        None,
        "10:30",
        "11:00",
        "11:30",
    ]
    assert history["versions"][-1]["metrics"] == versions[-1]["metrics"]


def test_history_of_a_middle_version_stops_there(client, stand, make_request):
    """Вниз не спускаемся: у версии может быть несколько потомков, и цепочка стала бы деревом."""
    _, _, plan = stand
    versions = apply(client, plan.id, chain(make_request))

    history = client.get(f"/api/plans/{versions[0]['id']}/history").json()

    assert [item["id"] for item in history["versions"]] == [plan.id, versions[0]["id"]]


def test_history_diff_compares_the_first_version_not_the_parent(
    client, stand, make_request
):
    _, _, plan = stand
    versions = apply(client, plan.id, chain(make_request))
    last = service.get_plan(versions[-1]["id"])

    history = client.get(f"/api/plans/{last.id}/history").json()

    expected = diff.compare(service.get_plan(plan.id), last)
    assert history["diff"] == expected.model_dump(by_alias=True, mode="json")
    # А `diff` последней версии — против её родителя, и это другое сравнение.
    assert history["diff"] != versions[-1]["diff"]


def test_history_of_an_unknown_plan_is_404(client):
    assert client.get("/api/plans/нет-такого/history").status_code == 404
