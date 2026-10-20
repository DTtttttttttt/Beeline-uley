"""Предпросмотр события, черновики и пул процессов (PLAN 6.12, блок 53).

Valhalla и 2ГИС не вызываются: таблицы переездов собираются фикстурами, линия — подменой
`valhalla.route`. Переезд 10 минут и 5 км, работа 60 минут: выезды 09:00, 10:10 и 11:20,
поэтому событие в 10:30 застаёт первые две заявки начатыми, а третью — нет.

Варианты считаются в процессе теста (`SOLVER_WORKERS=1`): процесс пула подмен фикстур не
видит. Сам пул проверяет отдельный тест — на данных, которые передаются ему целиком.
"""

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
    Metrics,
    Plan,
    PreviewOption,
    PreviewSummary,
    ReplanMode,
    UpdateEngineerEvent,
    Validation,
)
from app.planning import parallel, service
from app.routing import matrices, valhalla

WIDE = {"windowStart": "09:00", "windowEnd": "17:00"}


@pytest.fixture(autouse=True)
def _in_process(monkeypatch):
    monkeypatch.setattr(settings, "solver_workers", 1)


@pytest.fixture
def stand(mongo, monkeypatch, make_dataset, make_travel):
    """Сохранённый первичный план по заявкам и бригадам; точки заявок событий — заранее."""

    def make(requests, engineers, extra, algorithm=Algorithm.BASELINE) -> Plan:
        dataset = make_dataset(requests, engineers)
        travel = make_travel([*requests, *extra], engineers=engineers)
        monkeypatch.setattr(seed, "by_id", lambda _: dataset)
        monkeypatch.setattr(matrices, "get", lambda _: travel)
        monkeypatch.setattr(
            valhalla, "route", lambda locations, costing: [list(locations)]
        )
        return service.create_plan(dataset, algorithm)

    return make


@pytest.fixture
def client(mongo):
    return TestClient(app)


@pytest.fixture
def ordinary(stand, make_request, make_engineer):
    """Три заявки у первой бригады, вторая простаивает. Третья — строго в 11:30."""
    requests = [
        make_request("1", **WIDE),
        make_request("2", **WIDE),
        make_request("3", windowStart="11:25", windowEnd="11:35"),
    ]
    late = make_request("9", windowStart="13:00", windowEnd="17:00")
    plan = stand(
        requests, [make_engineer("brigade-1"), make_engineer("brigade-2")], [late]
    )
    return plan, late


@pytest.fixture
def gain(stand, make_request, make_engineer):
    """Встраивание заявку не ставит, а пересчёт остатка ставит.

    Все три заявки утра у первой бригады — только она выходит к 09:00. Новая заявка требует
    подключения, которое умеет только первая, и её окно занято третьей. Пересчёт отдаёт
    третью второй бригаде (она с 10:00) и ставит новую на место третьей; встраивание никого
    не двигает, и новой места нет.
    """
    requests = [
        make_request("1", windowStart="09:00", windowEnd="09:15"),
        make_request("2", windowStart="10:15", windowEnd="10:25"),
        make_request("3", windowStart="11:25", windowEnd="11:35"),
    ]
    engineers = [
        make_engineer("brigade-1", skills=["local", "connection"]),
        make_engineer("brigade-2", shiftStart="10:00"),
    ]
    new = make_request("9", skill="connection", windowStart="11:25", windowEnd="11:35")
    return stand(requests, engineers, [new], Algorithm.OPTIMIZED), new


def adding(request, time="10:30") -> dict:
    event = AddRequestEvent(type="add_request", time=time, request=request)
    return event.model_dump(by_alias=True, mode="json")


def preview(client, plan_id: str, event: dict):
    return client.post(f"/api/plans/{plan_id}/events/preview", json={"event": event})


def modes(body: dict) -> list[str]:
    return [option["mode"] for option in body["options"]]


# --- черновики ---------------------------------------------------------------


def test_preview_drafts_stay_out_of_the_chain(ordinary, client):
    plan, late = ordinary
    body = preview(client, plan.id, adding(late)).json()
    draft = body["options"][0]["plan"]

    assert draft["draft"] is True
    assert draft["draftExpiresAt"]
    assert draft["parentPlanId"] == plan.id
    # Истории родителя черновик не виден: она идёт вверх по цепочке, а не вниз.
    history = client.get(f"/api/plans/{plan.id}/history").json()
    assert [version["id"] for version in history["versions"]] == [plan.id]


def test_draft_cannot_be_a_parent(ordinary, client):
    plan, late = ordinary
    draft = preview(client, plan.id, adding(late)).json()["options"][0]["plan"]
    cancel = CancelRequestEvent(type="cancel_request", time="10:40", request_id="3")

    event = client.post(
        f"/api/plans/{draft['id']}/events",
        json={"event": cancel.model_dump(by_alias=True, mode="json")},
    )
    manual = client.post(
        f"/api/plans/{draft['id']}/manual",
        json={"requestId": "3", "engineerId": "brigade-2"},
    )

    assert event.status_code == 409
    assert manual.status_code == 409
    assert "не применён" in event.json()["detail"]


def test_commit_makes_a_version_and_drops_the_other_drafts(gain, client):
    plan, new = gain
    body = preview(client, plan.id, adding(new)).json()
    chosen = next(
        option["plan"]
        for option in body["options"]
        if option["mode"] == body["recommended"]
    )
    others = [
        option["plan"]["id"]
        for option in body["options"]
        if option["plan"]["id"] != chosen["id"]
    ]
    assert others, "фикстура обязана дать больше одного варианта"

    committed = client.post(f"/api/plans/{chosen['id']}/commit")
    again = client.post(f"/api/plans/{chosen['id']}/commit")

    assert committed.status_code == 200
    assert committed.json()["draft"] is False
    assert committed.json()["draftExpiresAt"] is None
    # Содержимое плана не меняется: снимается только отметка черновика.
    assert committed.json()["routes"] == chosen["routes"]
    assert again.status_code == 200
    assert again.json()["id"] == chosen["id"]
    assert all(db.plans.find_one({"_id": other}) is None for other in others)
    # Применённый план — обычная версия: от него принимается следующее событие.
    cancel = CancelRequestEvent(type="cancel_request", time="10:40", request_id="9")
    after = client.post(
        f"/api/plans/{chosen['id']}/events",
        json={"event": cancel.model_dump(by_alias=True, mode="json")},
    )
    assert after.status_code == 200


def test_commit_keeps_another_preview_of_the_same_parent(ordinary, client):
    """Вторая вкладка считает своё событие от того же плана: применение в первой её варианты
    не трогает."""
    plan, late = ordinary
    mine = preview(client, plan.id, adding(late)).json()["options"][0]["plan"]
    cancel = CancelRequestEvent(type="cancel_request", time="10:30", request_id="3")
    theirs = preview(
        client, plan.id, cancel.model_dump(by_alias=True, mode="json")
    ).json()["options"]

    assert client.post(f"/api/plans/{mine['id']}/commit").status_code == 200
    assert all(
        db.plans.find_one({"_id": option["plan"]["id"], "draft": True})
        for option in theirs
    )


def test_invalid_variant_leaves_no_drafts(gain, client, monkeypatch):
    """Вариант не прошёл проверку — 500, и черновиков остальных вариантов в базе нет."""
    plan, new = gain
    real = service.build_plan

    def broken(*args, **kwargs):
        built = real(*args, **kwargs)
        if built.replan_mode is ReplanMode.FROM_EVENT:
            built.validation = Validation(ok=False, errors=["подделка"])
        return built

    monkeypatch.setattr(service, "build_plan", broken)
    response = preview(client, plan.id, adding(new))

    assert response.status_code == 500
    assert db.plans.count_documents({"draft": True}) == 0


def test_commit_of_an_expired_draft_is_404(client):
    response = client.post("/api/plans/no-such-draft/commit")
    assert response.status_code == 404
    assert "посчитайте заново" in response.json()["detail"]


def test_drafts_expire_by_ttl(mongo):
    db.ensure_indexes()
    index = db.plans.index_information()["draftExpiresAt_1"]
    assert index["expireAfterSeconds"] == 0


# --- варианты ----------------------------------------------------------------


def test_identical_variants_collapse_into_one(ordinary, client):
    """Заявке на 13:00 одно место — после третьей. Встраивание, пересчёт остатка и пересчёт
    дня дают одни маршруты, и выбирать не из чего."""
    plan, late = ordinary
    body = preview(client, plan.id, adding(late)).json()

    assert modes(body) == ["insert"]
    assert body["recommended"] == "insert"
    assert body["refused"] == {}


def test_recount_is_recommended_when_it_takes_more(gain, client):
    plan, new = gain
    body = preview(client, plan.id, adding(new)).json()
    options = {option["mode"]: option for option in body["options"]}

    assert body["recommended"] == "from_event"
    assert options["insert"]["summary"]["taken"] is None
    taken = options["from_event"]["summary"]["taken"]
    # Первая бригада заканчивает вторую заявку в 11:20, едет 10 минут: прибытие 11:30.
    assert taken == {"engineerId": "brigade-1", "arrivalMin": 60, "start": "11:30"}
    # Третья заявка уехала ко второй бригаде.
    assert options["from_event"]["summary"]["reassigned"] == 1


def test_mode_that_refuses_the_event_is_named(ordinary, client, make_engineer):
    """Сдвинуть начало смены, когда работа уже начата, остаток дня не может (409), а пересчёт
    всего дня — может: правила «выезд ≤ T» там нет (PLAN 6.12)."""
    plan, _ = ordinary
    event = UpdateEngineerEvent(
        type="update_engineer",
        time="10:30",
        engineer=make_engineer("brigade-1", shiftStart="09:30"),
    )
    body = preview(client, plan.id, event.model_dump(by_alias=True, mode="json")).json()

    assert modes(body) == ["full"]
    assert body["recommended"] == "full"
    assert set(body["refused"]) == {"from_event"}


def test_preview_refused_by_every_mode_answers_like_events(ordinary, client):
    """Отмена закрытой или начатой работы — 409 в любом режиме, как у `POST /events`."""
    plan, _ = ordinary
    cancel = CancelRequestEvent(type="cancel_request", time="10:30", request_id="1")
    response = preview(client, plan.id, cancel.model_dump(by_alias=True, mode="json"))
    direct = client.post(
        f"/api/plans/{plan.id}/events",
        json={"event": cancel.model_dump(by_alias=True, mode="json")},
    )

    assert response.status_code == direct.status_code == 409
    assert response.json()["detail"] == direct.json()["detail"]


def _option(mode: ReplanMode, assigned: int, emergencies: int = 0) -> PreviewOption:
    metrics = Metrics(
        assigned_count=assigned,
        assigned_by_work_priority={
            "emergency": emergencies,
            "new_connection": 0,
            "regular": assigned - emergencies,
        },
    )
    return PreviewOption(
        mode=mode, plan=Plan.model_construct(metrics=metrics), summary=PreviewSummary()
    )


@pytest.mark.parametrize(
    ("options", "default", "expected"),
    [
        # Без выигрыша — режим по умолчанию: обычная заявка встраивается (ответ 18).
        ([("insert", 5, 0), ("from_event", 5, 0)], "insert", "insert"),
        ([("insert", 5, 0), ("from_event", 6, 0)], "insert", "from_event"),
        # Аварии старше количества.
        ([("insert", 6, 0), ("from_event", 5, 1)], "insert", "from_event"),
        # Пересчёт дня сравнительный и не рекомендуется, даже когда берёт больше.
        ([("from_event", 5, 0), ("full", 7, 1)], "from_event", "from_event"),
        # Умолчание отклонило событие — первый оставшийся.
        ([("full", 5, 0)], "from_event", "full"),
    ],
)
def test_recommendation(options, default, expected):
    built = [_option(ReplanMode(mode), *counts) for mode, *counts in options]
    assert service.recommended(built, ReplanMode(default)) is ReplanMode(expected)


# --- пул процессов -----------------------------------------------------------


def test_pool_gives_the_same_plans_as_one_process(
    monkeypatch, make_request, make_engineer, make_dataset, make_travel
):
    """Повторяемость держит предел решений (блок 45), а не процесс: три процесса дают то же,
    что один."""
    requests = [make_request(str(number), **WIDE) for number in range(1, 7)]
    brigades = [make_engineer("brigade-1"), make_engineer("brigade-2")]
    dataset = make_dataset(requests, brigades)
    travel = make_travel(requests, engineers=brigades)
    jobs = [
        (dataset, travel, Algorithm.OPTIMIZED),
        (dataset, travel, Algorithm.BASELINE),
        (dataset, travel, Algorithm.OPTIMIZED),
    ]

    def routes(plan):
        return [
            (route.engineer_id, [stop.request_id for stop in route.stops])
            for route in plan.routes
        ]

    alone = parallel.run(service.build_plan, jobs)
    monkeypatch.setattr(settings, "solver_workers", 3)
    try:
        pooled = parallel.run(service.build_plan, jobs)
    finally:
        parallel.shutdown()

    assert [routes(plan) for plan in pooled] == [routes(plan) for plan in alone]
    assert all(plan.validation.ok for plan in pooled)
