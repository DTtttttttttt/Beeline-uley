"""Скорость инженера и факты дня на сервере (PLAN 9.1, блок 68).

Один день на двух заявках: смена 09:00–18:00, переезд 10 минут. Заявка «1» — 60 минут с 09:10
до 10:10, заявка «2» — 30 минут, по плану 10:20–10:50. Скрытая длительность в тестах задаётся
явно (`hidden`): какой она выйдет у генератора, тест не проверяет.
"""

from datetime import UTC, datetime

import pytest

from app import db
from app.models import (
    AddEngineerEvent,
    Algorithm,
    ClockClosure,
    ClockEvent,
    CloseRequestEvent,
    ReplanMode,
)
from app.planning import facts, profiles, replan, service, validator
from app.timeutil import to_seconds


@pytest.fixture
def day(make_request, make_engineer, make_dataset, make_travel):
    def build(second=None, more=(), engineers=None, legs=None):
        requests = [
            make_request("1", windowEnd="18:00"),
            make_request(
                "2", serviceDurationMin=30, **(second or {"windowEnd": "18:00"})
            ),
            *more,
        ]
        engineers = engineers or [make_engineer()]
        dataset = make_dataset(requests, engineers)
        travel = make_travel(requests, legs=legs)
        plan = service.build_plan(dataset, travel, Algorithm.BASELINE)
        assert plan.validation.ok, plan.validation.errors
        return dataset, travel, plan

    return build


def hidden(monkeypatch, minutes: dict[str, int]):
    """Скрытая длительность работы, минуты: по номеру заявки."""
    monkeypatch.setattr(
        facts, "actual", lambda request, engineer: minutes[request.id] * 60
    )


def ticked(dataset, travel, parent, time):
    event = facts.observe(parent, travel, to_seconds(time))
    if event is None:
        return None
    state = service.replan_state(parent, event, ReplanMode.DELAY, travel)
    plan = service.build_plan(dataset, travel, parent.algorithm, state)
    assert plan.validation.ok, plan.validation.errors
    return plan


def stop(plan, request_id):
    return next(s for r in plan.routes for s in r.stops if s.request_id == request_id)


def owner(plan, request_id):
    return plan.assignments.get(request_id)


def test_actual_duration_repeats_and_hides(make_request, make_engineer):
    request = make_request(object={"floor": 9})
    first, second = make_engineer("brigade-1"), make_engineer("brigade-2")

    assert facts.actual(request, first) == facts.actual(request, first)
    # Истинная скорость — оценка ± 5 %, своим потоком от id.
    assert abs(facts.true_speed(first) - first.speed) <= 50
    assert {facts.actual(request, first), facts.actual(request, second)} != set()


def test_early_finish_moves_the_next_request_earlier_but_not_before_its_window(
    day, monkeypatch
):
    dataset, travel, plan = day({"windowStart": "10:15", "windowEnd": "18:00"})
    assert stop(plan, "2").start == "10:20"
    hidden(monkeypatch, {"1": 40, "2": 30})

    later = ticked(dataset, travel, plan, "10:00")

    assert later.closed["1"].time == "09:50"
    assert later.closed["1"].outcome == "done"
    assert stop(later, "1").end == "09:50"
    # Остаток — от момента часов: выезд 10:00, прибытие 10:10, но окно — с 10:15.
    assert stop(later, "2").start == "10:15"
    assert later.event.type == "clock"
    assert later.replan_mode is ReplanMode.DELAY


def test_delay_without_conflict_makes_no_version(day, monkeypatch):
    _, travel, plan = day()
    hidden(monkeypatch, {"1": 180, "2": 30})

    assert facts.observe(plan, travel, to_seconds("11:00")) is None
    # Работа ещё не просрочена по плану — тоже ничего.
    assert facts.observe(plan, travel, to_seconds("10:00")) is None


def test_delay_with_conflict_hands_the_request_to_another_engineer(
    day, monkeypatch, make_engineer
):
    """Окно заявки «2» — до 11:00. «1» ещё идёт, а от неё до «2» 10 минут: с 10:51 не успеть —
    часы, спрошенные в 10:55, ставят событие на эту минуту (блок 69).
    Второй инженер выходит в 10:30 из офиса, откуда до «2» минута, — он успевает."""
    dataset, travel, plan = day(
        {"windowEnd": "11:00"},
        engineers=[
            make_engineer("brigade-1"),
            make_engineer("brigade-2", shiftStart="10:30"),
        ],
        legs={("office", "2"): (60, 500)},
    )
    assert owner(plan, "2") == "brigade-1"
    hidden(monkeypatch, {"1": 180, "2": 30})

    later = ticked(dataset, travel, plan, "10:55")

    assert later.running == {"1": "10:51"}
    assert owner(later, "2") == "brigade-2"
    # Затянувшаяся работа остаётся за своим инженером и закреплена.
    assert owner(later, "1") == "brigade-1"
    assert stop(later, "1").committed
    assert stop(later, "1").end >= "10:51"


def test_delay_with_conflict_reorders_the_same_engineer_first(
    day, monkeypatch, make_request
):
    """«2» широкая, «3» — до 12:00. В 11:30 «1» ещё идёт: «2» перед «3» уже не пускает
    «3» в окно, а «3» перед «2» — пускает, и обе остаются у того же инженера."""
    third = make_request("3", serviceDurationMin=30, windowEnd="12:00")
    dataset, travel, plan = day(more=[third])
    assert [s.request_id for s in plan.routes[0].stops] == ["1", "2", "3"]
    hidden(monkeypatch, {"1": 150, "2": 30, "3": 30})

    later = ticked(dataset, travel, plan, "11:30")

    assert [s.request_id for s in later.routes[0].stops] == ["1", "3", "2"]
    assert not later.unassigned


def test_delay_with_no_one_to_take_it_is_delayed(day, monkeypatch):
    dataset, travel, plan = day({"windowEnd": "11:00"})
    hidden(monkeypatch, {"1": 180, "2": 30})

    later = ticked(dataset, travel, plan, "10:55")

    (left,) = later.unassigned
    assert (left.request_id, left.reason_code) == ("2", "DELAYED")
    assert left.reason_text == "Не удалось выполнить в допустимое время после задержки"
    assert later.explanations["2"].text == left.reason_text
    # Следующий тик закрывает «1» — причина у «2» та же: день её так и не взял.
    after = ticked(dataset, travel, later, "12:15")
    assert after.closed["1"].time == "12:10"
    assert [(u.request_id, u.reason_code) for u in after.unassigned] == [
        ("2", "DELAYED")
    ]


def test_manual_close_cancels_the_automatic_one(day, monkeypatch):
    dataset, travel, plan = day()
    closing = CloseRequestEvent(
        type="close_request", time="10:05", request_id="1", outcome="done"
    )
    state = service.replan_state(plan, closing, ReplanMode.FROM_EVENT, travel)
    closed = service.build_plan(dataset, travel, Algorithm.BASELINE, state)
    hidden(monkeypatch, {"1": 30, "2": 300})

    event = facts.observe(closed, travel, to_seconds("10:30"))

    # «1» закрыл диспетчер, «2» ещё не кончилась и не просрочена — сказать часам нечего.
    assert event is None


def test_clock_event_only_with_the_delay_mode(day):
    _dataset, travel, plan = day()
    event = ClockEvent(
        type="clock",
        time="10:00",
        closed=[ClockClosure(request_id="1", actual_end="09:55")],
    )
    with pytest.raises(replan.ReplanError):
        service.replan_state(plan, event, ReplanMode.FROM_EVENT, travel)
    assert replan.default_mode(event) is ReplanMode.DELAY


def test_validator_catches_work_that_ends_before_its_fact(day, monkeypatch):
    dataset, travel, plan = day()
    hidden(monkeypatch, {"1": 40, "2": 30})
    later = ticked(dataset, travel, plan, "10:00")

    forged = later.model_copy(deep=True)
    stop(forged, "1").end = "10:10"  # плановый конец вместо факта

    errors = validator.validate(forged, travel).errors
    assert any("окончание 10:10, пересчёт даёт 09:50" in error for error in errors)


def test_validator_counts_a_running_work_past_its_plan(day, monkeypatch, make_engineer):
    dataset, travel, plan = day(
        {"windowEnd": "11:00"},
        engineers=[
            make_engineer("brigade-1"),
            make_engineer("brigade-2", shiftStart="10:30"),
        ],
    )
    hidden(monkeypatch, {"1": 180, "2": 30})
    later = ticked(dataset, travel, plan, "10:55")

    forged = later.model_copy(deep=True)
    forged.running = {}
    assert not validator.validate(forged, travel).ok


def test_planned_work_survives_the_fact(day, monkeypatch, make_engineer):
    """Конец закрытой и затянувшейся работы — факт, а расчётная работа стопа — по-прежнему план
    (блок 69): по ней карточка пишет «По плану» и «сверх плана»."""
    dataset, travel, plan = day()
    hidden(monkeypatch, {"1": 40, "2": 30})
    early = ticked(dataset, travel, plan, "10:00")
    assert (stop(early, "1").end, stop(early, "1").work_min) == ("09:50", 60)

    dataset, travel, plan = day(
        {"windowEnd": "11:00"},
        engineers=[
            make_engineer("brigade-1"),
            make_engineer("brigade-2", shiftStart="10:30"),
        ],
    )
    hidden(monkeypatch, {"1": 180, "2": 30})
    late = ticked(dataset, travel, plan, "10:55")
    assert (stop(late, "1").end, stop(late, "1").work_min) == ("10:51", 60)


def test_validator_catches_forged_planned_work(day, monkeypatch):
    dataset, travel, plan = day()
    hidden(monkeypatch, {"1": 40, "2": 30})
    later = ticked(dataset, travel, plan, "10:00")

    forged = later.model_copy(deep=True)
    stop(forged, "1").work_min = 40  # факт вместо плана

    errors = validator.validate(forged, travel).errors
    assert any("работа по плану, мин 40, пересчёт даёт 60" in error for error in errors)


def test_clock_that_jumps_over_the_day_goes_through_it_in_order(
    day, monkeypatch, make_request
):
    """Страницу открыли вечером (блок 69): часы спрошены сразу в 23:00, а не поминутно. События
    встают на свои моменты — факты и первые минуты конфликтов, — и день выходит тем же, что
    поминутно: те же исполнители и факты, каждая версия валидна."""
    third = make_request("3", serviceDurationMin=30, windowEnd="12:00")
    dataset, travel, plan = day(more=[third])
    hidden(monkeypatch, {"1": 150, "2": 20, "3": 45})

    def played(times):
        current, times_seen = plan, []
        for time in times:
            while (after := ticked(dataset, travel, current, time)) is not None:
                times_seen.append(after.event.time)
                current = after
        return current, times_seen

    stepped, _ = played(f"{h:02d}:{m:02d}" for h in range(9, 23) for m in range(60))
    jumped, moments = played(["23:00"])

    assert moments == sorted(moments) and moments[0] < "12:00"
    assert jumped.closed == stepped.closed
    assert jumped.assignments == stepped.assignments
    assert [s.request_id for s in jumped.routes[0].stops] == ["1", "3", "2"]


def test_two_runs_of_the_day_give_the_same_facts(day):
    _dataset, travel, plan = day()
    first = facts.observe(plan, travel, to_seconds("11:30"))
    second = facts.observe(plan, travel, to_seconds("11:30"))
    assert first == second


def test_speed_update_is_smooth():
    # Факт 90 мин при плане 60 у нормы: отношение 1,5 → 0,8 × 1,0 + 0,2 × 1,5 = 1,1.
    assert profiles.updated(1000, 60, 90) == 1100
    assert profiles.updated(1000, 60, 60) == 1000


def test_profile_learns_from_a_done_request_and_leaves_the_day_alone(
    mongo, day, monkeypatch
):
    dataset, travel, plan = day()
    hidden(monkeypatch, {"1": 90, "2": 30})
    later = ticked(dataset, travel, plan, "10:45")
    assert later.closed["1"].time == "10:40"

    profiles.learn(later, plan.closed)

    profile = db.profiles.find_one({"namespace": "test", "engineerId": "brigade-1"})
    # Работа по плану при норме 60 мин, по факту 90: скорость 1,1.
    assert profile["speed"] == 1100
    assert profile["history"][0]["factMin"] == 90
    # Длительности дня — по скорости на начало дня: в плане она прежняя.
    assert later.input.engineers[0].speed == 1000
    summary = service.day_summary(later)
    (engineer,) = summary.engineers
    assert (engineer.speed_start, engineer.speed_now) == (1000, 1100)
    assert [(item.plan_min, item.fact_min) for item in engineer.requests] == [(60, 90)]


def test_clock_endpoint_answers_204_without_news(mongo, day, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    dataset, travel, plan = day()
    mongo.datasets.insert_one(
        {"_id": dataset.id, **dataset.model_dump(by_alias=True, exclude={"id"})}
    )
    plan.created_at = datetime.now(UTC)
    mongo.plans.insert_one(service.to_document(plan))
    monkeypatch.setattr(service.matrices, "get", lambda _: travel)
    hidden(monkeypatch, {"1": 300, "2": 30})

    response = TestClient(app).post(
        f"/api/plans/{plan.id}/clock", json={"time": "09:30"}
    )

    assert response.status_code == 204


def test_clock_endpoint_goes_through_the_day_in_one_call(
    mongo, day, monkeypatch, make_request
):
    """Скачок на вечер (блок 82): один вызов отдаёт все версии по порядку — те же, что
    вызовы по одной, — и каждая сохранена потомком предыдущей. Повторный вызов — 204."""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.routing import valhalla

    third = make_request("3", serviceDurationMin=30, windowEnd="12:00")
    dataset, travel, plan = day(more=[third])
    mongo.datasets.insert_one(
        {"_id": dataset.id, **dataset.model_dump(by_alias=True, exclude={"id"})}
    )
    plan.created_at = datetime.now(UTC)
    mongo.plans.insert_one(service.to_document(plan))
    monkeypatch.setattr(service.matrices, "get", lambda _: travel)
    monkeypatch.setattr(valhalla, "route", lambda locations, costing: [list(locations)])
    hidden(monkeypatch, {"1": 150, "2": 20, "3": 45})
    stepped, current = [], plan
    while (after := ticked(dataset, travel, current, "23:00")) is not None:
        stepped.append(after)
        current = after
    client = TestClient(app)

    response = client.post(f"/api/plans/{plan.id}/clock", json={"time": "23:00"})

    assert response.status_code == 200
    versions = response.json()
    assert len(versions) == len(stepped) > 1
    assert [version["parentPlanId"] for version in versions] == [
        plan.id,
        *(version["id"] for version in versions[:-1]),
    ]
    assert [version["event"]["time"] for version in versions] == [
        version.event.time for version in stepped
    ]
    assert (
        versions[-1]["closed"]
        == stepped[-1].model_dump(by_alias=True, mode="json")["closed"]
    )
    for version in versions:
        assert service.get_plan(version["id"]) is not None
    again = client.post(
        f"/api/plans/{versions[-1]['id']}/clock", json={"time": "23:00"}
    )
    assert again.status_code == 204


def test_full_replan_after_a_delay_drops_the_running_fact(
    day, monkeypatch, make_engineer
):
    """«Весь день заново» возвращает затянувшуюся работу в расчёт — её «ещё идёт» к новому
    месту не относится, и версия проходит проверку."""
    dataset, travel, plan = day(
        {"windowEnd": "11:00"},
        engineers=[
            make_engineer("brigade-1"),
            make_engineer("brigade-2", shiftStart="10:30"),
        ],
        legs={("office", "2"): (60, 500)},
    )
    hidden(monkeypatch, {"1": 180, "2": 30})
    later = ticked(dataset, travel, plan, "10:55")
    assert later.running == {"1": "10:51"}

    gone = CloseRequestEvent(
        type="close_request", time="11:00", request_id="2", outcome="cancelled"
    )
    state = service.replan_state(later, gone, ReplanMode.FULL, travel)
    full = service.build_plan(dataset, travel, Algorithm.BASELINE, state)

    assert full.validation.ok, full.validation.errors
    assert full.running == {}


def test_profile_learns_a_request_once(mongo, day, monkeypatch):
    """Повторный прогон того же дня даёт те же факты — скорость не уползает от повторов."""
    dataset, travel, plan = day()
    hidden(monkeypatch, {"1": 90, "2": 30})
    later = ticked(dataset, travel, plan, "10:45")

    profiles.learn(later, plan.closed)
    profiles.learn(later, plan.closed)

    profile = db.profiles.find_one({"namespace": "test", "engineerId": "brigade-1"})
    assert profile["speed"] == 1100
    assert len(profile["history"]) == 1


def test_added_engineer_starts_from_the_norm(mongo, day, make_engineer):
    """Новый инженер начинает с чистого профиля (правки 82): номер `new-N` переиспользуется, и
    прежний обладатель номера не должен отдать ему свою скорость. Демо-человек из резерва —
    тот же человек, его профиль остаётся."""
    _, _, plan = day()
    for engineer_id in ("new-1", "brigade-20"):
        for space in ("test", "demo"):
            db.profiles.insert_one(
                {"namespace": space, "engineerId": engineer_id, "speed": 1200}
            )

    for engineer_id, dataset_id in (
        ("new-1", "test"),
        ("new-1", "builtin"),
        ("brigade-20", "builtin"),
    ):
        added = AddEngineerEvent(
            type="add_engineer", time="12:00", engineer=make_engineer(engineer_id)
        )
        profiles.welcome(
            plan.model_copy(update={"event": added, "dataset_id": dataset_id})
        )

    assert profiles.speeds("test") == {"brigade-20": 1200}
    assert profiles.speeds("builtin") == {"brigade-20": 1200}


def test_events_endpoint_refuses_a_clock_event(mongo, day):
    """Событие часов собирает сервер по скрытым фактам, клиент его не присылает."""
    from fastapi.testclient import TestClient

    from app.main import app

    dataset, _, plan = day()
    mongo.datasets.insert_one(
        {"_id": dataset.id, **dataset.model_dump(by_alias=True, exclude={"id"})}
    )
    mongo.plans.insert_one(service.to_document(plan))
    clock = {"type": "clock", "time": "10:00", "running": ["2"]}

    for path in ("events", "events/preview"):
        response = TestClient(app).post(
            f"/api/plans/{plan.id}/{path}", json={"event": clock}
        )
        assert response.status_code == 400
