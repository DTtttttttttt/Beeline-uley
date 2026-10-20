"""Аварии вперёд: поэтапное решение и ожидание аварий (PLAN 6.5, 6.10, блок 49).

Примеры маленькие и с очевидным ответом: этап 1 ставит аварию как можно раньше, этап 2
держит её срок. Ни Valhalla, ни Mongo тестам не нужны: таблицы — фикстура из плеч.
"""

import pytest

from app.config import settings
from app.dictionaries import DurationMode
from app.models import AddRequestEvent, Algorithm, SearchStats, Variant
from app.planning import candidates, optimizer, repair, replan, service, validator
from app.planning.schedule import capped, latest_start
from app.planning.variants import SETUPS, Setup
from app.routing.matrices import start_id
from app.timeutil import to_seconds

EMERGENCY = {
    "skill": "emergency",
    "workPriority": "emergency",
    "serviceDurationMin": 80,
    "windowStart": "09:00",
    "windowEnd": "18:00",
}


@pytest.fixture(autouse=True)
def _short(monkeypatch):
    """Пределы маленьких примеров: ответ очевиден, ждать полного поиска незачем."""
    monkeypatch.setattr(settings, "solver_solution_limit", 300)
    monkeypatch.setattr(settings, "replan_solution_limit", 200)


def starts(plan) -> dict[str, str]:
    return {
        stop.request_id: stop.start for route in plan.routes for stop in route.stops
    }


def owners(plan) -> dict[str, str]:
    return {
        stop.request_id: route.engineer_id
        for route in plan.routes
        for stop in route.stops
    }


@pytest.fixture
def emergency_last(make_request, make_engineer, make_travel, make_dataset):
    """Одна бригада: две обычные заявки по пути и авария в стороне.

    По километрам выгодно «r1, r2, авария» (3 км), а авария первой стоит 41 км. Окно аварии
    на весь день, поэтому без этапов её ставят в конец — дешевле на 38 км.
    """
    requests = [
        make_request("r1", windowEnd="17:00"),
        make_request("r2", windowEnd="17:00"),
        make_request("e", **EMERGENCY),
    ]
    engineer = make_engineer("brigade-1", skills=["local", "emergency"])
    near, far = (600, 1000), (600, 20000)
    travel = make_travel(
        requests,
        legs={
            ("office", "r1"): near,
            ("r1", "r2"): near,
            ("r2", "e"): near,
            ("office", "e"): far,
            ("e", "r1"): far,
            ("e", "r2"): far,
            ("r2", "r1"): far,
            ("r1", "e"): far,
            ("office", "r2"): far,
        },
    )
    return make_dataset(requests, [engineer]), travel


def test_staged_starts_the_emergency_first_without_losing_requests(
    emergency_last, monkeypatch
):
    data, travel = emergency_last

    plain = service.build_plan(data, travel, Algorithm.OPTIMIZED)
    monkeypatch.setattr(settings, "objective_staged", True)
    staged = service.build_plan(data, travel, Algorithm.OPTIMIZED)

    assert starts(plain)["e"] > "11:00"  # без этапов авария — последней
    assert starts(staged)["e"] == "09:10"
    assert staged.metrics.assigned_count == plain.metrics.assigned_count == 3
    assert staged.metrics.emergency_wait_min < plain.metrics.emergency_wait_min
    assert staged.validation.ok
    # Авария в модели этапа 1 одна: GLS дошёл бы до предела только по часам, спуск — сам.
    assert staged.search.stopped_by == "solutions"


def test_staged_plan_does_not_lose_to_a_shorter_baseline(emergency_last, monkeypatch):
    """Базовый вариант ТЗ здесь ставит аварию последней и короче на 38 км. По старшим
    критериям планы равны, и сравнение по одной цели вернуло бы базовый — то есть аварию
    в конец дня. При авариях вперёд ожидание стоит над пробегом (PLAN 6.6)."""
    data, travel = emergency_last
    monkeypatch.setattr(settings, "objective_staged", True)

    plan = service.build_plan(data, travel, Algorithm.OPTIMIZED)

    assert not plan.fallback_to_baseline
    assert starts(plan)["e"] == "09:10"


def test_required_request_is_kept_against_the_objective(
    make_request, make_engineer, make_travel, make_dataset
):
    """`count_first` взял бы две обычные заявки вместо аварии на весь день; обязательную
    аварию (этап 2) солвер не вправе оставить без исполнителя."""
    requests = [
        make_request("r1", windowEnd="17:00"),
        make_request(
            "e",
            skill="emergency",
            workPriority="emergency",
            serviceDurationMin=480,
            windowEnd="10:00",
        ),
        make_request("r2", windowEnd="17:00"),
    ]
    data = make_dataset(
        requests, [make_engineer("brigade-1", skills=["local", "emergency"])]
    )
    travel = make_travel(requests)
    weights = optimizer.make_weights(
        optimizer.ORDERS["count_first"], optimizer.bounds(data, travel)
    )

    solved = optimizer.solve(
        data,
        travel,
        candidates.allowed(data, travel),
        weights,
        2,
        solution_limit=200,
        required=frozenset({"e"}),
        # Авария занимает весь день: переставлять нечего, и GLS дошёл бы до предела только
        # по часам. Спуск кончается сам (см. `optimizer._search`).
        descent=True,
    )

    assert [request.id for request in solved.routes["brigade-1"]] == ["e"]


def test_stage_two_may_hand_the_emergency_to_another_brigade(
    make_request, make_engineer, make_travel, make_dataset, monkeypatch
):
    """Этап 1 отдаёт аварию бригаде, живущей рядом (меньше километров). Этап 2 бригаду не
    закрепляет: вторая бригада успевает к сроку и всё равно нужна для обычной заявки, а одна
    бригада вместо двух старше километров."""
    requests = [make_request("e", **EMERGENCY), make_request("r", windowEnd="17:00")]
    neighbour = make_engineer(
        "brigade-1", skills=["emergency"], startLat=55.75, startLon=37.80
    )
    universal = make_engineer("brigade-2", skills=["local", "emergency"])
    data = make_dataset(requests, [neighbour, universal])
    home = start_id(data.office, neighbour)
    travel = make_travel(
        requests,
        legs={(home, "e"): (600, 1000), ("office", "e"): (600, 5000)},
        engineers=[neighbour, universal],
    )
    monkeypatch.setattr(settings, "objective_staged", True)

    plan = service.build_plan(data, travel, Algorithm.OPTIMIZED)

    assert owners(plan) == {"e": "brigade-2", "r": "brigade-2"}
    assert starts(plan)["e"] == "09:10"
    assert plan.validation.ok


@pytest.mark.parametrize("rule", ["start_in_window", "fit_in_window"])
def test_capped_only_narrows_the_window(make_request, make_engineer, monkeypatch, rule):
    monkeypatch.setattr(settings, "window_rule", rule)
    request = make_request("e", **EMERGENCY)
    engineer = make_engineer()
    nine_thirty = to_seconds("09:30")

    def cap(latest):
        return capped(request, latest, [engineer], DurationMode.NORM)

    assert latest_start(cap(nine_thirty), engineer, DurationMode.NORM) == nine_thirty
    assert cap(nine_thirty).window_start == request.window_start
    # Срок позже окна окно не расширяет: копия не нужна вовсе.
    assert cap(to_seconds("23:00")) is request


def test_repair_keeps_the_emergency_deadline(
    make_request, make_engineer, make_travel, make_dataset
):
    """Обычная заявка встала бы только перед аварией. С исходным окном аварии досчёт её
    ставит, с ужатым (срок 09:25) — нет: авария началась бы в 10:20."""
    emergency = make_request("e", **EMERGENCY)
    early = make_request("r", windowEnd="09:30")
    engineer = make_engineer("brigade-1", skills=["local", "emergency"])
    data = make_dataset([emergency, early], [engineer])
    travel = make_travel([emergency, early])
    weights = optimizer.make_weights(
        optimizer.ORDERS["emergency_first"], optimizer.bounds(data, travel)
    )

    loose = {"brigade-1": [emergency]}
    assert repair.fill(data, loose, [early], travel, weights) == []

    tight_emergency = capped(
        emergency, to_seconds("09:25"), [engineer], DurationMode.NORM
    )
    tight = make_dataset([tight_emergency, early], [engineer])
    held = {"brigade-1": [tight_emergency]}
    assert repair.fill(tight, held, [early], travel, weights) == [early]
    assert held == {"brigade-1": [tight_emergency]}


def test_replanned_emergency_keeps_the_warm_start(
    make_request, make_engineer, make_travel, make_dataset, monkeypatch, spy_initial
):
    """Заявка из события в 13:00, а авария набора ещё впереди: этап 2 стартует от прошлого
    плана с аварией в срок, а не с нуля (блок 44), закреплённое не двигается, план валиден.

    Аварию из события пересчёт не ставит — её отдаёт правило «кто раньше прибудет» (блок 74),
    поэтому старт этапа проверяется на аварии набора."""
    requests = [make_request(str(number), windowEnd="17:00") for number in (1, 2, 3)]
    later = make_request("4", windowStart="14:00", windowEnd="17:00")
    emergency = make_request("9", **EMERGENCY | {"windowStart": "14:00"})
    arrived = make_request("5", windowEnd="17:00")
    engineers = [
        make_engineer("brigade-1", skills=["local", "emergency"]),
        make_engineer("brigade-2", skills=["local", "emergency"]),
    ]
    data = make_dataset([*requests, later, emergency], engineers)
    travel = make_travel([*requests, later, emergency, arrived])
    monkeypatch.setattr(settings, "objective_staged", True)
    parent = service.build_plan(data, travel, Algorithm.OPTIMIZED)
    spy_initial.clear()

    event = AddRequestEvent(type="add_request", time="13:00", request=arrived)
    state = replan.prepare(parent, event, replan.ReplanMode.FROM_EVENT, travel)
    plan = service.build_plan(data, travel, Algorithm.OPTIMIZED, state)

    assert all(spy_initial), "стартовое решение этапа не прочитано"
    assert plan.search.warm_started
    assert starts(plan)["9"] == starts(parent)["9"]
    committed = {
        stop.request_id: stop.start
        for route in parent.routes
        for stop in route.stops
        if to_seconds(stop.departure) <= to_seconds("13:00")
    }
    assert {key: starts(plan)[key] for key in committed} == committed
    assert plan.validation.ok


def test_time_stopped_stage_makes_the_whole_search_time_stopped():
    """Неповторяемый этап 1 делает неповторяемым весь план: кэш его не возьмёт (блок 45)."""
    first = SearchStats(
        objective=5,
        solutions=40,
        stopped_by="time",
        warm_started=False,
        sec=1.5,
        trace=[],
    )
    second = SearchStats(
        objective=9,
        solutions=300,
        stopped_by="solutions",
        warm_started=True,
        sec=2.0,
        trace=[(0.1, 9)],
    )

    joined = service._joined(first, second)

    assert joined.stopped_by == "time"
    assert (joined.solutions, joined.sec, joined.objective) == (340, 3.5, 9)
    assert joined.warm_started
    assert service._joined(None, second) is second


def test_staging_applies_only_to_the_guided_emergency_first(monkeypatch):
    monkeypatch.setattr(settings, "objective_staged", True)

    assert service._staged("emergency_first", None, True)
    assert not service._staged("count_first", None, True)
    # «Аварии сразу» считается поэтапно и при выключенной общей настройке.
    monkeypatch.setattr(settings, "objective_staged", False)
    assert service._staged("emergency_first", SETUPS[Variant.EMERGENCY_ASAP], True)
    assert not service._staged("emergency_first", SETUPS[Variant.EMERGENCY_FIRST], True)
    monkeypatch.setattr(settings, "objective_staged", True)
    assert not service._staged("emergency_first", SETUPS[Variant.FAST], False)
    # Вариант со своей настройкой сильнее общей.
    own = Setup(
        Variant.EMERGENCY_FIRST, Algorithm.OPTIMIZED, "emergency_first", 2, staged=False
    )
    assert not service._staged("emergency_first", own, True)


def test_cache_key_changes_with_staging(emergency_last, monkeypatch):
    data, travel = emergency_last
    plain = service._fingerprint(data, travel, None)
    monkeypatch.setattr(settings, "objective_staged", True)

    assert service._fingerprint(data, travel, None) != plain


def test_emergency_wait_counts_from_the_working_day(
    make_request, make_engineer, make_travel, make_dataset
):
    """Окно 00:01–23:59 — авария, начатая в 09:10, ждала 10 минут, а не с полуночи."""
    emergency = make_request(
        "e", **(EMERGENCY | {"windowStart": "00:01", "windowEnd": "23:59"})
    )
    data = make_dataset([emergency], [make_engineer(skills=["emergency"])])

    plan = service.build_plan(data, make_travel([emergency]))

    assert starts(plan)["e"] == "09:10"
    assert plan.metrics.emergency_wait_min == 10
    assert plan.validation.ok

    plan.metrics.emergency_wait_min = 0
    assert any(
        "emergencyWaitMin" in error
        for error in validator.validate(plan, make_travel([emergency])).errors
    )


def test_insert_on_a_staged_day_keeps_the_emergency_deadline(
    make_request, make_engineer, make_travel, make_dataset, monkeypatch
):
    """Обычная заявка днём встраивается (`insert`), но на дне аварий вперёд не отодвигает
    аварию за её срок. Заявка x помещается только перед аварией и сдвинула бы её с 11:00 на
    11:40. Без сроков досчёт так и делает, со сроками x остаётся без бригады. Заявка r —
    чтобы GLS родительского дня было что переставлять (см. `optimizer._search`)."""
    emergency = make_request("e", **(EMERGENCY | {"windowStart": "11:00"}))
    other = make_request("r", windowStart="15:00", windowEnd="17:00")
    data = make_dataset(
        [emergency, other], [make_engineer(skills=["local", "emergency"])]
    )
    late = make_request("x", windowStart="10:30", windowEnd="11:00")
    travel = make_travel([emergency, other, late])
    event = AddRequestEvent(type="add_request", time="08:30", request=late)

    monkeypatch.setattr(settings, "objective_staged", True)
    parent = service.build_plan(data, travel, Algorithm.OPTIMIZED)
    assert starts(parent)["e"] == "11:00"

    def inserted(staged: bool):
        monkeypatch.setattr(settings, "objective_staged", staged)
        state = replan.prepare(parent, event, replan.default_mode(event), travel)
        return service.build_plan(data, travel, Algorithm.OPTIMIZED, state)

    plain, staged = inserted(False), inserted(True)

    assert (starts(plain)["x"], starts(plain)["e"]) == ("10:30", "11:40")
    assert "x" not in starts(staged)
    assert starts(staged)["e"] == "11:00"
    assert staged.validation.ok


def test_failed_stage_two_falls_back_to_the_plain_search(emergency_last, monkeypatch):
    """Этап 2 не нашёл решения — день считается обычным поиском, а не базовым вариантом."""
    data, travel = emergency_last
    monkeypatch.setattr(settings, "objective_staged", True)
    original = optimizer.solve

    def solve(*args, **kwargs):
        return None if kwargs.get("required") else original(*args, **kwargs)

    monkeypatch.setattr(optimizer, "solve", solve)

    plan = service.build_plan(data, travel, Algorithm.OPTIMIZED)

    assert not plan.fallback_to_baseline
    assert plan.metrics.assigned_count == 3
    assert plan.search is not None
