"""Сценарии и история версий (PLAN 6.17, блок 20).

Valhalla и 2ГИС не вызываются: таблицы переездов собираются фикстурами, линия — подменой
`valhalla.route`. Mongo нужна почти всем: и сценарии, и история живут в базе.

Базовый день фикстуры `day`: три заявки и две одинаковые бригады, поэтому перебор ТЗ
отдаёт все три первой. Переезд 10 минут и 5 км, работа 60 минут: выезды 09:00, 10:10
и 11:20. Событие в 10:30 застаёт третью заявку не начатой, а первую — уже сделанной.
"""

import json
import shutil

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db
from app.config import settings
from app.data import seed
from app.main import app
from app.models import (
    AddRequestEvent,
    Algorithm,
    CancelRequestEvent,
    EngineerUnavailableEvent,
    ReplanMode,
    Scenario,
    Validation,
)
from app.planning import diff, scenarios, service
from app.routing import matrices, valhalla
from app.timeutil import to_seconds

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


def store(dataset_id: str, events, scenario_id="demo", name="Демонстрация") -> Scenario:
    scenario = Scenario(id=scenario_id, dataset_id=dataset_id, name=name, events=events)
    db.scenarios.insert_one(seed.scenario_to_document(scenario))
    return scenario


# --- цепочка событий (PLAN 20.6) ---------------------------------------------


def test_three_events_make_three_versions(stand, make_request):
    dataset, _, plan = stand
    scenario = store(dataset.id, chain(make_request))

    versions = scenarios.run(plan, dataset, scenario)

    assert len(versions) == 3
    for version in versions:
        assert version.validation.errors == []
    # Каждая версия — потомок предыдущей, первая — потомок исходного плана.
    assert [version.parent_plan_id for version in versions] == [
        plan.id,
        versions[0].id,
        versions[1].id,
    ]
    assert [version.event.type for version in versions] == [
        "cancel_request",
        "engineer_unavailable",
        "add_request",
    ]
    # Режим — как у события без выбора диспетчера: обычная заявка встраивается (блок 30).
    assert [version.replan_mode for version in versions] == [
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


# --- запуск сценария (PLAN 20.4) ---------------------------------------------


def test_scenario_endpoint_returns_every_version(client, stand, make_request):
    dataset, _, plan = stand
    store(dataset.id, chain(make_request))

    response = client.post(f"/api/plans/{plan.id}/scenarios/demo")

    assert response.status_code == 200
    versions = response.json()
    assert len(versions) == 3
    for version in versions:
        assert version["validation"]["ok"]
        # Версии сохранены: любую можно открыть по её идентификатору.
        assert service.get_plan(version["id"]) is not None


def test_scenario_stops_on_a_refusal_and_names_the_event(client, stand):
    """Отменить начатую работу нельзя — и видно, какое именно событие не применилось."""
    dataset, _, plan = stand
    store(
        dataset.id,
        [
            CancelRequestEvent(type="cancel_request", time="10:30", request_id="3"),
            CancelRequestEvent(type="cancel_request", time="11:00", request_id="1"),
        ],
    )

    response = client.post(f"/api/plans/{plan.id}/scenarios/demo")

    assert response.status_code == 409
    assert "событие 2 из 2" in response.json()["detail"]
    assert "в 11:00" in response.json()["detail"]


def test_chain_stops_on_a_version_that_failed_validation(
    stand, make_request, monkeypatch
):
    """Не прошедшую проверку версию `_finish` не сохраняет — считать от неё следующее событие
    значит наплодить версии с `parentPlanId` в никуда."""
    dataset, _, plan = stand
    scenario = store(dataset.id, chain(make_request))
    honest = service.build_plan

    def broken(*args, **kwargs):
        version = honest(*args, **kwargs)
        version.validation = Validation(ok=False, errors=["подделка"])
        return version

    monkeypatch.setattr(service, "build_plan", broken)

    versions = scenarios.run(plan, dataset, scenario)

    assert len(versions) == 1
    assert not versions[0].validation.ok
    # Оборванная версия действительно не сохранена, и потомков у неё нет.
    assert service.get_plan(versions[0].id) is None
    assert db.plans.count_documents({"parentPlanId": versions[0].id}) == 0


def test_scenario_of_another_dataset_is_refused(client, stand, make_request):
    _, _, plan = stand
    store("чужой-набор", chain(make_request))

    response = client.post(f"/api/plans/{plan.id}/scenarios/demo")

    assert response.status_code == 400
    assert "другого набора" in response.json()["detail"]


def test_unknown_scenario_and_unknown_plan(client, stand, make_request):
    dataset, _, plan = stand
    store(dataset.id, chain(make_request))

    assert client.post(f"/api/plans/{plan.id}/scenarios/нет").status_code == 404
    assert client.post("/api/plans/нет-такого/scenarios/demo").status_code == 404


def test_scenarios_endpoint_lists_only_the_current_dataset(client, stand, make_request):
    dataset, _, _ = stand
    store(dataset.id, chain(make_request), scenario_id="свой", name="Свой")
    store("чужой-набор", chain(make_request), scenario_id="чужой", name="Чужой")

    response = client.get("/api/scenarios")

    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == ["свой"]


# --- история версий (PLAN 20.2, 20.5) ----------------------------------------


def test_history_of_a_fresh_plan_is_a_single_version(client, stand):
    _, _, plan = stand

    history = client.get(f"/api/plans/{plan.id}/history").json()

    assert [version["id"] for version in history["versions"]] == [plan.id]
    assert history["versions"][0]["event"] is None
    # Сравнивать не с чем: версия одна.
    assert history["diff"] is None


def test_history_walks_up_to_the_first_version(client, stand, make_request):
    dataset, _, plan = stand
    store(dataset.id, chain(make_request))
    versions = client.post(f"/api/plans/{plan.id}/scenarios/demo").json()

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
    dataset, _, plan = stand
    store(dataset.id, chain(make_request))
    versions = client.post(f"/api/plans/{plan.id}/scenarios/demo").json()

    history = client.get(f"/api/plans/{versions[0]['id']}/history").json()

    assert [item["id"] for item in history["versions"]] == [plan.id, versions[0]["id"]]


def test_history_diff_compares_the_first_version_not_the_parent(
    client, stand, make_request
):
    dataset, _, plan = stand
    store(dataset.id, chain(make_request))
    versions = client.post(f"/api/plans/{plan.id}/scenarios/demo").json()
    last = service.get_plan(versions[-1]["id"])

    history = client.get(f"/api/plans/{last.id}/history").json()

    expected = diff.compare(service.get_plan(plan.id), last)
    assert history["diff"] == expected.model_dump(by_alias=True, mode="json")
    # А `diff` последней версии — против её родителя, и это другое сравнение.
    assert history["diff"] != versions[-1]["diff"]


def test_history_of_an_unknown_plan_is_404(client):
    assert client.get("/api/plans/нет-такого/history").status_code == 404


# --- файлы сценариев (PLAN 20.3) ---------------------------------------------


def test_seeding_reads_the_scenario_files(mongo, tmp_path, monkeypatch):
    """Сценарии кладутся при старте и повторным запуском не задваиваются."""
    shutil.copytree(settings.data_dir / "seed", tmp_path / "seed")
    shutil.copytree(settings.data_dir / "scenarios", tmp_path / "scenarios")
    monkeypatch.setattr(settings, "data_dir", tmp_path)

    seed.ensure_seeded()
    seed.ensure_seeded()

    stored = seed.scenarios_for(seed.BUILTIN_ID)
    assert [item.id for item in stored] == [
        "brigades-out",
        "emergency-day",
        "urgent-day",
    ]
    assert db.scenarios.count_documents({}) == 3


def test_builtin_scenarios_fit_the_builtin_dataset():
    """Опечатка в идентификаторе иначе всплыла бы только на защите."""
    dataset = seed.load_builtin()
    requests = {request.id for request in dataset.requests}
    engineers = {engineer.id for engineer in dataset.engineers}

    stored = seed.load_scenarios()

    assert stored, "каталог data/scenarios пуст"
    for scenario in stored:
        times = [event.time for event in scenario.events]
        assert times == sorted(times), f"{scenario.id}: события идут не по времени"
        for event in scenario.events:
            for request_id in [
                *getattr(event, "request_ids", []),
                *filter(None, [getattr(event, "request_id", None)]),
            ]:
                assert request_id in requests, f"{scenario.id}: {request_id}"
            if getattr(event, "engineer_id", None):
                assert event.engineer_id in engineers, (
                    f"{scenario.id}: {event.engineer_id}"
                )
            if getattr(event, "request", None):
                assert event.request.id not in requests, (
                    f"{scenario.id}: идентификатор новой заявки уже занят"
                )


def test_emergency_day_gives_three_valid_versions(mongo, monkeypatch):
    """Три аварии днём поверх утреннего плана встроенного набора (ответ 5, PLAN 26.5).

    Valhalla недоступна: точки аварий считаются по прямой, остальное — по готовым
    матрицам. Кому достанется авария, решает эвристический поиск, поэтому здесь только
    то, что обещано всегда: каждая версия проходит проверку, а окно аварии — не длиннее
    срока реакции от момента поступления.
    """
    dataset = seed.load_builtin()
    travel = matrices.Travel(
        dataset.id,
        matrices.points(dataset),
        {
            document["profile"]: matrices.from_document(document)
            for document in seed.load_matrices()
        },
    )

    def unavailable(*_, **__):
        raise httpx.ConnectError("Valhalla не поднята")

    monkeypatch.setattr(seed, "current", lambda: dataset)
    monkeypatch.setattr(seed, "by_id", lambda _: dataset)
    monkeypatch.setattr(matrices, "get", lambda _: travel)
    monkeypatch.setattr(valhalla, "matrix", unavailable)
    monkeypatch.setattr(valhalla, "route", lambda locations, costing: [list(locations)])
    monkeypatch.setattr(settings, "solver_time_limit_sec", 2)
    monkeypatch.setattr(settings, "replan_time_limit_sec", 1)
    (scenario,) = [item for item in seed.load_scenarios() if item.id == "emergency-day"]

    versions = scenarios.run(
        service.create_plan(dataset, Algorithm.OPTIMIZED), dataset, scenario
    )

    assert len(versions) == 3
    assert versions[0].approximate  # точка аварии — по прямой, Valhalla не вызывалась
    for version, event in zip(versions, scenario.events, strict=True):
        assert version.validation.errors == []
        # Авария пересчитывает остаток дня, а не встраивается (PLAN 6.12).
        assert version.replan_mode is ReplanMode.FROM_EVENT
        (request,) = [
            item for item in version.input.requests if item.id == event.request.id
        ]
        assert request.window_start == event.time
        assert to_seconds(request.window_end) - to_seconds(request.window_start) == (
            settings.emergency_response_min * 60
        )


def test_a_file_with_its_own_id_does_not_break_the_start(tmp_path, monkeypatch):
    """Сохранённый ответ `GET /api/scenarios` — файл ровно с этими полями, а `TypeError`
    из него прошёл бы мимо обработки в `main.lifespan` и не дал бы сервису подняться."""
    folder = tmp_path / "scenarios"
    folder.mkdir()
    (folder / "своё.json").write_text(
        json.dumps(
            {
                "id": "чужое",
                "datasetId": "чужой-набор",
                "name": "Сохранённый ответ",
                "events": [
                    {"type": "cancel_request", "time": "10:30", "requestId": "3"}
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "data_dir", tmp_path)

    stored = seed.load_scenarios()

    # Идентификатор и набор берутся у загрузчика, а не у файла.
    assert [(item.id, item.dataset_id) for item in stored] == [
        ("своё", seed.BUILTIN_ID)
    ]


def test_scenario_files_are_only_name_and_events():
    """`datasetId` и `id` проставляет загрузчик: в файле их нет (PLAN 5.3)."""
    for path in sorted((settings.data_dir / "scenarios").glob("*.json")):
        assert set(json.loads(path.read_text("utf-8"))) == {"name", "events"}
