"""Длительность по сложности объекта и скорости инженера (PLAN 9.1, блок 66).

Пример задания: подключение (работа 60 + документы 10), 9-й этаж без лифта (+20 %), закрытая
территория (+10 мин), инженер на 12 % медленнее — `60 × 1,2 × 1,12 + 10 + 10 = 100,64 → 101`.
"""

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.dictionaries import DurationMode
from app.main import app
from app.models import Algorithm
from app.planning import (
    candidates,
    duration,
    explain,
    optimizer,
    schedule,
    service,
    validator,
)
from app.routing.matrices import OFFICE
from app.timeutil import to_seconds

COMPLEXITY = DurationMode.COMPLEXITY
NORM = DurationMode.NORM
# Подключение по нормативу: работа на объекте 70 мин, из них 10 — документы.
CONNECTION = {
    "serviceDurationMin": 70,
    "baseNormMin": 90,
    "documentsMin": 10,
    "workPriority": "new_connection",
    "skill": "connection",
}


def complex_day(dataset):
    return dataset.model_copy(update={"duration_mode": COMPLEXITY})


def at(mode):
    return schedule.Position(
        OFFICE,
        None,
        equipment_used=0,
        duration_mode=mode,
        finished={},
        turns={},
        lunch_done=True,
        after_work=False,
    )


def test_task_example_takes_101_minutes(make_request, make_engineer):
    request = make_request(
        **CONNECTION, object={"floor": 9, "elevator": False, "access": "closed"}
    )
    engineer = make_engineer(speed=1120)

    assert duration.complexity(request) == 1200
    assert duration.minutes(request, engineer, COMPLEXITY) == 101
    # По нормативу ни сложность, ни скорость не применяются — как до блока 66.
    assert duration.minutes(request, engineer, NORM) == 70


def test_minutes_round_up(make_request, make_engineer):
    """30 × 1,05 = 31,5 → 32: округление вверх, а не к ближайшему."""
    request = make_request(serviceDurationMin=30, object={"flats": 40})
    assert duration.minutes(request, make_engineer(), COMPLEXITY) == 32
    # Ровное произведение не округляется: 60 × 1,05 = 63.
    exact = make_request(serviceDurationMin=60, object={"flats": 40})
    assert duration.minutes(exact, make_engineer(), COMPLEXITY) == 63


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"object": {"floor": 2}}, 1000),
        ({"object": {"floor": 3}}, 1100),
        ({"object": {"floor": 5}}, 1100),
        ({"object": {"floor": 6}}, 1200),
        ({"object": {"floor": 10}}, 1300),
        ({"object": {"floor": 12, "elevator": True}}, 1000),
        # Квартир в подъезде (блок 78): от 30 — +5 %, от 60 — +10 %.
        ({"object": {"flats": 29}}, 1000),
        ({"object": {"flats": 30}}, 1050),
        ({"object": {"flats": 60}}, 1100),
        ({"object": {"buildingType": "private"}}, 900),
        ({"object": {"buildingType": "office"}}, 1050),
        ({"gigabit": True}, 1100),
        ({"repeatVisit": True}, 1150),
        ({"cable": True}, 1200),
        ({"drilling": True}, 1100),
        ({"requiredEquipment": ["router"]}, 1000),
        ({"requiredEquipment": ["router", "alice"]}, 1050),
        ({"requiredEquipment": ["router", "alice", "set_top_box"]}, 1100),
    ],
)
def test_each_factor(make_request, changes, expected):
    assert duration.complexity(make_request(**changes)) == expected


@pytest.mark.parametrize(("access", "extra"), [("far_parking", 5), ("closed", 10)])
def test_access_adds_minutes_not_percent(make_request, make_engineer, access, extra):
    request = make_request(serviceDurationMin=60, object={"access": access})
    assert duration.minutes(request, make_engineer(), COMPLEXITY) == 60 + extra


def test_private_house_ignores_floor_and_flats(make_request):
    request = make_request(
        object={"buildingType": "private", "floor": 12, "flats": 300}
    )
    assert duration.complexity(request) == 900


def test_factors_add_up_and_clamp(make_request, monkeypatch):
    # 10-й этаж без лифта + офис + гигабит + повторный выезд: 1 + 0,3 + 0,05 + 0,1 + 0,15.
    many = make_request(
        object={"floor": 10, "buildingType": "office"},
        gigabit=True,
        repeatVisit=True,
    )
    assert duration.complexity(many) == 1600
    monkeypatch.setitem(duration.BUILDING, "office", 1000)
    assert duration.complexity(many) == 2000
    monkeypatch.setitem(duration.BUILDING, "private", -500)
    assert duration.complexity(make_request(object={"buildingType": "private"})) == 800


def test_breakdown_names_the_reasons(make_request):
    request = make_request(
        **CONNECTION, object={"floor": 9, "elevator": False, "access": "closed"}
    )
    parts = duration.breakdown(request)

    assert (parts.norm_min, parts.minutes, parts.work_min) == (70, 92, 60)
    reasons = explain.duration_reasons(request)
    assert [(item.minutes, item.text) for item in reasons] == [
        (12, "9-й этаж без лифта"),
        (10, "закрытая территория или пропуск"),
    ]
    assert all(item.hint for item in reasons)


def test_reason_minutes_add_up_to_the_duration(make_request):
    """Пример задания: 60 × 1,2 = 72 — +12 мин за этаж, скорость на 12 % медленнее — остаток
    до 101: +9 мин; подъезд +10. Сумма причин — длительность минус норматив."""
    request = make_request(
        **CONNECTION, object={"floor": 9, "elevator": False, "access": "closed"}
    )
    reasons = explain.duration_reasons(request, 1120)

    assert [(item.minutes, item.text) for item in reasons] == [
        (12, "9-й этаж без лифта"),
        (10, "закрытая территория или пропуск"),
        (9, "скорость инженера"),
    ]
    assert reasons[-1].hint == "обычно работает на 12 % медленнее нормы"
    assert sum(item.minutes for item in reasons) == 101 - 70


def test_rounding_goes_to_the_biggest_reason_without_speed(make_request):
    """30 × 1,05 = 31,5 → 32: полминуты округления — у единственной добавки, а не строкой."""
    request = make_request(serviceDurationMin=30, object={"flats": 40})
    (reason,) = explain.duration_reasons(request)

    assert (reason.minutes, reason.text) == (2, "40 квартир в подъезде")


def test_clamped_complexity_still_adds_up(make_request, monkeypatch):
    """Зажим ×2,0 срезает добавки — сумма причин всё равно равна итогу."""
    monkeypatch.setitem(duration.BUILDING, "office", 1500)
    request = make_request(
        serviceDurationMin=60, object={"buildingType": "office"}, gigabit=True
    )
    reasons = explain.duration_reasons(request)

    assert sum(item.minutes for item in reasons) == 120 - 60


def test_clamped_complexity_keeps_the_speed_sign(make_request, monkeypatch):
    """Срезанное зажимом делят добавки, а скорость — разница с нормой: у медленного
    инженера её строка не уходит в минус (+10 % от 120 мин — +12)."""
    monkeypatch.setitem(duration.BUILDING, "office", 1500)
    request = make_request(
        serviceDurationMin=60, object={"buildingType": "office"}, gigabit=True
    )
    reasons = explain.duration_reasons(request, 1100)

    assert [item.minutes for item in reasons] == [56, 4, 12]
    assert sum(item.minutes for item in reasons) == 132 - 60


def test_norm_mode_keeps_the_old_durations(make_request, make_engineer, make_travel):
    request = make_request(object={"floor": 9}, gigabit=True)
    engineer = make_engineer(speed=1500)

    (visit,) = schedule.build_schedule(
        engineer, [request], make_travel([request]), at(NORM)
    )

    assert visit.end - visit.start == 60 * 60


def test_same_request_ends_later_with_the_slower_engineer(
    make_request, make_engineer, make_travel
):
    request = make_request()
    travel = make_travel([request])

    (fast,) = schedule.build_schedule(
        make_engineer(speed=1000), [request], travel, at(COMPLEXITY)
    )
    (slow,) = schedule.build_schedule(
        make_engineer(speed=1500), [request], travel, at(COMPLEXITY)
    )

    assert fast.end == to_seconds("10:10")
    assert slow.end == to_seconds("10:40")


@pytest.mark.parametrize(
    ("rule", "slow_verdict"),
    [("start_in_window", None), ("fit_in_window", schedule.Violation.WINDOW_END)],
)
def test_window_rule_on_the_pair(
    make_request, make_engineer, make_travel, monkeypatch, rule, slow_verdict
):
    """Окно 09:00–10:30, начало 09:10. Быстрый кончает в 10:10, медленный (×1,5) — в 10:40:
    по правилу «начать в окне» успевают оба, по правилу «уложиться в окно» — только быстрый."""
    monkeypatch.setattr(settings, "window_rule", rule)
    request = make_request(windowEnd="10:30")
    travel = make_travel([request])

    fast = schedule.first_violation(
        make_engineer(speed=1000), [request], travel, at(COMPLEXITY)
    )
    slow = schedule.first_violation(
        make_engineer(speed=1500), [request], travel, at(COMPLEXITY)
    )

    assert fast is None
    assert slow is slow_verdict


def test_validator_catches_a_norm_plan_in_complexity_mode(
    make_request, make_engineer, make_dataset, make_travel
):
    request = make_request(object={"floor": 9})  # ×1,2: 60 → 72 мин
    engineer = make_engineer()
    travel = make_travel([request])
    dataset = make_dataset([request], [engineer])

    honest = service.build_plan(complex_day(dataset), travel, Algorithm.BASELINE)
    assert honest.validation.ok, honest.validation.errors
    assert honest.routes[0].stops[0].end == "10:22"
    assert honest.metrics.complexity_extra_min == 12

    plain = service.build_plan(dataset, travel, Algorithm.BASELINE)
    forged = plain.model_copy(
        update={"input": plain.input.model_copy(update={"duration_mode": COMPLEXITY})}
    )
    errors = validator.validate(forged, travel).errors

    assert any("окончание 10:10, пересчёт даёт 10:22" in error for error in errors)
    assert any("complexityExtraMin" in error for error in errors)


def test_validator_counts_objects_by_level(
    make_request, make_engineer, make_dataset, make_travel
):
    requests = [
        make_request("1"),
        make_request("2", object={"floor": 4}),
        make_request("3", object={"floor": 12}, gigabit=True, repeatVisit=True),
    ]
    travel = make_travel(requests)
    plan = service.build_plan(
        complex_day(make_dataset(requests, [make_engineer()])),
        travel,
        Algorithm.BASELINE,
    )

    assert plan.metrics.objects_by_level == {"simple": 1, "medium": 1, "complex": 1}
    forged = plan.model_copy(deep=True)
    forged.metrics.objects_by_level = {"simple": 3, "medium": 0, "complex": 0}
    assert not validator.validate(forged, travel).ok


def solve(data, travel, allowed=None):
    weights = optimizer.make_weights(
        optimizer.ORDERS["count_first"], optimizer.bounds(data, travel)
    )
    return optimizer.solve(
        data,
        travel,
        allowed or candidates.allowed(data, travel),
        weights,
        5,
        solution_limit=50,
    )


def test_optimizer_counts_work_at_the_engineer_speed(
    make_request, make_engineer, make_dataset, make_travel
):
    """Смена 09:00–11:00, две заявки по 50 мин. По нормативу встают обе (10+50+10+50 = 120),
    у инженера ×1,5 — одна: 10+75+10+75 = 170. Каждая по отдельности ему по силам."""
    requests = [
        make_request(str(number), serviceDurationMin=50, windowEnd="18:00")
        for number in (1, 2)
    ]
    slow = make_engineer(speed=1500, shiftEnd="11:00")
    travel = make_travel(requests)
    data = complex_day(make_dataset(requests, [slow]))

    solved = solve(data, travel)

    assert len(solved.routes["brigade-1"]) == 1
    assert len(solved.unassigned) == 1
    norm = solve(make_dataset(requests, [slow]), travel)
    assert len(norm.routes["brigade-1"]) == 2


def test_optimizer_gives_the_long_request_to_the_fast_engineer(
    make_request, make_engineer, make_dataset, make_travel
):
    """Смена 09:00–10:20. Длинную (60 мин) медленный ×1,5 не успевает — 90 мин, а короткую
    (40 мин, у него 60) успевает. Обе выполнены, только если длинная у быстрого."""
    long = make_request("long", serviceDurationMin=60, windowEnd="18:00")
    short = make_request("short", serviceDurationMin=40, windowEnd="18:00")
    slow = make_engineer("brigade-1", speed=1500, shiftEnd="10:20")
    fast = make_engineer("brigade-2", speed=1000, shiftEnd="10:20")
    travel = make_travel([long, short])
    data = complex_day(make_dataset([long, short], [slow, fast]))

    solved = solve(data, travel)

    assert [request.id for request in solved.routes["brigade-2"]] == ["long"]
    assert [request.id for request in solved.routes["brigade-1"]] == ["short"]


def test_optimizer_keeps_the_window_of_each_engineer(
    make_request, make_engineer, make_dataset, make_travel, monkeypatch
):
    """`fit_in_window`: крайнее начало у медленного раньше. Кандидатом его назначили силой —
    модель обязана сама не дать ему заявку, а не полагаться на отбор кандидатов."""
    monkeypatch.setattr(settings, "window_rule", "fit_in_window")
    request = make_request(windowEnd="10:30")
    slow = make_engineer("brigade-1", speed=1500)
    fast = make_engineer("brigade-2", speed=1000)
    travel = make_travel([request])
    data = complex_day(make_dataset([request], [slow, fast]))

    solved = solve(data, travel, {request.id: ["brigade-1", "brigade-2"]})

    assert solved.routes == {"brigade-1": [], "brigade-2": [request]}


def test_duration_endpoint_answers_with_the_breakdown():
    response = TestClient(app).post(
        "/api/requests/duration",
        json={
            "serviceDurationMin": 70,
            "documentsMin": 10,
            "object": {"floor": 9, "access": "closed"},
            "speed": 1120,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert (body["normMin"], body["minutes"], body["complexity"]) == (70, 101, 1.2)
    assert body["level"] == "medium"
    assert body["reasons"][0] == {
        "minutes": 12,
        "text": "9-й этаж без лифта",
        "hint": "подъём без лифта и перенос оборудования по лестнице",
    }
    assert sum(item["minutes"] for item in body["reasons"]) == 101 - 70


def test_capped_follows_the_fastest_engineer_who_can_take_it(
    make_request, make_engineer, monkeypatch
):
    """`fit_in_window`: срок аварии 09:30 выражен концом окна по самой быстрой бригаде из тех,
    кто заявку возьмёт. Быстрая без навыка срок не задаёт — иначе подходящей медленной он
    стал бы строже 09:30."""
    monkeypatch.setattr(settings, "window_rule", "fit_in_window")
    request = make_request(windowEnd="18:00")
    unskilled = make_engineer("brigade-1", speed=800, skills=["connection"])
    slow = make_engineer("brigade-2", speed=1500)

    tight = schedule.capped(request, to_seconds("09:30"), [unskilled, slow], COMPLEXITY)

    assert schedule.latest_start(tight, slow, COMPLEXITY) == to_seconds("09:30")


def test_plan_carries_complexity_and_the_norm_risk(
    make_request, make_engineer, make_dataset, make_travel
):
    """Бейджу — сложность каждой заявки входа, подсказке риска — «было» по нормативу (блок 67)."""
    requests = [make_request("1"), make_request("2", object={"floor": 12})]
    travel = make_travel(requests)
    dataset = make_dataset(requests, [make_engineer()])

    complex_plan = service.build_plan(complex_day(dataset), travel, Algorithm.BASELINE)
    plain = service.build_plan(dataset, travel, Algorithm.BASELINE)

    assert complex_plan.complexity["2"].complexity == 1.3
    assert complex_plan.complexity["2"].level == "medium"
    assert set(complex_plan.norm_forecast) == set(complex_plan.forecast.requests)
    assert plain.norm_forecast == {}
    assert set(plain.complexity) == {"1", "2"}  # сложность объекта от режима не зависит


def test_validator_catches_a_forged_complexity(
    make_request, make_engineer, make_dataset, make_travel
):
    request = make_request(object={"floor": 12})
    travel = make_travel([request])
    plan = service.build_plan(
        make_dataset([request], [make_engineer()]), travel, Algorithm.BASELINE
    )
    forged = plan.model_copy(deep=True)
    forged.complexity["1"].level = "simple"

    assert any(
        "сложность" in error for error in validator.validate(forged, travel).errors
    )


def test_every_candidate_carries_its_own_duration(
    make_request, make_engineer, make_dataset, make_travel
):
    """«Кандидаты»: сколько заявка займёт у каждого (блок 69) — по скорости на начало дня."""
    request = make_request(**CONNECTION)
    engineers = [
        make_engineer("brigade-1", skills=["connection"]),
        make_engineer("brigade-2", skills=["connection"], speed=1500),
    ]
    travel = make_travel([request])
    dataset = complex_day(make_dataset([request], engineers))

    plan = service.build_plan(dataset, travel, Algorithm.BASELINE)

    durations = {
        item.engineer_id: item.duration_min
        for item in plan.explanations[request.id].candidates
    }
    assert durations == {"brigade-1": 70, "brigade-2": 100}  # 60 × 1,5 + 10
    norm = service.build_plan(
        dataset.model_copy(update={"duration_mode": NORM}), travel, Algorithm.BASELINE
    )
    assert {c.duration_min for c in norm.explanations[request.id].candidates} == {70}


def test_validator_counts_the_new_conditions_and_flats_like_the_formula(make_request):
    """Своя арифметика валидатора (блок 78) знает прокладку кабеля, сверление и квартиры в
    подъезде — иначе план с ними не прошёл бы проверку."""
    request = make_request(
        object={"floor": 4, "flats": 64}, cable=True, drilling=True, gigabit=True
    )

    assert validator._complexity(request) == duration.complexity(request) == 1600
