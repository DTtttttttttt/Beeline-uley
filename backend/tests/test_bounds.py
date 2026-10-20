"""Нижняя граница бригад и доля от выполнимого (PLAN 6.10, блок 50).

Граница — оценка снизу: на маленьких примерах её проверяют очевидным ответом, на демо-парах —
тем, что она не превышает число бригад настоящего плана. Факт не может быть меньше границы
ни при каком расписании; если может — оценка неверна.
"""

import importlib.util
from pathlib import Path

import pytest

from app.config import Settings, settings
from app.data import seed
from app.models import Algorithm
from app.planning import bounds, service

# Демо-пару на готовых таблицах переездов собирает скрипт замеров — без Valhalla и Mongo.
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "measure.py"
spec = importlib.util.spec_from_file_location("measure", SCRIPT)
measure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(measure)


def _ids(requests) -> set[str]:
    return {request.id for request in requests}


@pytest.mark.parametrize("rule", ["start_in_window", "fit_in_window"])
def test_four_long_works_in_one_two_hour_window_need_two_engineers(
    rule, monkeypatch, make_request, make_engineer, make_dataset, make_travel
):
    """По объёму хватило бы одной смены (4 × 80 мин < 9 ч), но окно одно на всех: начать
    четыре работы по 70 минут между 10:00 и 12:00 одна бригада не успевает."""
    monkeypatch.setattr(settings, "window_rule", rule)
    requests = [
        make_request(
            str(n), serviceDurationMin=70, windowStart="10:00", windowEnd="12:00"
        )
        for n in range(1, 5)
    ]
    engineers = [make_engineer(f"brigade-{n}") for n in range(1, 5)]
    data = make_dataset(requests, engineers)

    assert bounds.lower_bound(data, make_travel(requests), _ids(requests)) >= 2


def test_volume_alone_needs_two_shifts(
    make_request, make_engineer, make_dataset, make_travel
):
    """Окна на весь день, но работы с дорогой — 10 × 70 мин, а смена — 9 ч: одной мало."""
    requests = [
        make_request(str(n), windowStart="09:00", windowEnd="18:00")
        for n in range(1, 11)
    ]
    engineers = [make_engineer(f"brigade-{n}") for n in range(1, 4)]
    data = make_dataset(requests, engineers)

    assert bounds.lower_bound(data, make_travel(requests), _ids(requests)) == 2


def test_equipment_is_bounded_by_engineers_who_carry_it(
    make_request, make_engineer, make_dataset, make_travel
):
    """Группы берутся из того же правила допуска, что у расписания: роутер есть только у
    бригад с короткой сменой — значит, работы с роутером делят только они."""
    requests = [
        make_request(
            str(n), windowStart="09:00", windowEnd="18:00", requiredEquipment=["router"]
        )
        for n in range(1, 5)
    ]
    engineers = [
        make_engineer("brigade-1", equipment=["router"], shiftEnd="13:00"),
        make_engineer("brigade-2", equipment=["router"], shiftEnd="13:00"),
        make_engineer("brigade-3"),
    ]
    data = make_dataset(requests, engineers)

    assert bounds.lower_bound(data, make_travel(requests), _ids(requests)) == 2


def test_emergency_skill_is_bounded_by_its_own_engineers(
    make_request, make_engineer, make_dataset, make_travel
):
    """Четыре аварии по 60 мин: на всех хватило бы одной девятичасовой смены, но умеют их
    только бригады с четырёхчасовой сменой — нужно две."""
    emergency = {"skill": "emergency", "workPriority": "emergency"}
    requests = [
        make_request(str(n), windowStart="09:00", windowEnd="18:00", **emergency)
        for n in range(1, 5)
    ]
    engineers = [
        make_engineer("brigade-1", skills=["emergency"], shiftEnd="13:00"),
        make_engineer("brigade-2", skills=["emergency"], shiftEnd="13:00"),
        make_engineer("brigade-3", skills=["local"]),
    ]
    data = make_dataset(requests, engineers)

    assert bounds.lower_bound(data, make_travel(requests), _ids(requests)) == 2


def test_car_requests_are_bounded_by_car_engineers(
    make_request, make_engineer, make_dataset, make_travel
):
    """То же для заявок, которым нужен автомобиль: везут их только машины с короткой сменой."""
    requests = [
        make_request(
            str(n), windowStart="09:00", windowEnd="18:00", requiredTransport="car"
        )
        for n in range(1, 5)
    ]
    engineers = [
        make_engineer("brigade-1", shiftEnd="13:00"),
        make_engineer("brigade-2", shiftEnd="13:00"),
        make_engineer("brigade-3", transport="bicycle"),
    ]
    data = make_dataset(requests, engineers)

    assert bounds.lower_bound(data, make_travel(requests), _ids(requests)) == 2


def test_no_requests_need_no_engineers(
    make_request, make_engineer, make_dataset, make_travel
):
    requests = [make_request()]
    data = make_dataset(requests, [make_engineer()])

    assert bounds.lower_bound(data, make_travel(requests), set()) == 0


def test_ceiling_counts_only_requests_someone_can_do(
    make_request, make_engineer, make_dataset, make_travel
):
    """Заявка без исполнителя с навыком (`NO_SKILL`) в выполнимые не входит, а та, что
    просто не поместилась (`NOT_FITTED`), — входит."""
    requests = [
        make_request("1", windowStart="10:00", windowEnd="10:05"),
        make_request("2", windowStart="10:00", windowEnd="10:05"),
        make_request("3", skill="connection"),
    ]
    data = make_dataset(requests, [make_engineer()])
    plan = service.build_plan(data, make_travel(requests), Algorithm.BASELINE)

    assert plan.metrics.assigned_count == 1
    assert {item.reason_code for item in plan.unassigned} == {"NOT_FITTED", "NO_SKILL"}
    assert plan.metrics.requests_ceiling == 2
    assert plan.metrics.engineers_lower_bound == 1


# --- демо-пары -----------------------------------------------------------------------


PAIRS = [
    (requests_id, crew_id)
    for requests_id in seed.DEMO_REQUESTS
    for crew_id in seed.DEMO_CREWS
]


@pytest.mark.parametrize(("requests_id", "crew_id"), PAIRS)
def test_bound_never_exceeds_the_real_plan(requests_id, crew_id, monkeypatch):
    """Сторожевой тест: граница не больше бригад настоящего плана ни на одной демо-паре.

    Короткий предел поиска: план хуже, чем в README, но граница обязана держать любой
    допустимый план — а самые плотные планы девяти пар проверяет `measure.py` при каждом
    пересчёте README.
    """
    # Настройки по умолчанию, как у замера: правило окна или порядок критериев из `.env`
    # иначе проверяли бы здесь другой расчёт, чем в README.
    for name, field in Settings.model_fields.items():
        monkeypatch.setattr(
            settings, name, field.get_default(call_default_factory=True)
        )
    monkeypatch.setattr(settings, "solver_solution_limit", 50)
    dataset = measure._dataset(requests_id, crew_id)
    travel = measure._travel(dataset)
    for algorithm in (Algorithm.BASELINE, Algorithm.OPTIMIZED):
        plan = service.build_plan(dataset, travel, algorithm)
        figures = plan.metrics

        assert plan.validation.ok, plan.validation.errors
        assert 1 <= figures.engineers_lower_bound <= figures.engineers_used
        assert figures.assigned_count <= figures.requests_ceiling
        assert figures.requests_ceiling <= len(dataset.requests)
