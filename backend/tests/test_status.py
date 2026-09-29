"""Статусы заявок и фиксация факта (PLAN 6.19, блок 25).

День фикстуры `day` — тот же, что в `test_replan.py`: три заявки одной бригаде, переезд
10 минут, работа 60. Значит выезды 09:00, 10:10 и 11:20, окончания 10:10, 11:20 и 12:30 —
по этим часам и проверяются все четыре момента дня.
"""

import pytest

from app.dictionaries import Outcome, RequestStatus
from app.models import (
    Algorithm,
    CancelRequestEvent,
    CloseRequestEvent,
    Closure,
    DeferRequestEvent,
    EngineerUnavailableEvent,
    ReplanMode,
    UpdateEngineerEvent,
)
from app.planning import manual, replan, service, status, validator
from app.planning.schedule import Visit

WIDE = {"windowStart": "09:00", "windowEnd": "17:00"}


@pytest.fixture
def day(make_request, make_engineer, make_dataset, make_travel):
    """Набор, таблицы и посчитанный по нему базовый план."""

    def make(engineers: int = 1):
        requests = [make_request(str(number), **WIDE) for number in (1, 2, 3)]
        brigades = [
            make_engineer(f"brigade-{number}") for number in range(1, engineers + 1)
        ]
        dataset = make_dataset(requests, brigades)
        travel = make_travel(requests)
        return dataset, travel, service.build_plan(dataset, travel)

    return make


def replanned(dataset, travel, parent, event, mode=ReplanMode.FROM_EVENT):
    state = replan.prepare(parent, event, mode, travel)
    return service.build_plan(dataset, travel, parent.algorithm, state)


def closing(request_id="1", time="10:15", outcome="done", actual_end=None):
    return CloseRequestEvent(
        type="close_request",
        time=time,
        requestId=request_id,
        outcome=outcome,
        actualEnd=actual_end,
    )


def stop_of(plan, request_id):
    return next(
        (
            stop
            for route in plan.routes
            for stop in route.stops
            if stop.request_id == request_id
        ),
        None,
    )


# --- статус вычисляется по временам стопа (PLAN 6.19) ------------------------


def visit(departure: int, arrival: int, end: int) -> Visit:
    """Стоп во внутренних секундах: статусу важны только выезд, прибытие и окончание."""
    return Visit("1", departure, arrival, arrival, end, arrival - departure, 0)


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (None, RequestStatus.SENT),  # первичный план: день ещё не начался
        (9 * 3600 - 1, RequestStatus.SENT),
        (9 * 3600 + 5 * 60, RequestStatus.EN_ROUTE),
        (9 * 3600 + 30 * 60, RequestStatus.IN_PROGRESS),
        (10 * 3600 + 15 * 60, RequestStatus.DONE),
    ],
)
def test_status_at_each_moment_of_the_day(moment, expected):
    day = {"brigade-1": [visit(9 * 3600, 9 * 3600 + 10 * 60, 10 * 3600 + 10 * 60)]}
    assert status.compute(day, {}, {}, moment) == {"1": expected}


def test_deferred_and_closed_outrank_the_schedule():
    """Перенос старше расписания, а факт — старше и переноса: заявку уже закрыли."""
    day = {"brigade-1": [visit(9 * 3600, 9 * 3600 + 10 * 60, 10 * 3600 + 10 * 60)]}
    deferred = {"1": "09:30"}
    assert status.compute(day, {}, deferred, 12 * 3600) == {"1": RequestStatus.DEFERRED}

    closed = {"1": Closure(outcome="failed", time="09:40", engineerId="brigade-1")}
    assert status.compute(day, closed, deferred, 12 * 3600) == {
        "1": RequestStatus.FAILED
    }


def test_primary_plan_has_every_assigned_request_sent(day):
    _, _, plan = day()
    assert set(plan.statuses.values()) == {RequestStatus.SENT}
    assert len(plan.statuses) == 3


def test_event_moves_statuses_forward(day):
    dataset, travel, plan = day()
    later = replanned(
        dataset,
        travel,
        plan,
        DeferRequestEvent(type="defer_request", time="11:00", requestId="3"),
    )
    # В 11:00 первая заявка сделана, вторая выполняется, третью перенесли.
    assert later.statuses["1"] is RequestStatus.DONE
    assert later.statuses["2"] is RequestStatus.IN_PROGRESS
    assert later.statuses["3"] is RequestStatus.DEFERRED


# --- закрытие факта (PLAN 6.19, ответ 1) -------------------------------------


def test_closed_request_stays_committed_and_gets_metrics(day):
    dataset, travel, plan = day()
    later = replanned(dataset, travel, plan, closing("1", "10:15", "done", "10:05"))

    assert later.closed["1"].outcome is Outcome.DONE
    assert later.closed["1"].time == "10:05"
    assert later.closed["1"].engineer_id == "brigade-1"
    assert later.statuses["1"] is RequestStatus.DONE
    assert later.metrics.closed_by_outcome[Outcome.DONE] == 1
    assert stop_of(later, "1").committed
    assert later.validation.ok


def test_closed_request_is_not_replanned_even_in_full_mode(day):
    """Режим `full` считает день заново, но факт пересчёту не подлежит (PLAN 6.19)."""
    dataset, travel, plan = day(engineers=2)
    closed = replanned(dataset, travel, plan, closing("1", "10:15", "done"))
    assert closed.assignments["1"] == "brigade-1"

    later = replanned(
        dataset,
        travel,
        closed,
        EngineerUnavailableEvent(
            type="engineer_unavailable", time="11:00", engineerId="brigade-2"
        ),
        ReplanMode.FULL,
    )
    assert later.assignments["1"] == "brigade-1"
    assert stop_of(later, "1").committed
    assert later.closed["1"].outcome is Outcome.DONE
    assert later.validation.ok


def test_failed_outcome_defers_the_request_and_frees_the_brigade(day):
    dataset, travel, plan = day()
    later = replanned(dataset, travel, plan, closing("2", "11:00", "failed", "10:50"))

    assert stop_of(later, "2") is None  # из маршрута ушла
    assert later.deferred["2"] == "10:50"
    assert later.assignments["2"] is None
    assert later.statuses["2"] is RequestStatus.FAILED
    assert later.metrics.closed_by_outcome[Outcome.FAILED] == 1
    assert [item.request_id for item in later.unassigned] == ["2"]
    # Бригада свободна и берёт оставшуюся заявку — но не раньше момента события.
    assert stop_of(later, "3").departure == "11:00"
    assert later.validation.ok


def test_failed_request_is_inherited_by_the_chain(day):
    dataset, travel, plan = day()
    failed = replanned(dataset, travel, plan, closing("2", "11:00", "failed"))
    later = replanned(
        dataset,
        travel,
        failed,
        EngineerUnavailableEvent(
            type="engineer_unavailable", time="12:00", engineerId="brigade-1"
        ),
    )
    assert later.closed["2"].outcome is Outcome.FAILED
    assert later.deferred["2"] == "11:00"
    assert stop_of(later, "2") is None
    assert later.validation.ok


# --- отказы (PLAN 6.19) ------------------------------------------------------


def test_cannot_close_work_that_has_not_started(day):
    _, travel, plan = day()
    with pytest.raises(replan.ReplanError) as problem:
        replan.prepare(plan, closing("3", "09:30"), ReplanMode.FROM_EVENT, travel)
    assert problem.value.status == 409
    assert "ещё не начиналась" in str(problem.value)


def test_cannot_close_twice(day):
    dataset, travel, plan = day()
    closed = replanned(dataset, travel, plan, closing("1", "10:15", "done"))
    with pytest.raises(replan.ReplanError) as problem:
        replan.prepare(
            closed, closing("1", "11:00", "cancelled"), ReplanMode.FROM_EVENT, travel
        )
    assert problem.value.status == 409
    assert "уже закрыта" in str(problem.value)


def test_fact_cannot_be_later_than_the_event(day):
    _, travel, plan = day()
    with pytest.raises(replan.ReplanError) as problem:
        replan.prepare(
            plan, closing("1", "10:15", "done", "10:40"), ReplanMode.FROM_EVENT, travel
        )
    assert problem.value.status == 409
    assert "позже события" in str(problem.value)


def test_closed_request_cannot_be_reassigned_by_hand(day):
    dataset, travel, plan = day(engineers=2)
    closed = replanned(dataset, travel, plan, closing("1", "10:15", "done"))
    state = manual.day(closed, travel)
    with pytest.raises(replan.ReplanError) as problem:
        manual.move(state, travel, "1", "brigade-2", None)
    assert problem.value.status == 409
    assert "уже закрыта" in str(problem.value)


# --- независимая проверка (PLAN 6.7) -----------------------------------------


def test_validator_catches_a_closed_request_moved_to_another_brigade(day):
    dataset, travel, plan = day(engineers=2)
    closed = replanned(dataset, travel, plan, closing("1", "10:15", "done"))
    closed.closed["1"] = Closure(outcome="done", time="10:05", engineerId="brigade-2")
    result = validator.validate(closed, travel)
    assert not result.ok
    assert any("а стоит в маршруте brigade-1" in error for error in result.errors)


def test_validator_catches_a_failed_request_left_in_the_route(day):
    _, travel, plan = day()
    plan.closed["1"] = Closure(outcome="failed", time="09:30", engineerId="brigade-1")
    result = validator.validate(plan, travel)
    assert not result.ok
    assert any("закрыта как невыполненная" in error for error in result.errors)


def test_validator_catches_a_forged_committed_flag(day):
    """Закреплённый стоп в плане без события проходит только вместе с фактом (PLAN 6.12)."""
    _, travel, plan = day()
    plan.routes[0].stops[0].committed = True
    assert not validator.validate(plan, travel).ok

    plan.closed["1"] = Closure(outcome="done", time="10:05", engineerId="brigade-1")
    plan.metrics.closed_by_outcome[Outcome.DONE] = 1
    assert validator.validate(plan, travel).ok


# --- правки по ревью блока 25 -------------------------------------------------


def test_closing_a_middle_stop_in_full_mode_pins_only_the_fact(day):
    """В режиме `full` закрепляется закрытое, а не всё до него (ревью, замечание 1).

    Соседу закреплённого стопа в `full` опереться не на что: правила «выезд ≤ `T`» там нет,
    факта у него нет — и независимая проверка справедливо отвергла бы такое закрепление.
    """
    dataset, travel, plan = day()
    later = replanned(
        dataset, travel, plan, closing("2", "11:00", "done"), ReplanMode.FULL
    )
    assert later.validation.ok
    assert stop_of(later, "2").committed
    assert not stop_of(later, "1").committed


def test_engineer_edit_in_full_mode_keeps_the_closed_head(day):
    """Правка бригады не затирает точку продолжения закреплённого факта (замечание 2)."""
    dataset, travel, plan = day()
    closed = replanned(
        dataset, travel, plan, closing("1", "10:15", "done"), ReplanMode.FULL
    )
    engineer = closed.input.engineers[0]
    later = replanned(
        dataset,
        travel,
        closed,
        UpdateEngineerEvent(type="update_engineer", time="10:30", engineer=engineer),
        ReplanMode.FULL,
    )
    assert later.validation.ok
    assert stop_of(later, "1").committed


def test_manual_edit_over_a_full_mode_closure(day):
    """Ручная правка поверх `full`-плана с фактом (замечание 3)."""
    dataset, travel, plan = day(engineers=2)
    closed = replanned(
        dataset, travel, plan, closing("1", "10:15", "done"), ReplanMode.FULL
    )
    state = manual.day(closed, travel)
    edit = manual.move(state, travel, "3", "brigade-2", None)
    later = service.build_plan(dataset, travel, Algorithm.MANUAL, state, edit=edit)
    assert later.validation.ok
    assert later.assignments["3"] == "brigade-2"
    assert stop_of(later, "1").committed


def test_closed_request_cannot_be_cancelled_or_deferred(day):
    """Отмена и перенос не стирают записанный факт (замечания 4 и 6)."""
    dataset, travel, plan = day()
    failed = replanned(dataset, travel, plan, closing("2", "11:00", "failed"))
    for event in (
        CancelRequestEvent(type="cancel_request", time="11:30", requestId="2"),
        DeferRequestEvent(type="defer_request", time="11:30", requestId="2"),
    ):
        with pytest.raises(replan.ReplanError) as problem:
            replan.prepare(failed, event, ReplanMode.FROM_EVENT, travel)
        assert problem.value.status == 409
        assert "уже закрыта" in str(problem.value)


def test_fact_cannot_end_before_the_brigade_left(day):
    """Нижняя граница факта — выезд к заявке (замечание 5)."""
    _, travel, plan = day()
    with pytest.raises(replan.ReplanError) as problem:
        replan.prepare(
            plan,
            closing("2", "11:00", "done", "09:30"),
            ReplanMode.FROM_EVENT,
            travel,
        )
    assert problem.value.status == 409
    assert "раньше выезда" in str(problem.value)
