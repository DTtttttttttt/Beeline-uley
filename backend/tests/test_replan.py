"""Перепланирование после события (PLAN 6.12, блок 11).

Valhalla и 2ГИС не вызываются: таблицы переездов собираются фикстурами, линия — подменой
`valhalla.route`. Mongo нужна только тесту про новую версию в базе.

Базовый день фикстуры `day`: три заявки одной бригаде, выезды 09:00, 10:10 и 11:20
(переезд 10 минут, работа 60). Поэтому событие в 10:15 закрепляет первые две заявки —
ко второй бригада уже выехала, — а третью оставляет на пересчёт.
"""

import pytest

from app import db
from app.config import settings
from app.dictionaries import Outcome
from app.models import (
    AddEngineerEvent,
    AddRequestEvent,
    Algorithm,
    CancelRequestEvent,
    Change,
    CloseRequestEvent,
    DeferRequestEvent,
    EngineerUnavailableEvent,
    ReplanMode,
    UpdateEngineerEvent,
    UpdateRequestEvent,
)
from app.planning import replan, service, validator
from app.routing import geometry, matrices, valhalla
from app.routing.matrices import OFFICE, Travel

# Окно на весь день: блок 11 проверяет фиксацию и точки продолжения, а не границы окна —
# их разбирает test_schedule.py в обоих режимах WINDOW_RULE.
WIDE = {"windowStart": "09:00", "windowEnd": "17:00"}


@pytest.fixture
def day(make_request, make_engineer, make_dataset, make_travel):
    """Набор, таблицы и посчитанный по нему базовый план."""

    def make(engineers: int = 1, extra=()):
        requests = [make_request(str(number), **WIDE) for number in (1, 2, 3)]
        brigades = [
            make_engineer(f"brigade-{number}") for number in range(1, engineers + 1)
        ]
        dataset = make_dataset(requests, brigades)
        travel = make_travel([*requests, *extra])
        return dataset, travel, service.build_plan(dataset, travel)

    return make


def replanned(
    dataset, travel, parent, event, mode=ReplanMode.FROM_EVENT, algorithm=None
):
    state = replan.prepare(parent, event, mode, travel)
    return service.build_plan(dataset, travel, algorithm or parent.algorithm, state)


def routes_by_engineer(plan):
    return {route.engineer_id: route for route in plan.routes}


def ids(route):
    return [stop.request_id for stop in route.stops]


# --- фиксация (PLAN 6.12, шаг 1) ---------------------------------------------


def test_committed_stops_do_not_move(day):
    """Что началось до события, остаётся как есть — вплоть до времён."""
    dataset, travel, parent = day()
    plan = replanned(
        dataset,
        travel,
        parent,
        CancelRequestEvent(type="cancel_request", time="10:30", request_id="3"),
    )

    route = plan.routes[0]
    assert ids(route) == ["1", "2"]
    assert all(stop.committed for stop in route.stops)
    was = {stop.request_id: stop for stop in parent.routes[0].stops}
    assert [(stop.departure, stop.start, stop.end) for stop in route.stops] == [
        (was[request_id].departure, was[request_id].start, was[request_id].end)
        for request_id in ("1", "2")
    ]
    assert plan.validation.ok


def test_request_the_engineer_already_left_for_stays_with_it(day):
    """Выезд 10:10, прибытие 10:20: в 10:15 бригада уже в пути, заявку она не теряет."""
    dataset, travel, parent = day(engineers=2)
    plan = replanned(
        dataset,
        travel,
        parent,
        EngineerUnavailableEvent(
            type="engineer_unavailable", time="10:15", engineer_id="brigade-1"
        ),
    )

    routes = routes_by_engineer(plan)
    assert ids(routes["brigade-1"]) == ["1", "2"]
    assert all(stop.committed for stop in routes["brigade-1"].stops)
    # Незакреплённая заявка недоступной бригады ушла в общий список и нашла исполнителя.
    assert ids(routes["brigade-2"]) == ["3"]
    assert plan.validation.ok


def test_unavailable_engineer_gets_no_new_stops(day):
    """Недоступная бригада освобождается только к концу смены — новых стопов у неё нет."""
    dataset, travel, parent = day(engineers=2)
    plan = replanned(
        dataset,
        travel,
        parent,
        EngineerUnavailableEvent(
            type="engineer_unavailable", time="10:15", engineer_id="brigade-1"
        ),
        mode=ReplanMode.FULL,
    )

    routes = routes_by_engineer(plan)
    assert ids(routes["brigade-1"]) == []
    assert ids(routes["brigade-2"]) == ["1", "2", "3"]
    assert plan.validation.ok


def test_full_mode_commits_nothing(day):
    """Режим full считает день заново: закреплять нечего, утро может перестроиться."""
    dataset, travel, parent = day()
    plan = replanned(
        dataset,
        travel,
        parent,
        CancelRequestEvent(type="cancel_request", time="10:30", request_id="3"),
        mode=ReplanMode.FULL,
    )

    assert not any(stop.committed for route in plan.routes for stop in route.stops)
    assert plan.replan_mode is ReplanMode.FULL
    assert plan.validation.ok


# --- события (PLAN 6.12, шаг 3) ----------------------------------------------


@pytest.fixture
def idle(make_request, make_engineer, make_dataset, make_travel):
    """Бригада без заявок: единственная заявка набора ей не по навыку.

    Ровно тот случай, когда бригада простаивает до события, — на нём и проверяется, что
    выезд не назначается раньше `T`, а валидатор это знает (PLAN 11.5).
    """
    blocked = make_request("1", skill="emergency", **WIDE)
    urgent = make_request("9", **WIDE)
    dataset = make_dataset([blocked], [make_engineer("brigade-1")])
    travel = make_travel([blocked, urgent])
    parent = service.build_plan(dataset, travel)
    assert parent.routes[0].stops == []
    return dataset, travel, parent, urgent


def test_urgent_request_does_not_start_before_the_event(idle):
    dataset, travel, parent, urgent = idle
    plan = replanned(
        dataset,
        travel,
        parent,
        # Флаг явно: у обычной заявки без него срочности нет (PLAN 6.12, блок 30).
        AddRequestEvent(type="add_request", time="13:00", request=urgent, urgent=True),
    )

    stop = plan.routes[0].stops[0]
    assert (stop.departure, stop.arrival, stop.start) == ("13:00", "13:10", "13:10")
    added = {request.id: request for request in plan.input.requests}["9"]
    assert added.urgent
    assert added.window_start == "13:00"  # начало окна прижато к моменту события
    assert plan.validation.ok


def test_urgent_request_lives_only_in_the_plan(idle):
    """Набор данных событием не меняется: план — новая версия, вход остаётся входом."""
    dataset, travel, parent, urgent = idle
    plan = replanned(
        dataset,
        travel,
        parent,
        AddRequestEvent(type="add_request", time="13:00", request=urgent),
    )

    assert {request.id for request in plan.input.requests} == {"1", "9"}
    assert {request.id for request in dataset.requests} == {"1"}


def test_validator_rejects_a_departure_before_the_event(idle):
    """Подделка «бригада выехала с утра» обязана всплыть: события в 09:00 ещё не было."""
    dataset, travel, parent, urgent = idle
    plan = replanned(
        dataset,
        travel,
        parent,
        AddRequestEvent(type="add_request", time="13:00", request=urgent),
    )

    forged = plan.model_copy(deep=True)
    stop = forged.routes[0].stops[0]
    # Ровно то, что дал бы пересчёт от начала смены, без оглядки на момент события.
    stop.departure, stop.arrival, stop.start, stop.end = (
        "09:00",
        "09:10",
        "13:00",
        "14:00",
    )

    result = validator.validate(forged, travel)
    assert not result.ok
    assert any("выезд" in error for error in result.errors)


def test_cancelling_a_committed_request_is_rejected(day):
    _, travel, parent = day()
    with pytest.raises(replan.ReplanError) as error:
        replan.prepare(
            parent,
            CancelRequestEvent(type="cancel_request", time="10:30", request_id="1"),
            ReplanMode.FROM_EVENT,
            travel,
        )
    assert error.value.status == 409


def test_cancelled_request_leaves_the_day(day):
    dataset, travel, parent = day()
    plan = replanned(
        dataset,
        travel,
        parent,
        CancelRequestEvent(type="cancel_request", time="10:30", request_id="3"),
    )

    assert "3" not in {request.id for request in plan.input.requests}
    assert "3" not in plan.assignments
    assert any(
        change.request_id == "3" and change.change is Change.CANCELLED
        for change in plan.diff.requests
    )


def test_impossible_events_are_rejected(day, make_request):
    """Событие, которого нельзя применить, — ошибка входа, а не тихо посчитанный план."""
    _, travel, parent = day()
    events = [
        CancelRequestEvent(type="cancel_request", time="10:30", request_id="нет такой"),
        EngineerUnavailableEvent(
            type="engineer_unavailable", time="10:30", engineer_id="нет такой"
        ),
        # Идентификатор уже занят: иначе набор получил бы две разные заявки под одним id.
        AddRequestEvent(
            type="add_request", time="10:30", request=make_request("1", **WIDE)
        ),
        # Окно закончилось раньше события — начать заявку уже негде.
        AddRequestEvent(
            type="add_request",
            time="13:00",
            request=make_request("9", windowStart="09:00", windowEnd="12:00"),
        ),
    ]
    for event in events:
        with pytest.raises(replan.ReplanError) as error:
            replan.prepare(parent, event, ReplanMode.FROM_EVENT, travel)
        assert error.value.status == 400, event.type


def test_event_cannot_go_back_in_time(day):
    """Время назад «разморозило» бы работу, которую прошлая версия объявила начатой (PLAN 20.1)."""
    dataset, travel, parent = day()
    version = replanned(
        dataset,
        travel,
        parent,
        CancelRequestEvent(type="cancel_request", time="10:30", request_id="3"),
    )

    with pytest.raises(replan.ReplanError) as error:
        replan.prepare(
            version,
            EngineerUnavailableEvent(
                type="engineer_unavailable", time="09:30", engineer_id="brigade-1"
            ),
            ReplanMode.FROM_EVENT,
            travel,
        )
    assert error.value.status == 400


def test_event_after_a_full_version_may_be_earlier_than_it(day):
    """«Весь день заново» стоит на шкале с 00:00 (блок 33): закреплённого в нём нет, и событие
    раньше самого пересчёта ничего начатого не размораживает."""
    dataset, travel, parent = day()
    full = replanned(
        dataset,
        travel,
        parent,
        CancelRequestEvent(type="cancel_request", time="10:30", request_id="3"),
        mode=ReplanMode.FULL,
    )
    assert replan.earliest(full) == 0

    plan = replanned(
        dataset,
        travel,
        full,
        CancelRequestEvent(type="cancel_request", time="09:30", request_id="2"),
    )
    assert ids(plan.routes[0]) == ["1"]
    assert plan.routes[0].stops[0].committed  # выезд 09:00 — до события 09:30
    assert plan.validation.ok


def test_event_after_a_full_version_is_not_earlier_than_its_facts(day):
    """Факт — то, что уже случилось: в прошлое до него не уйти и после пересчёта дня."""
    dataset, travel, parent = day()
    closed = replanned(
        dataset,
        travel,
        parent,
        CloseRequestEvent(
            type="close_request",
            time="10:30",
            request_id="1",
            outcome=Outcome.DONE,
            actual_end="10:10",
        ),
    )
    full = replanned(
        dataset,
        travel,
        closed,
        CancelRequestEvent(type="cancel_request", time="11:00", request_id="3"),
        mode=ReplanMode.FULL,
    )
    assert replan.earliest(full) == 10 * 3600 + 10 * 60  # фактическое окончание

    with pytest.raises(replan.ReplanError) as error:
        replan.prepare(
            full,
            CancelRequestEvent(type="cancel_request", time="10:00", request_id="2"),
            ReplanMode.FROM_EVENT,
            travel,
        )
    assert error.value.status == 400
    assert "последнего факта или переноса (10:10)" in str(error.value)


def test_full_mode_does_not_pull_the_window_of_an_added_request(idle):
    """В режиме `full` заявка известна с утра: окно не прижимается к моменту события."""
    dataset, travel, parent, urgent = idle
    plan = replanned(
        dataset,
        travel,
        parent,
        AddRequestEvent(type="add_request", time="13:00", request=urgent),
        mode=ReplanMode.FULL,
    )

    added = {request.id: request for request in plan.input.requests}["9"]
    assert added.window_start == "09:00"
    stop = plan.routes[0].stops[0]
    assert (stop.departure, stop.start) == ("09:00", "09:10")
    assert plan.validation.ok


def test_started_request_cannot_be_cancelled_even_in_full_mode(day):
    """Режим `full` сравнительный, но работу, которая уже идёт, он не отменяет."""
    _, travel, parent = day()
    with pytest.raises(replan.ReplanError) as error:
        replan.prepare(
            parent,
            CancelRequestEvent(type="cancel_request", time="10:30", request_id="1"),
            ReplanMode.FULL,
            travel,
        )
    assert error.value.status == 409


def test_validator_rejects_a_forged_committed_flag(day):
    """Флаг `committed` выключает проверку выезда, поэтому проверяется и он сам."""
    dataset, travel, parent = day(engineers=2)
    plan = replanned(
        dataset,
        travel,
        parent,
        EngineerUnavailableEvent(
            type="engineer_unavailable", time="10:15", engineer_id="brigade-2"
        ),
    )
    stop = next(stop for stop in plan.routes[0].stops if not stop.committed)

    forged = plan.model_copy(deep=True)
    next(
        item for item in forged.routes[0].stops if item.request_id == stop.request_id
    ).committed = True

    result = validator.validate(forged, travel)
    assert not result.ok
    assert any("закреплена, но выезд" in error for error in result.errors)


def test_validator_rejects_committed_stops_in_a_first_plan(day):
    """В первичном плане закреплять нечего: события ещё не было."""
    _, travel, parent = day()
    forged = parent.model_copy(deep=True)
    forged.routes[0].stops[0].committed = True

    result = validator.validate(forged, travel)
    assert not result.ok
    assert any("не с момента события" in error for error in result.errors)


# --- новая версия плана ------------------------------------------------------


def test_new_version_records_the_event(day):
    dataset, travel, parent = day()
    event = CancelRequestEvent(type="cancel_request", time="10:30", request_id="3")
    plan = replanned(dataset, travel, parent, event)

    assert plan.id != parent.id
    assert plan.parent_plan_id == parent.id
    assert plan.event == event
    assert plan.replan_mode is ReplanMode.FROM_EVENT


def test_every_request_of_the_day_is_explained(day):
    """Закреплённая заявка тоже подписана: диспетчер должен видеть, почему её не двигали."""
    dataset, travel, parent = day()
    plan = replanned(
        dataset,
        travel,
        parent,
        CancelRequestEvent(type="cancel_request", time="10:30", request_id="3"),
    )

    assert set(plan.explanations) == {request.id for request in plan.input.requests}
    assert "закреплена" in plan.explanations["1"].text
    assert "закреплено" in plan.route_explanations["brigade-1"].order


def test_optimized_replan_preserves_every_committed_stop(
    day, make_request, monkeypatch
):
    """Оптимизатор получает точки продолжения и прошлый план: закреплённое он не трогает.

    Какая бригада что взяла, тест не утверждает: поиск эвристический (PLAN 6.5). Проверяется
    контракт — закреплённый префикс каждого маршрута переходит в новую версию как есть.
    """
    monkeypatch.setattr(settings, "solver_time_limit_sec", 1)
    monkeypatch.setattr(settings, "replan_time_limit_sec", 1)
    urgent = make_request("9", **WIDE)
    dataset, travel, _ = day(engineers=2, extra=[urgent])
    parent = service.build_plan(dataset, travel, Algorithm.OPTIMIZED)

    plan = replanned(
        dataset,
        travel,
        parent,
        AddRequestEvent(type="add_request", time="10:15", request=urgent),
    )

    now = routes_by_engineer(plan)
    for route in parent.routes:
        fixed = [stop for stop in route.stops if stop.departure <= "10:15"]
        kept = now[route.engineer_id].stops[: len(fixed)]
        assert [stop.request_id for stop in fixed] == [stop.request_id for stop in kept]
        assert all(stop.committed for stop in kept)
        assert [stop.start for stop in fixed] == [stop.start for stop in kept]
    assert plan.validation.ok


def test_route_line_covers_committed_stops(day, monkeypatch):
    """Линия рисуется от офиса через **все** стопы, включая закреплённые (PLAN 11.5).

    Точка продолжения лежит внутри маршрута, поэтому отдельного старта линии не нужно:
    иначе она потеряла бы закреплённую часть и разошлась бы с `route.km`.
    """
    dataset, travel, parent = day()
    plan = replanned(
        dataset,
        travel,
        parent,
        CancelRequestEvent(type="cancel_request", time="10:30", request_id="3"),
    )
    monkeypatch.setattr(valhalla, "route", lambda locations, costing: [list(locations)])

    geometry.fill(plan, travel)

    route = plan.routes[0]
    assert all(stop.committed for stop in route.stops)
    assert len(route.geometry) == len(route.stops) + 1  # офис плюс каждый стоп


def test_replan_counts_the_point_of_an_urgent_request(
    make_request, make_engineer, make_dataset, make_travel, monkeypatch
):
    """Срочной заявки в наборе нет, поэтому её точку досчитывает расчёт (PLAN 5.5)."""
    request = make_request("1", **WIDE)
    urgent = make_request("9", **WIDE)
    dataset = make_dataset([request], [make_engineer("brigade-1")])
    travel = make_travel([request])  # точки срочной заявки в таблицах ещё нет
    monkeypatch.setattr(matrices, "get", lambda _: travel)
    monkeypatch.setattr(valhalla, "route", lambda locations, costing: [list(locations)])
    added: list[str] = []
    monkeypatch.setattr(
        type(travel),
        "add_point",
        lambda self, pid, lat, lon: _fake_point(self, pid, lat, lon, added),
    )

    monkeypatch.setattr(db, "plans", _Sink())
    parent = service.build_plan(dataset, travel)

    plan = service.replan_plan(
        parent,
        dataset,
        AddRequestEvent(type="add_request", time="10:00", request=urgent),
        ReplanMode.FROM_EVENT,
    )

    assert added == ["req:9"]
    assert plan.assignments["9"] == "brigade-1"
    assert plan.validation.ok


def test_chained_events_count_points_of_earlier_urgent_requests(
    make_request, make_engineer, make_dataset, make_travel, monkeypatch
):
    """Второе событие: срочной заявки первого в наборе нет, а расписание считается по ней.

    Таблицы каждый расчёт берёт заново, по набору из базы, — поэтому её точку приходится
    досчитывать снова, и обязательно **до** фиксации.
    """
    request = make_request("1", **WIDE)
    first, second = make_request("9", **WIDE), make_request("8", **WIDE)
    dataset = make_dataset([request], [make_engineer("brigade-1")])
    monkeypatch.setattr(matrices, "get", lambda _: make_travel([request]))
    monkeypatch.setattr(valhalla, "route", lambda locations, costing: [list(locations)])
    monkeypatch.setattr(db, "plans", _Sink())
    added: list[str] = []
    monkeypatch.setattr(
        Travel,
        "add_point",
        lambda self, pid, lat, lon: _fake_point(self, pid, lat, lon, added),
    )

    parent = service.build_plan(dataset, make_travel([request]))
    version = service.replan_plan(
        parent,
        dataset,
        AddRequestEvent(type="add_request", time="10:00", request=first),
        ReplanMode.FROM_EVENT,
    )
    version = service.replan_plan(
        version,
        dataset,
        AddRequestEvent(type="add_request", time="11:00", request=second),
        ReplanMode.FROM_EVENT,
    )

    assert added == [
        "req:9",
        "req:9",
        "req:8",
    ]  # на второй версии точка первой считается заново
    assert {item.id for item in version.input.requests} == {"1", "9", "8"}
    assert version.validation.ok


def _fake_point(travel, new_id, lat, lon, added):
    """Строка и столбец с плечом по умолчанию — вместо двух запросов в Valhalla."""
    added.append(new_id)
    for matrix in travel.matrices.values():
        matrix.point_ids.append(new_id)
        for row in matrix.durations:
            row.append(600)
        for row in matrix.distances:
            row.append(5000)
        matrix.durations.append([600] * len(matrix.durations[0]))
        matrix.distances.append([5000] * len(matrix.distances[0]))
    travel.points.append((new_id, lat, lon))
    travel.index[new_id] = len(travel.points) - 1


class _Sink:
    """Заглушка коллекции: этому тесту нужна не база, а вызов досчёта точки."""

    def insert_one(self, document):
        return None


# --- сохранение --------------------------------------------------------------


def test_event_saves_a_version_and_leaves_the_parent_alone(mongo, day, monkeypatch):
    """Планы неизменяемы: событие создаёт документ, а прежний остаётся как был."""
    dataset, travel, _ = day()
    monkeypatch.setattr(matrices, "get", lambda _: travel)
    monkeypatch.setattr(valhalla, "route", lambda locations, costing: [list(locations)])

    parent = service.create_plan(dataset)
    before = service.get_plan(parent.id).model_dump()
    plan = service.replan_plan(
        parent,
        dataset,
        CancelRequestEvent(type="cancel_request", time="10:30", request_id="3"),
        ReplanMode.FROM_EVENT,
    )

    assert service.get_plan(parent.id).model_dump() == before
    stored = service.get_plan(plan.id)
    assert stored is not None
    assert stored.parent_plan_id == parent.id
    assert stored.diff == plan.diff


# --- бригада вышла в течение дня (PLAN 6.19, блок 24.7) ----------------------


def test_added_engineer_takes_work_only_after_the_event(
    day, make_engineer, make_travel, make_request
):
    """Бригада, вышедшая в 12:00, не получает заявок с утра: availableFrom = T."""
    dataset, _, _ = day()
    joined = make_engineer("brigade-9", shiftStart="09:00", shiftEnd="18:00")
    travel = make_travel(dataset.requests, engineers=[*dataset.engineers, joined])
    parent = service.build_plan(dataset, travel)

    plan = replanned(
        dataset,
        travel,
        parent,
        AddEngineerEvent(type="add_engineer", time="12:00", engineer=joined),
    )

    route = routes_by_engineer(plan)["brigade-9"]
    assert plan.validation.ok
    assert all(stop.departure >= "12:00" for stop in route.stops)
    assert any(engineer.id == "brigade-9" for engineer in plan.input.engineers)


def test_added_engineer_starts_from_its_own_address(day, make_engineer, make_travel):
    """Старт новой бригады — её собственный адрес, а не офис (ответ 13)."""
    dataset, _, _ = day()
    joined = make_engineer("brigade-9", startLat=54.84, startLon=38.19)
    home = "start:54.840000,38.190000"
    travel = make_travel(
        dataset.requests,
        engineers=[*dataset.engineers, joined],
        legs={(home, "3"): (1800, 30_000)},
    )
    parent = service.build_plan(dataset, travel)

    state = replan.prepare(
        parent,
        AddEngineerEvent(type="add_engineer", time="10:00", engineer=joined),
        ReplanMode.FROM_EVENT,
        travel,
    )

    assert state.positions["brigade-9"].point_id == home
    assert state.positions["brigade-9"].available_from == 10 * 3600


def test_added_engineer_works_the_whole_day_in_full_mode(
    day, make_engineer, make_travel
):
    """Режим `full` — сравнительный расчёт «как если бы бригада была с утра» (PLAN 6.12)."""
    dataset, _, _ = day()
    joined = make_engineer("brigade-9")
    travel = make_travel(dataset.requests, engineers=[*dataset.engineers, joined])
    parent = service.build_plan(dataset, travel)

    state = replan.prepare(
        parent,
        AddEngineerEvent(type="add_engineer", time="12:00", engineer=joined),
        ReplanMode.FULL,
        travel,
    )

    assert state.positions["brigade-9"].available_from is None


def test_added_engineer_must_be_new(day, make_engineer, make_travel):
    _, travel, parent = day()
    twin = make_engineer("brigade-1")

    with pytest.raises(replan.ReplanError) as error:
        replan.prepare(
            parent,
            AddEngineerEvent(type="add_engineer", time="10:00", engineer=twin),
            ReplanMode.FROM_EVENT,
            travel,
        )
    assert error.value.status == 400


def test_added_engineer_with_a_finished_shift_is_rejected(
    day, make_engineer, make_travel
):
    """Смена закончилась до события — выходить уже некуда."""
    _, travel, parent = day()
    late = make_engineer("brigade-9", shiftStart="09:00", shiftEnd="11:00")

    with pytest.raises(replan.ReplanError) as error:
        replan.prepare(
            parent,
            AddEngineerEvent(type="add_engineer", time="12:00", engineer=late),
            ReplanMode.FROM_EVENT,
            travel,
        )
    assert error.value.status == 400


# --- заявка, поступившая днём (PLAN 6.19) ------------------------------------


def test_added_request_can_be_ordinary(day, make_request, make_travel):
    """Диспетчер вправе добавить днём обычную заявку, а не срочную (PLAN 6.19)."""
    dataset, _, _ = day()
    added = make_request("9", **WIDE)
    travel = make_travel([*dataset.requests, added])
    parent = service.build_plan(dataset, travel)

    plan = replanned(
        dataset,
        travel,
        parent,
        AddRequestEvent(type="add_request", time="10:00", request=added, urgent=False),
    )

    (fresh,) = [item for item in plan.input.requests if item.id == "9"]
    assert fresh.urgent is False
    assert fresh.window_start == "10:00"  # начало не раньше поступления (ответ 5)
    assert plan.metrics.assigned_urgent == 0


def test_the_old_event_name_is_still_accepted(day, make_request, make_travel):
    """`urgent_request` — прежнее имя события: сценарии и README не должны ломаться."""
    dataset, _, _ = day()
    added = make_request("9", **WIDE)
    travel = make_travel([*dataset.requests, added])
    parent = service.build_plan(dataset, travel)

    plan = replanned(
        dataset,
        travel,
        parent,
        AddRequestEvent(type="urgent_request", time="10:00", request=added),
    )

    (fresh,) = [item for item in plan.input.requests if item.id == "9"]
    assert fresh.urgent is True


# --- состояние дня переживает цепочку событий (PLAN 6.12, 6.19) --------------


def test_joined_engineer_keeps_its_start_time_through_the_chain(
    make_request, make_engineer, make_dataset, make_travel
):
    """Время выхода записано в бригаду, поэтому следующее событие его не теряет.

    Иначе закреплённый стоп вышедшей в 12:00 бригады пересчитывался бы от начала смены,
    и её день оказался бы занят дважды — причём проверка этого не заметила бы, потому
    что делала ту же ошибку.
    """
    requests = [make_request("1", **WIDE), make_request("2", **WIDE)]
    # Своя бригада успевает только первую заявку: смена до 11:00.
    own = make_engineer("brigade-1", shiftEnd="11:00")
    joined = make_engineer("brigade-9")
    dataset = make_dataset(requests, [own])
    travel = make_travel(
        [*requests, make_request("9", **WIDE)], engineers=[own, joined]
    )
    first = service.build_plan(dataset, travel)

    second = replanned(
        dataset,
        travel,
        first,
        AddEngineerEvent(type="add_engineer", time="12:00", engineer=joined),
    )
    third = replanned(
        dataset,
        travel,
        second,
        AddRequestEvent(
            type="add_request", time="13:00", request=make_request("9", **WIDE)
        ),
    )

    was = routes_by_engineer(second)["brigade-9"].stops[0]
    now = routes_by_engineer(third)["brigade-9"].stops[0]
    assert (was.departure, was.end) == ("12:00", "13:10")
    assert (now.departure, now.end) == (was.departure, was.end)
    assert now.committed
    assert third.validation.ok
    # Время выхода видно в самом плане, а не только в памяти расчёта.
    joined_now = next(e for e in third.input.engineers if e.id == "brigade-9")
    assert joined_now.available_from == "12:00"


def test_unavailable_engineer_stays_unavailable_through_the_chain(day, make_request):
    """Диспетчер сказал «бригада выбыла» — следующее событие обязано об этом помнить."""
    dataset, travel, parent = day(extra=[make_request("9", **WIDE)])

    gone = replanned(
        dataset,
        travel,
        parent,
        EngineerUnavailableEvent(
            type="engineer_unavailable", time="10:15", engineerId="brigade-1"
        ),
    )
    later = replanned(
        dataset,
        travel,
        gone,
        AddRequestEvent(type="add_request", time="11:00", request=_ninth(dataset)),
    )

    assert ids(routes_by_engineer(gone)["brigade-1"]) == ["1", "2"]
    assert ids(routes_by_engineer(later)["brigade-1"]) == ["1", "2"]
    assert {item.request_id for item in later.unassigned} == {"3", "9"}
    left = next(e for e in later.input.engineers if e.id == "brigade-1")
    assert left.unavailable_from == "10:15"


def test_validator_catches_work_given_to_an_unavailable_engineer(day):
    """Подделка: выбывшей бригаде дописали стоп после её ухода."""
    dataset, travel, parent = day()
    gone = replanned(
        dataset,
        travel,
        parent,
        EngineerUnavailableEvent(
            type="engineer_unavailable", time="10:15", engineerId="brigade-1"
        ),
    )
    route = routes_by_engineer(gone)["brigade-1"]
    route.stops.append(
        gone.routes[0]
        .stops[-1]
        .model_copy(
            update={
                "requestId": "3",
                "departure": "11:20",
                "arrival": "11:30",
                "start": "11:30",
                "end": "12:30",
                "committed": False,
            }
        )
    )
    gone.assignments["3"] = "brigade-1"
    gone.unassigned = [item for item in gone.unassigned if item.request_id != "3"]

    result = validator.validate(gone, travel)

    assert not result.ok
    assert any("недоступна" in error for error in result.errors)


def _ninth(dataset):
    """Заявка, которой нет в наборе: её точка уже есть в таблицах фикстуры."""
    return dataset.requests[0].model_copy(update={"id": "9"})


def test_engineer_on_a_new_transport_does_not_break_the_plan(
    make_request, make_engineer, make_dataset, make_travel, monkeypatch
):
    """Бригада вышла на транспорте, которым в наборе никто не ездил (ответ 13, 6.19).

    Профиля в таблицах нет, и раньше первый же её переезд отвечал 500. Досчитывает его
    `matrices.ensure_profiles` — там же, где досчитываются точки, то есть в расчёте,
    который умеет ходить в Valhalla, а не в чистой сборке плана.
    """
    requests = [make_request("1", **WIDE), make_request("2", **WIDE)]
    own = make_engineer("brigade-1", shiftEnd="11:00", transport="car")
    dataset = make_dataset(requests, [own])
    travel = make_travel(requests, engineers=[own])
    # В наборе ездят только на автомобиле — других профилей в таблицах нет.
    travel.matrices = {"auto": travel.matrices["auto"]}
    monkeypatch.setattr(matrices, "get", lambda _: travel)
    monkeypatch.setattr(valhalla, "route", lambda locations, costing: [list(locations)])
    monkeypatch.setattr(db, "plans", _Sink())
    counted: list[str] = []

    def profiles(table, data):
        counted.append("ensure")
        table.matrices["bicycle"] = table.matrices["auto"]

    monkeypatch.setattr(matrices, "ensure_profiles", profiles)
    parent = service.build_plan(dataset, travel)

    plan = service.replan_plan(
        parent,
        dataset,
        AddEngineerEvent(
            type="add_engineer",
            time="10:00",
            engineer=make_engineer("brigade-9", transport="bicycle"),
        ),
        ReplanMode.FROM_EVENT,
    )

    # Досчёт профилей спрашивается дважды: сначала за бригадой из события — до фиксации,
    # иначе проверка её закреплённой части упала бы `KeyError` (ревью блока 28), — потом за
    # днём целиком. Второй раз считать уже нечего: недостающих профилей в нём нет.
    assert counted == ["ensure", "ensure"]
    assert plan.validation.ok
    assert plan.assignments["2"] == "brigade-9"


# --- правка заявки и бригады, перенос на завтра (PLAN 6.19, блок 28) ---------


def test_edited_request_takes_the_new_fields(day, make_request):
    """Правка заявки в течение дня меняет день, а не набор (PLAN 6.19)."""
    dataset, travel, plan = day()
    edited = make_request("3", **WIDE, serviceDurationMin=15)

    later = replanned(
        dataset,
        travel,
        plan,
        UpdateRequestEvent(type="update_request", time="10:15", request=edited),
    )

    assert later.validation.errors == []
    request = next(item for item in later.input.requests if item.id == "3")
    assert request.service_duration_min == 15
    # Работа стала короче, поэтому третий стоп кончается раньше: день пересчитан по новым полям.
    stop = routes_by_engineer(later)["brigade-1"].stops[-1]
    assert stop.request_id == "3"
    assert stop.end == "11:45"


def test_edited_request_is_refused_after_the_work_started(day, make_request):
    _, travel, plan = day()

    with pytest.raises(replan.ReplanError) as error:
        replan.prepare(
            plan,
            UpdateRequestEvent(
                type="update_request", time="10:15", request=make_request("1", **WIDE)
            ),
            ReplanMode.FROM_EVENT,
            travel,
        )

    assert error.value.status == 409
    assert "Работа уже началась" in str(error.value)


def test_edited_engineer_keeps_the_state_of_the_day(day, make_engineer):
    """Смена и навыки берутся из тела, состояние дня — у родителя (PLAN 6.12)."""
    dataset, travel, plan = day()
    edited = make_engineer("brigade-1", shiftEnd="20:00", availableFrom="06:00")

    later = replanned(
        dataset,
        travel,
        plan,
        UpdateEngineerEvent(type="update_engineer", time="10:15", engineer=edited),
    )

    engineer = later.input.engineers[0]
    assert engineer.shift_end == "20:00"
    assert engineer.available_from is None  # из тела не читается
    assert ids(routes_by_engineer(later)["brigade-1"])[:2] == ["1", "2"]  # закреплённое


def test_edited_engineer_is_refused_when_it_breaks_committed_work(day, make_engineer):
    """Смену сократили под уже начатую работу — правка отклоняется целиком."""
    _, travel, plan = day()

    with pytest.raises(replan.ReplanError) as error:
        replan.prepare(
            plan,
            UpdateEngineerEvent(
                type="update_engineer",
                time="10:15",
                engineer=make_engineer("brigade-1", shiftEnd="10:30"),
            ),
            ReplanMode.FROM_EVENT,
            travel,
        )

    assert error.value.status == 409
    assert "уже начатую работу" in str(error.value)


@pytest.mark.parametrize("mode", [ReplanMode.FROM_EVENT, ReplanMode.FULL])
def test_deferred_request_leaves_the_day_in_both_modes(day, mode):
    dataset, travel, plan = day()

    later = replanned(
        dataset,
        travel,
        plan,
        DeferRequestEvent(type="defer_request", time="10:15", request_id="3"),
        mode,
    )

    assert later.validation.errors == []
    assert later.deferred == {"3": "10:15"}
    assert "3" not in [
        stop.request_id for route in later.routes for stop in route.stops
    ]
    item = next(item for item in later.unassigned if item.request_id == "3")
    assert item.reason_code == "DEFERRED"
    assert item.defer_next_day
    assert later.metrics.deferred_count == 1
    assert [c.change for c in later.diff.requests if c.request_id == "3"] == [
        Change.DEFERRED
    ]


def test_deferred_request_is_refused_after_the_work_started(day):
    _, travel, plan = day()

    with pytest.raises(replan.ReplanError) as error:
        replan.prepare(
            plan,
            DeferRequestEvent(type="defer_request", time="10:15", request_id="1"),
            ReplanMode.FROM_EVENT,
            travel,
        )

    assert error.value.status == 409


def test_deferred_request_stays_deferred_in_the_next_version(day):
    """Перенос наследуется цепочкой: следующее событие не возвращает заявку в день."""
    dataset, travel, plan = day()
    first = replanned(
        dataset,
        travel,
        plan,
        DeferRequestEvent(type="defer_request", time="10:15", request_id="3"),
    )

    # Вторым событием берём недоступность бригады: заявки 1 и 2 к 10:20 уже начаты,
    # и отменить их нельзя — проверяем именно наследование переноса.
    second = replanned(
        dataset,
        travel,
        first,
        EngineerUnavailableEvent(
            type="engineer_unavailable", time="10:20", engineer_id="brigade-1"
        ),
    )

    assert second.deferred == {"3": "10:15"}
    assert "3" not in [
        stop.request_id for route in second.routes for stop in route.stops
    ]


def test_edited_request_moves_its_point_in_the_tables(day, make_request, monkeypatch):
    """Правка адреса: точка заявки в таблицах пересчитывается, а не остаётся от прежнего.

    Идентификатор `req:3` от адреса не зависит, и без сверки координат остаток дня считался
    бы по старому месту (ревью блока 28).
    """
    dataset, travel, plan = day()
    moved = make_request("3", **WIDE, lat=55.9, lon=37.9)
    seen: list[tuple[str, float, float]] = []
    monkeypatch.setattr(matrices, "get", lambda _: travel)
    monkeypatch.setattr(valhalla, "route", lambda locations, costing: [list(locations)])
    monkeypatch.setattr(db, "plans", _Sink())
    monkeypatch.setattr(
        Travel,
        "add_point",
        lambda self, pid, lat, lon: _fake_point(self, pid, lat, lon, seen),
    )

    service.replan_plan(
        plan,
        dataset,
        UpdateRequestEvent(type="update_request", time="10:15", request=moved),
        ReplanMode.FROM_EVENT,
    )

    assert "req:3" in seen  # точку пересчитали, а не взяли прежнюю


def test_edited_engineer_may_not_move_committed_departures(day, make_engineer):
    """Правка смены двигает времена закреплённой части — и ломает само закрепление.

    Ревью блока 28: позиция продолжения оставалась от прежней бригады, остаток дня выезжал
    раньше конца закреплённой работы, и независимая проверка отвечала 500. Теперь такая
    правка отклоняется на входе, а не превращается в противоречивый план.
    """
    dataset, travel, plan = day()

    event = UpdateEngineerEvent(
        type="update_engineer",
        time="10:15",
        engineer=make_engineer("brigade-1", shiftStart="09:30"),
    )

    with pytest.raises(replan.ReplanError) as error:
        replan.prepare(plan, event, ReplanMode.FROM_EVENT, travel)

    assert error.value.status == 409
    assert "уже начата" in str(error.value)

    # Тот же день, пересчитанный целиком, правку принимает: в режиме `full` ничего
    # не закрепляется, и сдвигать нечего.
    later = replanned(dataset, travel, plan, event, ReplanMode.FULL)
    assert later.validation.errors == []
    assert later.input.engineers[0].shift_start == "09:30"


def test_edited_engineer_may_switch_to_an_unused_transport(
    day, make_engineer, monkeypatch
):
    """Транспорт, которым в наборе никто не ездил: профиля в таблицах нет, его досчитывают.

    Ревью блока 28: профиль досчитывался **после** подготовки состояния, а проверка
    закреплённой части идёт внутри неё — и правка падала `KeyError` в 500.
    """
    dataset, travel, plan = day()
    travel.matrices = {"auto": travel.matrices["auto"]}
    monkeypatch.setattr(matrices, "get", lambda _: travel)
    monkeypatch.setattr(valhalla, "route", lambda locations, costing: [list(locations)])
    monkeypatch.setattr(db, "plans", _Sink())

    # Режим `full`: транспорт задним числом меняет и времена утренних выездов, поэтому
    # с закреплённой частью такая правка отклоняется (тест выше).
    later = service.replan_plan(
        plan,
        dataset,
        UpdateEngineerEvent(
            type="update_engineer",
            time="10:15",
            engineer=make_engineer("brigade-1", transport="bicycle"),
        ),
        ReplanMode.FULL,
    )

    assert later.validation.errors == []
    assert later.input.engineers[0].transport == "bicycle"


def test_edited_engineer_that_is_not_in_the_plan_is_rejected(day, make_engineer):
    _, travel, plan = day()

    with pytest.raises(replan.ReplanError) as error:
        replan.prepare(
            plan,
            UpdateEngineerEvent(
                type="update_engineer",
                time="10:15",
                engineer=make_engineer("brigade-404"),
            ),
            ReplanMode.FROM_EVENT,
            travel,
        )

    assert error.value.status == 400
    assert "нет в плане" in str(error.value)


def test_cancelled_request_leaves_the_deferred_list(day):
    """Отменённой заявки в дне нет, и в перенесённых ей делать нечего."""
    dataset, travel, plan = day()
    deferred = replanned(
        dataset,
        travel,
        plan,
        DeferRequestEvent(type="defer_request", time="10:15", request_id="3"),
    )

    cancelled = replanned(
        dataset,
        travel,
        deferred,
        CancelRequestEvent(type="cancel_request", time="10:20", request_id="3"),
    )

    assert cancelled.deferred == {}
    assert cancelled.metrics.deferred_count == 0
    assert cancelled.validation.errors == []


# --- обычная заявка днём встраивается, а не перестраивает план (блок 30) -----


def test_ordinary_request_is_inserted_without_touching_the_rest(
    day, make_request, make_travel
):
    """Вставка в свободное время: остальные заявки не меняют ни бригаду, ни порядок."""
    dataset, _, _ = day(engineers=2)
    added = make_request("9", **WIDE)
    travel = make_travel([*dataset.requests, added])
    parent = service.build_plan(dataset, travel)

    plan = replanned(
        dataset,
        travel,
        parent,
        AddRequestEvent(type="add_request", time="10:15", request=added),
        ReplanMode.INSERT,
    )

    routes = routes_by_engineer(plan)
    # Встала между началом дня и заявкой 3 — у бригады, которая и так работает: простаивающую
    # ради неё не поднимаем. Заявка 3 сдвинулась, но осталась у своей бригады.
    assert ids(routes["brigade-1"]) == ["1", "2", "9", "3"]
    assert ids(routes["brigade-2"]) == []
    # Номер места у заявки 3 вырос на единицу — это `order_changed` в diff; бригаду же
    # и взаимный порядок не меняет никто.
    assert {change.change for change in plan.diff.requests} <= {
        Change.ADDED,
        Change.ORDER_CHANGED,
        Change.TIME_CHANGED,
    }
    assert plan.replan_mode is ReplanMode.INSERT
    assert "встроена в свободное время" in plan.explanations["9"].text
    assert plan.validation.errors == []


def test_inserted_version_keeps_the_rest_of_the_day_to_the_minute(
    make_request, make_engineer, make_dataset, make_travel
):
    """Заявка, которой места нет: план остаётся родительским до минуты.

    Заявка 3 обязана начаться не позже 11:30, а новая — не позже 12:30. Перед третьей новая
    ломает её окно, после третьей не успевает сама. В пустой маршрут бригады она встала бы,
    поэтому причина — `NOT_FITTED`, и текст подсказывает пересчитать день.
    """
    requests = [
        make_request("1", **WIDE),
        make_request("2", **WIDE),
        make_request("3", windowStart="09:00", windowEnd="11:30"),
    ]
    added = make_request("9", windowStart="12:00", windowEnd="12:30")
    dataset = make_dataset(requests, [make_engineer("brigade-1")])
    travel = make_travel([*requests, added])
    parent = service.build_plan(dataset, travel)

    plan = replanned(
        dataset,
        travel,
        parent,
        AddRequestEvent(type="add_request", time="10:15", request=added),
        ReplanMode.INSERT,
    )

    assert [
        (s.request_id, s.departure, s.start, s.end) for s in plan.routes[0].stops
    ] == [(s.request_id, s.departure, s.start, s.end) for s in parent.routes[0].stops]
    (item,) = plan.unassigned
    assert (item.request_id, item.reason_code) == ("9", "NOT_FITTED")
    # Подсказка — и в объяснении, и в причине: таблица неназначенных показывает причину.
    assert "пересчитайте день" in plan.explanations["9"].text
    assert "пересчитайте день" in item.reason_text
    assert plan.validation.errors == []


def test_inserted_version_keeps_the_fallback_mark(day, make_request, make_travel):
    """Маршруты вставки — родительские: подмена базовым вариантом не пропадает молча."""
    dataset, _, _ = day()
    added = make_request("9", **WIDE)
    travel = make_travel([*dataset.requests, added])
    parent = service.build_plan(dataset, travel)
    parent.fallback_to_baseline = True

    plan = replanned(
        dataset,
        travel,
        parent,
        AddRequestEvent(type="add_request", time="10:15", request=added),
        ReplanMode.INSERT,
    )

    assert plan.fallback_to_baseline


def test_insert_mode_is_refused_for_anything_but_a_new_request(day):
    _, travel, plan = day()

    with pytest.raises(replan.ReplanError) as error:
        replan.prepare(
            plan,
            CancelRequestEvent(type="cancel_request", time="10:15", request_id="3"),
            ReplanMode.INSERT,
            travel,
        )

    assert error.value.status == 400


def test_default_mode_depends_on_the_event(make_request):
    """Обычная заявка встраивается, авария и остальные события пересчитывают остаток."""
    ordinary = make_request("9", **WIDE)
    emergency = make_request("9", **WIDE, workPriority="emergency", skill="emergency")

    assert (
        replan.default_mode(
            AddRequestEvent(type="add_request", time="10:00", request=ordinary)
        )
        is ReplanMode.INSERT
    )
    assert (
        replan.default_mode(
            AddRequestEvent(type="add_request", time="10:00", request=emergency)
        )
        is ReplanMode.FROM_EVENT
    )
    assert (
        replan.default_mode(
            CancelRequestEvent(type="cancel_request", time="10:00", request_id="3")
        )
        is ReplanMode.FROM_EVENT
    )


@pytest.mark.parametrize(
    "event",
    [
        {"type": "add_request", "urgent": True},
        {"type": "urgent_request"},
        {"type": "add_request", "request_urgent": True},
    ],
    ids=["флаг события", "прежнее имя", "флаг в заявке"],
)
def test_urgent_ordinary_request_is_not_just_inserted(make_request, event):
    """Срочную заявку встраиванием не продвинуть вперёд других — она пересчитывает остаток."""
    request = make_request("9", **WIDE, urgent=event.pop("request_urgent", False))
    added = AddRequestEvent(time="10:00", request=request, **event)

    assert replan.default_mode(added) is ReplanMode.FROM_EVENT


def test_urgent_flag_of_the_request_survives_without_the_event_flag(idle):
    dataset, travel, parent, added = idle
    plan = replanned(
        dataset,
        travel,
        parent,
        AddRequestEvent(
            type="add_request",
            time="13:00",
            request=added.model_copy(update={"urgent": True}),
        ),
    )

    fresh = {request.id: request for request in plan.input.requests}["9"]
    assert fresh.urgent is True


# --- авария днём: срочность и срок реакции (блок 30) -------------------------


@pytest.fixture
def emergency_day(make_request, make_engineer, make_dataset, make_travel):
    """Аварийная бригада на автомобиле без заявок и авария, поступающая в 13:00."""

    def make(legs=None):
        blocked = make_request("1", **WIDE)  # не по навыку: день бригады пуст
        crew = make_engineer(
            "brigade-1", skills=["emergency"], transport="car", shiftEnd="22:00"
        )
        emergency = make_request(
            "9",
            windowStart="09:00",
            windowEnd="20:00",
            workPriority="emergency",
            skill="emergency",
            requiredTransport="car",
        )
        dataset = make_dataset([blocked], [crew])
        travel = make_travel([blocked, emergency], legs=legs)
        return dataset, travel, service.build_plan(dataset, travel), emergency

    return make


def test_emergency_window_ends_at_the_reaction_deadline(emergency_day):
    """Окно до 20:00 сужается до двух часов с момента поступления: 13:00–15:00."""
    dataset, travel, parent, emergency = emergency_day()

    plan = replanned(
        dataset,
        travel,
        parent,
        AddRequestEvent(type="add_request", time="13:00", request=emergency),
    )

    added = {request.id: request for request in plan.input.requests}["9"]
    assert (added.window_start, added.window_end) == ("13:00", "15:00")
    assert added.urgent  # авария, пришедшая днём, срочна и без флага
    assert plan.assignments["9"] == "brigade-1"
    assert "срок реакции на аварию: 120 мин" in plan.explanations["9"].text
    assert plan.validation.errors == []


def test_emergency_nobody_reaches_in_time_is_explained(emergency_day):
    """Три часа пути при сроке в два: причина называет срок реакции."""
    dataset, travel, parent, emergency = emergency_day(
        legs={(OFFICE, "9"): (3 * 3600, 90_000)}
    )

    plan = replanned(
        dataset,
        travel,
        parent,
        AddRequestEvent(type="add_request", time="13:00", request=emergency),
    )

    (item,) = [item for item in plan.unassigned if item.request_id == "9"]
    assert item.reason_code == "NO_TIME"
    assert item.reason_text.endswith(
        "16:00. Конец окна — срок реакции на аварию: 120 мин с момента, "
        "когда работу можно начать."
    )


def test_emergency_scheduled_for_later_counts_the_deadline_from_its_window(
    emergency_day,
):
    """Авария с окном на вечер: срок реакции считается от начала окна, а не от события.

    Иначе окно 18:00–21:00 при событии в 10:00 сжалось бы до 18:00–12:00, и заявку
    отклонили бы словами «окно закончилось», что неправда.
    """
    dataset, travel, parent, emergency = emergency_day()
    later = emergency.model_copy(
        update={"window_start": "18:00", "window_end": "21:00"}
    )

    plan = replanned(
        dataset,
        travel,
        parent,
        AddRequestEvent(type="add_request", time="10:00", request=later),
    )

    added = {request.id: request for request in plan.input.requests}["9"]
    assert (added.window_start, added.window_end) == ("18:00", "20:00")
    assert plan.validation.errors == []


def test_emergency_with_its_own_short_window_gets_no_deadline_note(emergency_day):
    """Окно до 14:00 при событии в 13:00 срок реакции не сужает — и оговорки нет."""
    dataset, travel, parent, emergency = emergency_day()
    short = emergency.model_copy(update={"window_end": "14:00"})

    plan = replanned(
        dataset,
        travel,
        parent,
        AddRequestEvent(type="add_request", time="13:00", request=short),
    )

    assert "срок реакции" not in plan.explanations["9"].text


def test_ordinary_request_keeps_its_window_and_is_not_urgent(idle):
    dataset, travel, parent, added = idle

    plan = replanned(
        dataset,
        travel,
        parent,
        AddRequestEvent(type="add_request", time="13:00", request=added),
    )

    fresh = {request.id: request for request in plan.input.requests}["9"]
    assert (fresh.window_start, fresh.window_end) == ("13:00", "17:00")
    assert fresh.urgent is False


# --- оборудование начатых работ уже унесено (PLAN 6.12, блок 27) -------------


def test_committed_equipment_counts_against_the_rest_of_the_day(
    make_request, make_engineer, make_dataset, make_travel
):
    """Две начатые заявки с роутером исчерпали вместимость пешего — третья не встаёт."""
    requests = [
        make_request(str(number), requiredEquipment=["router"], **WIDE)
        for number in (1, 2)
    ]
    added = make_request("9", requiredEquipment=["router"], **WIDE)
    walker = make_engineer(
        "brigade-1", transport="public_transport", equipment=["router"]
    )
    dataset = make_dataset(requests, [walker])
    travel = make_travel([*requests, added])
    parent = service.build_plan(dataset, travel)
    assert ids(parent.routes[0]) == ["1", "2"]

    plan = replanned(
        dataset,
        travel,
        parent,
        AddRequestEvent(type="add_request", time="10:15", request=added),
        ReplanMode.FROM_EVENT,
    )

    item = next(item for item in plan.unassigned if item.request_id == "9")
    assert item.reason_code == "NO_CAPACITY"
    assert "унесут не больше 0" in item.reason_text
    assert plan.validation.errors == []


def test_plan_over_a_lowered_capacity_still_takes_events(
    make_request, make_engineer, make_dataset, make_travel, monkeypatch
):
    """План посчитан при прежней вместимости, начатое уже за новой — день не ломается.

    Начатое пересмотру не подлежит, поэтому ни проверка, ни событие не спотыкаются о него:
    бригада берёт работу без оборудования и не берёт с оборудованием, а причина не уходит
    в «не больше −1».
    """
    requests = [
        make_request(str(number), requiredEquipment=["router"], **WIDE)
        for number in (1, 2)
    ]
    plain = make_request("8", **WIDE)
    boxed = make_request("9", requiredEquipment=["router"], **WIDE)
    walker = make_engineer(
        "brigade-1", transport="public_transport", equipment=["router"]
    )
    dataset = make_dataset(requests, [walker])
    travel = make_travel([*requests, plain, boxed])
    parent = service.build_plan(dataset, travel)
    monkeypatch.setattr(settings, "equipment_capacity", {"public_transport": 1})

    with_plain = replanned(
        dataset,
        travel,
        parent,
        AddRequestEvent(type="add_request", time="10:15", request=plain),
        ReplanMode.FROM_EVENT,
    )
    with_boxed = replanned(
        dataset,
        travel,
        with_plain,
        AddRequestEvent(type="add_request", time="10:20", request=boxed),
        ReplanMode.FROM_EVENT,
    )

    assert with_plain.validation.errors == []
    assert with_plain.assignments["8"] == "brigade-1"
    assert with_boxed.validation.errors == []
    item = next(item for item in with_boxed.unassigned if item.request_id == "9")
    assert item.reason_code == "NO_CAPACITY"
    assert "не больше 0" in item.reason_text


# --- отмена и перенос списка заявок (блок 42) ---------------------------------


def test_cancel_and_defer_take_a_list_and_still_read_the_old_single_form():
    """Прежний `requestId` лежит в сохранённых планах и сценариях: он читается как список из одной."""
    from pydantic import TypeAdapter, ValidationError

    from app.models import Event

    event = TypeAdapter(Event)

    assert event.validate_python(
        {"type": "cancel_request", "time": "10:00", "requestIds": ["1", "2"]}
    ).request_ids == ["1", "2"]
    assert event.validate_python(
        {"type": "cancel_request", "time": "10:00", "requestId": "1"}
    ).request_ids == ["1"]
    assert event.validate_python(
        {"type": "defer_request", "time": "10:00", "request_id": "1"}
    ).request_ids == ["1"]
    for bad in (
        {"type": "cancel_request", "time": "10:00", "requestIds": []},
        {"type": "cancel_request", "time": "10:00", "requestIds": ["1", "1"]},
        {"type": "defer_request", "time": "10:00"},
    ):
        with pytest.raises(ValidationError):
            event.validate_python(bad)


def test_one_event_cancels_the_whole_list_in_one_new_version(day):
    dataset, travel, parent = day()

    plan = replanned(
        dataset,
        travel,
        parent,
        CancelRequestEvent(type="cancel_request", time="08:00", requestIds=["1", "3"]),
    )

    assert plan.parent_plan_id is not None or plan.event is not None
    assert {request.id for request in plan.input.requests} == {"2"}
    assert set(plan.assignments) == {"2"}
    cancelled = {
        change.request_id
        for change in plan.diff.requests
        if change.change is Change.CANCELLED
    }
    assert cancelled == {"1", "3"}
    assert plan.validation.ok


def test_one_event_defers_the_whole_list(day):
    dataset, travel, parent = day()

    plan = replanned(
        dataset,
        travel,
        parent,
        DeferRequestEvent(type="defer_request", time="08:00", requestIds=["2", "3"]),
    )

    assert set(plan.deferred) == {"2", "3"}
    assert set(plan.assignments) == {"1", "2", "3"}
    assert plan.assignments["2"] is None and plan.assignments["3"] is None
    assert plan.metrics.deferred_count == 2
    assert plan.validation.ok


def test_a_list_is_applied_whole_or_not_at_all(day):
    """Одна начатая заявка в списке отклоняет событие целиком: половина отмены хуже отказа."""
    _, travel, parent = day()

    with pytest.raises(replan.ReplanError) as error:
        replan.prepare(
            parent,
            CancelRequestEvent(
                type="cancel_request", time="10:30", requestIds=["3", "1"]
            ),
            ReplanMode.FROM_EVENT,
            travel,
        )

    assert error.value.status == 409
    assert "№1" in str(error.value)
    # Вход родителя не тронут: заявка 3 из списка никуда не делась.
    assert {request.id for request in parent.input.requests} == {"1", "2", "3"}


def test_a_list_with_an_unknown_request_is_a_400(day):
    _, travel, parent = day()

    with pytest.raises(replan.ReplanError) as error:
        replan.prepare(
            parent,
            DeferRequestEvent(
                type="defer_request", time="08:00", requestIds=["1", "нет такой"]
            ),
            ReplanMode.FROM_EVENT,
            travel,
        )

    assert error.value.status == 400
