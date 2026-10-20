"""Прогноз опозданий (PLAN 6.18, блок 21).

Значения по умолчанию (conftest): смена 09:00–18:00, окно 09:00–12:00, работа 60 минут,
переезд 10 минут и 5 км. Отклонения задаются настройками, поэтому день без отклонений —
это множители `(1.0, 1.0, 1.0)`, а не отдельный режим в коде.
"""

import pytest
from pydantic import ValidationError

from app.config import Settings, settings
from app.models import CancelRequestEvent, ReplanMode, Risk
from app.planning import forecast, replan, service
from app.planning.schedule import start_positions

STEADY = (1.0, 1.0, 1.0)
WIDE = {"windowStart": "09:00", "windowEnd": "17:00"}


@pytest.fixture
def steady(monkeypatch):
    """День без отклонений: прогноз обязан повторить план и никого не обвинить."""
    monkeypatch.setattr(settings, "forecast_work_factor", STEADY)
    monkeypatch.setattr(settings, "forecast_travel_factor", STEADY)


@pytest.fixture
def build(make_request, make_engineer, make_dataset, make_travel):
    """План по перечисленным правкам заявок; бригад по умолчанию одна."""

    def make(*changes: dict, engineers: int = 1):
        requests = [
            make_request(str(number), **change)
            for number, change in enumerate(changes, start=1)
        ]
        brigades = [
            make_engineer(f"brigade-{number}") for number in range(1, engineers + 1)
        ]
        dataset = make_dataset(requests, brigades)
        return service.build_plan(dataset, make_travel(requests))

    return make


# --- отклонений нет (PLAN 6.18) ----------------------------------------------


def test_steady_day_has_no_late_requests(steady, build):
    """Множители 1.0 — расписание прогона совпадает с планом, опаздывать нечему."""
    plan = build({})
    item = plan.forecast.requests["1"]
    assert item.late_probability == 0
    assert item.risk == Risk.LOW
    assert item.p90_start == plan.routes[0].stops[0].start
    assert plan.metrics.expected_late == 0


def test_steady_day_never_runs_over_the_shift(steady, build):
    plan = build({})
    assert plan.forecast.engineers["brigade-1"].over_shift_probability == 0


# --- риск по заявке ----------------------------------------------------------


def test_zero_spare_gives_high_risk(build):
    """Начало ровно в конце окна: переезд чуть длиннее норматива — и это уже опоздание."""
    plan = build({"windowEnd": "09:10"})
    item = plan.forecast.requests["1"]
    assert item.late_probability > forecast.HIGH_FROM
    assert item.risk == Risk.HIGH


def test_wide_spare_gives_low_risk(build):
    """Запас в два часа никакое отклонение не съедает."""
    item = build(WIDE).forecast.requests["1"]
    assert item.late_probability == 0
    assert item.risk == Risk.LOW


@pytest.mark.parametrize(
    ("rule", "late"), [("start_in_window", False), ("fit_in_window", True)]
)
def test_window_rule_decides_what_counts_as_late(rule, late, monkeypatch, build):
    """Один и тот же день: по ТЗ опоздания нет, по Дополнениям — есть (PLAN 6.2, 6.18).

    Работа начинается в 11:00 и по нормативу заканчивается ровно в 12:00. Растянувшись,
    она вылезает за конец окна, не сдвинув начала: при `start_in_window` это никого не
    волнует, при `fit_in_window` — опоздание.
    """
    monkeypatch.setattr(settings, "window_rule", rule)
    probability = (
        build({"windowStart": "11:00"}).forecast.requests["1"].late_probability
    )
    assert (probability > forecast.HIGH_FROM) is late
    assert (probability == 0) is not late


def test_p90_start_is_not_earlier_than_the_planned_one(build):
    plan = build({})
    assert plan.forecast.requests["1"].p90_start >= plan.routes[0].stops[0].start


# --- риск по бригаде ---------------------------------------------------------


def test_work_until_the_end_of_the_shift_risks_running_over(build):
    """Окончание ровно в конце смены: растянувшаяся работа выходит за неё."""
    plan = build({"windowStart": "17:00", "windowEnd": "17:01"})
    assert plan.forecast.engineers["brigade-1"].over_shift_probability > 0.3


def test_idle_engineer_is_not_in_the_forecast(build):
    """У бригады без работы рисковать нечем — строки о ней нет вовсе."""
    plan = build({}, engineers=2)
    assert "brigade-2" not in plan.forecast.engineers


# --- детерминизм -------------------------------------------------------------


def test_same_seed_gives_the_same_forecast(
    monkeypatch, make_request, make_engineer, make_dataset, make_travel
):
    """Два расчёта одного плана обязаны совпасть, разные seed — разойтись.

    Окна узкие нарочно: на заявке с запасом в два часа любой seed даёт одни и те же нули,
    и тест подтверждал бы детерминизм, ничего о seed не говоря.
    """
    requests = [
        make_request("1", windowEnd="09:10"),
        make_request("2", windowStart="11:00", windowEnd="11:10"),
    ]
    dataset = make_dataset(requests, [make_engineer("brigade-1")])
    travel = make_travel(requests)
    routes = {"brigade-1": requests}
    positions = start_positions(dataset)

    first = forecast.compute(dataset, travel, routes, positions)
    assert forecast.compute(dataset, travel, routes, positions) == first

    monkeypatch.setattr(settings, "forecast_seed", settings.forecast_seed + 1)
    assert forecast.compute(dataset, travel, routes, positions) != first


# --- настройки (PLAN 6.18) ---------------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [
        # Порядок `random.triangular` — `(низ, верх, мода)`, и перепутанная тройка не падает,
        # а молча считает по другому распределению: такую настройку отвергаем на входе.
        {"forecast_work_factor": (0.8, 1.6, 1.0)},
        {"forecast_travel_factor": (1.5, 1.0, 0.9)},
        {"forecast_work_factor": (0, 1.0, 1.6)},
        # Ноль прогонов — не «прогноз выключен», а деление на ноль в каждом расчёте плана.
        {"forecast_runs": 0},
    ],
)
def test_broken_settings_are_refused(changes):
    with pytest.raises(ValidationError):
        Settings(**changes)


# --- место прогноза в плане --------------------------------------------------


def test_expected_late_is_the_sum_of_probabilities(build):
    plan = build({"windowEnd": "09:10"}, {"windowStart": "11:00", "windowEnd": "11:10"})
    assert plan.metrics.expected_late > 0
    assert plan.metrics.expected_late == round(
        sum(item.late_probability for item in plan.forecast.requests.values()), 2
    )


def test_explanation_tells_the_risk_and_the_spare(build):
    """PLAN 6.18: «Риск опоздания 35 %: запас до конца окна 8 мин»."""
    plan = build({"windowEnd": "09:40"})
    text = plan.explanations["1"].text
    probability = round(plan.forecast.requests["1"].late_probability * 100)
    assert f"Риск опоздания {probability} %: запас до конца окна 30 мин." in text


def test_unassigned_request_has_no_risk_line(build):
    """Опаздывать нечему: у заявки нет ни исполнителя, ни времени начала."""
    plan = build({"skill": "emergency"})
    assert plan.assignments["1"] is None
    assert "Риск опоздания" not in plan.explanations["1"].text
    assert plan.forecast.requests == {}


def test_committed_stops_stay_out_of_the_forecast(
    make_request, make_engineer, make_dataset, make_travel
):
    """Закреплённая работа уже идёт или к ней выехали — отклонять там нечего (PLAN 6.12).

    Выезды дня: 09:00, 10:10, 11:20 и 12:30, поэтому событие в 10:15 закрепляет первые две
    заявки. Отмена четвёртой оставляет на пересчёт ровно одну — третью.
    """
    requests = [make_request(str(number), **WIDE) for number in (1, 2, 3, 4)]
    dataset = make_dataset(requests, [make_engineer("brigade-1")])
    travel = make_travel(requests)
    parent = service.build_plan(dataset, travel)
    assert [stop.request_id for stop in parent.routes[0].stops] == ["1", "2", "3", "4"]

    event = CancelRequestEvent(type="cancel_request", time="10:15", requestId="4")
    state = replan.prepare(parent, event, ReplanMode.FROM_EVENT, travel)
    plan = service.build_plan(dataset, travel, parent.algorithm, state)

    assert [stop.committed for stop in plan.routes[0].stops] == [True, True, False]
    assert set(plan.forecast.requests) == {"3"}
