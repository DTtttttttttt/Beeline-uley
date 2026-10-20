"""Расписание, ограничения и лучшая вставка (PLAN 6.2, блок 6.3).

Значения по умолчанию (conftest): смена 09:00–18:00, окно 09:00–12:00, работа 60 минут,
переезд 10 минут и 5 км. От них и считаются все ожидаемые времена.
"""

from dataclasses import replace

import pytest

from app.config import settings
from app.dictionaries import DurationMode
from app.models import Office
from app.planning import schedule
from app.planning.schedule import Position, Violation
from app.routing import matrices
from app.routing.matrices import OFFICE, point_id
from app.timeutil import to_seconds

# Все бригады фикстур стоят на координатах офиса, поэтому их старт — точка `office`.
START = Position(
    OFFICE,
    equipment_used=0,
    duration_mode=DurationMode.NORM,
    finished={},
    turns={},
    lunch_done=True,
    after_work=False,
)


@pytest.fixture
def both_rules(request, monkeypatch):
    """Один и тот же случай в обеих трактовках окна (PLAN 6.2)."""
    monkeypatch.setattr(settings, "window_rule", request.param)
    return request.param


# --- квалификация и ресурс (PLAN 6.2, пп. 1-2) -------------------------------


@pytest.mark.parametrize(
    ("request_changes", "engineer_changes", "expected"),
    [
        ({}, {}, None),
        ({"skill": "emergency"}, {}, Violation.NO_SKILL),
        ({"requiredTransport": "bicycle"}, {}, Violation.NO_TRANSPORT),
        ({"requiredTransport": "car"}, {}, None),  # совпал с транспортом бригады
        ({"requiredEquipment": ["router"]}, {}, Violation.NO_EQUIPMENT),
        ({"requiredEquipment": ["router"]}, {"equipment": ["router", "alice"]}, None),
        # Инструменты — тот же ресурс «есть или нет» (PLAN 5.1, блок 27.8).
        ({"requiredTools": ["laptop"]}, {}, Violation.NO_EQUIPMENT),
        (
            {"requiredTools": ["laptop"]},
            {"tools": ["cable_tester"]},
            Violation.NO_EQUIPMENT,
        ),
        ({"requiredTools": ["laptop"]}, {"tools": ["laptop", "cable_tester"]}, None),
        # Оборудование есть, инструмента нет — всё равно не подходит.
        (
            {"requiredEquipment": ["router"], "requiredTools": ["laptop"]},
            {"equipment": ["router"]},
            Violation.NO_EQUIPMENT,
        ),
        # Транспорт не требуется — подходит бригада на любом.
        ({}, {"transport": "public_transport"}, None),
    ],
)
def test_resources(
    make_request, make_engineer, request_changes, engineer_changes, expected
):
    violation = schedule.resource_violation(
        make_request(**request_changes), make_engineer(**engineer_changes)
    )
    assert violation == expected


def test_resource_violation_is_stronger_than_time(
    make_request, make_engineer, make_travel
):
    """На одной заявке ресурс проверяется раньше времени."""
    task = make_request(skill="emergency", windowEnd="09:05")
    travel = make_travel([task])
    assert (
        schedule.first_violation(make_engineer(), [task], travel, START)
        is Violation.NO_SKILL
    )


def test_no_route(make_request, make_engineer, make_travel):
    """`null` от Valhalla — непроходимая вставка, а не нулевой переезд (PLAN 5.6)."""
    task = make_request()
    travel = make_travel([task], legs={("office", "1"): None})
    assert (
        schedule.first_violation(make_engineer(), [task], travel, START)
        is Violation.NO_ROUTE
    )
    with pytest.raises(ValueError):
        schedule.build_schedule(make_engineer(), [task], travel, START)


def test_best_insertion_into_an_impassable_route_is_none(
    make_request, make_engineer, make_travel
):
    """Маршрут, который бригада не проезжает (в «Что поможет» — копия на другом транспорте),
    не ломает подбор: вставлять некуда (блок 78, замер «Юго-восток»)."""
    tasks = [make_request("1"), make_request("2"), make_request("3")]
    travel = make_travel(tasks, legs={("1", "2"): None})

    spot = schedule.best_insertion(make_engineer(), tasks[:2], tasks[2], travel, START)
    assert spot is None


def test_no_route_after_another_violation(make_request, make_engineer, make_travel):
    """Непроходимый переезд нельзя потерять из-за более раннего отказа по ресурсу.

    Код нарушения занят первым по маршруту (NO_SKILL), но маршрута дальше всё равно нет,
    и расписание обязано упасть, а не вернуть обрезанный маршрут с заниженным пробегом.
    """
    tasks = [make_request("1", skill="emergency"), make_request("2")]
    travel = make_travel(tasks, legs={("1", "2"): None})

    assert (
        schedule.first_violation(make_engineer(), tasks, travel, START)
        is Violation.NO_SKILL
    )
    with pytest.raises(ValueError):
        schedule.build_schedule(make_engineer(), tasks, travel, START)


# --- расписание (PLAN 6.2) ---------------------------------------------------


def test_schedule_follows_the_route(make_request, make_engineer, make_travel):
    tasks = [make_request("1"), make_request("2")]
    travel = make_travel(tasks)

    first, second = schedule.build_schedule(make_engineer(), tasks, travel, START)

    assert (first.departure, first.arrival, first.start, first.end) == (
        to_seconds("09:00"),
        to_seconds("09:10"),
        to_seconds("09:10"),
        to_seconds("10:10"),
    )
    # Выезд ко второй заявке — сразу после окончания первой.
    assert (second.departure, second.arrival, second.start, second.end) == (
        to_seconds("10:10"),
        to_seconds("10:20"),
        to_seconds("10:20"),
        to_seconds("11:20"),
    )
    assert schedule.meters([first, second]) == 10_000


def test_early_arrival_waits(make_request, make_engineer, make_travel):
    """Раньше начала окна работу не начинаем — ждём (PLAN 6.2)."""
    task = make_request(windowStart="11:00", windowEnd="13:00")
    (visit,) = schedule.build_schedule(
        make_engineer(), [task], make_travel([task]), START
    )

    assert visit.arrival == to_seconds("09:10")
    assert visit.start == to_seconds("11:00")
    assert visit.end == to_seconds("12:00")


def test_continues_from_position(make_request, make_engineer, make_travel):
    """Перепланирование: точка продолжения и availableFrom (PLAN 6.3, 6.12)."""
    done, task = make_request("1"), make_request("2", windowEnd="18:00")
    travel = make_travel([done, task], legs={("1", "2"): (1800, 12_000)})
    position = Position(
        point_id=point_id("1"),
        available_from=to_seconds("13:00"),
        equipment_used=0,
        duration_mode=DurationMode.NORM,
        finished={},
        turns={},
        lunch_done=True,
        after_work=False,
    )

    (visit,) = schedule.build_schedule(make_engineer(), [task], travel, position)

    assert visit.departure == to_seconds("13:00")
    assert visit.arrival == to_seconds("13:30")  # 30 минут от точки продолжения
    assert visit.travel_m == 12_000


def test_available_from_before_shift_is_ignored(
    make_request, make_engineer, make_travel
):
    """Бригада не выезжает раньше начала смены."""
    task = make_request()
    position = Position(
        OFFICE,
        available_from=to_seconds("07:00"),
        equipment_used=0,
        duration_mode=DurationMode.NORM,
        finished={},
        turns={},
        lunch_done=True,
        after_work=False,
    )
    (visit,) = schedule.build_schedule(
        make_engineer(), [task], make_travel([task]), position
    )
    assert visit.departure == to_seconds("09:00")


# --- окно заявки: обе трактовки (PLAN 6.2) -----------------------------------


def test_arrival_after_window_end(make_request, make_engineer, make_travel):
    task = make_request(windowEnd="10:00")
    travel = make_travel([task], legs={("office", "1"): (4200, 5000)})  # прибытие 10:10
    assert (
        schedule.first_violation(make_engineer(), [task], travel, START)
        is Violation.WINDOW_END
    )


@pytest.mark.parametrize(
    ("both_rules", "expected"),
    [("start_in_window", None), ("fit_in_window", Violation.WINDOW_END)],
    indirect=["both_rules"],
)
def test_start_exactly_at_window_end(
    make_request, make_engineer, make_travel, both_rules, expected
):
    """Начало ровно в конце окна: по ТЗ допустимо, по Дополнениям — нет.

    Работа заканчивается за окном (13:00), но внутри смены — тот же случай.
    """
    task = make_request(windowEnd="12:00")
    travel = make_travel([task], legs={("office", "1"): (10_800, 5000)})  # 12:00
    assert schedule.first_violation(make_engineer(), [task], travel, START) == expected


@pytest.mark.parametrize(
    "both_rules", ["start_in_window", "fit_in_window"], indirect=True
)
def test_start_at_window_end_minus_duration(
    make_request, make_engineer, make_travel, both_rules
):
    """Начало ровно в «конец окна − длительность» допустимо в обеих трактовках."""
    task = make_request(windowEnd="12:00")
    travel = make_travel([task], legs={("office", "1"): (7200, 5000)})  # 11:00
    assert schedule.first_violation(make_engineer(), [task], travel, START) is None


# --- смена (PLAN 6.2, п. 3) --------------------------------------------------


def test_end_exactly_at_shift_end(make_request, make_engineer, make_travel):
    task = make_request()  # работа 60 минут, прибытие 09:10, окончание 10:10
    engineer = make_engineer(shiftEnd="10:10")
    assert (
        schedule.first_violation(engineer, [task], make_travel([task]), START) is None
    )


def test_end_one_second_after_shift_end(make_request, make_engineer, make_travel):
    task = make_request()
    travel = make_travel([task], legs={("office", "1"): (601, 5000)})
    engineer = make_engineer(shiftEnd="10:10")
    assert (
        schedule.first_violation(engineer, [task], travel, START) is Violation.SHIFT_END
    )


def test_second_request_does_not_fit_the_shift(
    make_request, make_engineer, make_travel
):
    tasks = [make_request("1"), make_request("2")]
    engineer = make_engineer(shiftEnd="11:00")  # вторая закончилась бы в 11:20
    assert (
        schedule.first_violation(engineer, tasks, make_travel(tasks), START)
        is Violation.SHIFT_END
    )


# --- лучшая вставка (PLAN 6.2, 6.6) ------------------------------------------


def test_best_insertion_picks_the_cheapest_place(
    make_request, make_engineer, make_travel
):
    route = [make_request("1"), make_request("2")]
    new = make_request("3", serviceDurationMin=30, windowEnd="18:00")
    travel = make_travel([*route, new], legs={("2", "3"): (600, 1000)})

    assert schedule.best_insertion(make_engineer(), route, new, travel, START) == (
        2,
        1000,
    )


def test_best_insertion_prefers_the_earlier_position_on_a_tie(
    make_request, make_engineer, make_travel
):
    """Все плечи одинаковы — прирост везде один и тот же, выбор обязан быть повторяемым."""
    route = [make_request("1"), make_request("2")]
    new = make_request("3", serviceDurationMin=30, windowEnd="18:00")
    travel = make_travel([*route, new])

    assert schedule.best_insertion(make_engineer(), route, new, travel, START) == (
        0,
        5000,
    )


def test_best_insertion_returns_nothing_when_it_does_not_fit(
    make_request, make_engineer, make_travel
):
    route = [make_request("1"), make_request("2")]
    new = make_request("3", windowStart="09:00", windowEnd="09:05")
    travel = make_travel([*route, new])

    assert schedule.best_insertion(make_engineer(), route, new, travel, START) is None


def test_best_insertion_checks_resources(make_request, make_engineer, make_travel):
    route = [make_request("1")]
    new = make_request("2", skill="emergency")
    travel = make_travel([*route, new])

    assert schedule.best_insertion(make_engineer(), route, new, travel, START) is None


# --- стартовая точка бригады (PLAN 2.4, блок 24) -----------------------------


def test_route_starts_from_the_engineer_home(make_request, make_engineer, make_travel):
    """Бригада из удалённого города выезжает из дома, а не из офиса (ответ 13)."""
    task = make_request("1", windowStart="09:00", windowEnd="18:00")
    engineer = make_engineer("brigade-1", startLat=54.84, startLon=38.19)
    home = f"start:{54.84:.6f},{38.19:.6f}"
    travel = make_travel(
        [task],
        engineers=[engineer],
        legs={(home, "1"): (1200, 20_000), (OFFICE, "1"): (7200, 90_000)},
    )

    (visit,) = schedule.build_schedule(
        engineer,
        [task],
        travel,
        Position(
            matrices.start_id(Office(address="офис", lat=55.70, lon=37.77), engineer),
            equipment_used=0,
            duration_mode=DurationMode.NORM,
            finished={},
            turns={},
            lunch_done=True,
            after_work=False,
        ),
    )

    assert visit.travel_m == 20_000  # из дома, а не 90 км из офиса
    assert visit.arrival == to_seconds("09:20")


def test_start_positions_are_built_for_every_engineer(
    make_request, make_engineer, make_dataset
):
    data = make_dataset(
        [make_request("1")],
        [
            make_engineer("brigade-1"),
            make_engineer("brigade-2", startLat=54.84, startLon=38.19),
        ],
    )

    positions = schedule.start_positions(data)

    assert positions["brigade-1"].point_id == OFFICE
    assert positions["brigade-2"].point_id == "start:54.840000,38.190000"
    assert positions["brigade-2"].available_from is None


# --- свободные окна бригады (PLAN 6.9, блок 28) ------------------------------


def test_free_engineer_is_idle_the_whole_shift(make_engineer):
    slots = schedule.idle_slots(make_engineer("brigade-1"), [], START)

    assert slots == [(to_seconds("09:00"), to_seconds("18:00"))]


def test_idle_slots_lie_between_the_stops(make_request, make_engineer, make_travel):
    requests = [
        make_request("1", windowStart="09:00", windowEnd="12:00"),
        make_request("2", windowStart="15:00", windowEnd="17:00"),
    ]
    engineer = make_engineer("brigade-1")
    visits = schedule.build_schedule(engineer, requests, make_travel(requests), START)

    slots = schedule.idle_slots(engineer, visits, START)

    # Работа идёт 09:10–10:10, потом бригада свободна до выезда в 14:50 (прибытие к 15:00),
    # после окончания в 16:00 — до конца смены.
    assert slots == [
        (to_seconds("10:10"), to_seconds("14:50")),
        (to_seconds("16:00"), to_seconds("18:00")),
    ]


def test_engineer_who_left_the_day_has_no_free_slots(make_engineer):
    engineer = make_engineer("brigade-1", unavailableFrom="13:00")
    position = schedule.free_from(
        Office(address="офис", lat=55.70, lon=37.77), engineer, DurationMode.NORM
    )

    assert schedule.idle_slots(engineer, [], position) == []


# --- вместимость оборудования (PLAN 6.2, блок 27) ----------------------------

ALL_DAY = {"windowEnd": "18:00"}


@pytest.mark.parametrize(
    ("transport", "count", "expected"),
    [
        ("public_transport", 2, None),
        ("public_transport", 3, Violation.NO_CAPACITY),
        ("bicycle", 3, None),
        ("bicycle", 4, Violation.NO_CAPACITY),
        ("car", 4, None),  # у автомобиля ограничения нет
    ],
)
def test_capacity_by_transport(
    make_request, make_engineer, make_travel, transport, count, expected
):
    tasks = [
        make_request(str(number), requiredEquipment=["router"], **ALL_DAY)
        for number in range(1, count + 1)
    ]
    engineer = make_engineer(transport=transport, equipment=["router"])

    assert (
        schedule.first_violation(engineer, tasks, make_travel(tasks), START) == expected
    )


def test_request_takes_one_unit_of_every_kind(make_request, make_engineer, make_travel):
    """Роутер с Алисой — две единицы: вместе с третьей заявкой пешему уже не унести."""
    tasks = [
        make_request("1", requiredEquipment=["router", "alice"], **ALL_DAY),
        make_request("2", requiredEquipment=["router"], **ALL_DAY),
    ]
    walker = make_engineer(transport="public_transport", equipment=["router", "alice"])

    assert (
        schedule.first_violation(walker, tasks, make_travel(tasks), START)
        is Violation.NO_CAPACITY
    )


def test_equipment_of_the_committed_part_is_already_spent(
    make_request, make_engineer, make_travel
):
    """Пополнения нет: после двух начатых работ у пешего остаток дня пуст (PLAN 6.12)."""
    task = make_request("1", requiredEquipment=["router"], **ALL_DAY)
    walker = make_engineer(transport="public_transport", equipment=["router"])
    spent = Position(
        OFFICE,
        None,
        equipment_used=2,
        duration_mode=DurationMode.NORM,
        finished={},
        turns={},
        lunch_done=True,
        after_work=False,
    )

    assert (
        schedule.first_violation(walker, [task], make_travel([task]), spent)
        is Violation.NO_CAPACITY
    )


def test_missing_kind_is_reported_before_capacity(
    make_request, make_engineer, make_travel
):
    """Вида нет вовсе — это NO_EQUIPMENT, а не «не унесёт»."""
    task = make_request(
        "1", requiredEquipment=["router", "set_top_box", "alice"], **ALL_DAY
    )
    walker = make_engineer(transport="public_transport", equipment=["router"])

    assert (
        schedule.first_violation(walker, [task], make_travel([task]), START)
        is Violation.NO_EQUIPMENT
    )


def test_tools_are_not_spent(make_request, make_engineer, make_travel):
    """Инструмент не расходуется: пешему с вместимостью 2 хватает одного ноутбука на
    три заявки, а в счёт вместимости идёт только оборудование (PLAN 5.1)."""
    tasks = [
        make_request(str(number), requiredTools=["laptop", "cable_tester"], **ALL_DAY)
        for number in (1, 2, 3)
    ]
    walker = make_engineer(
        transport="public_transport", tools=["laptop", "cable_tester"]
    )

    assert schedule.units(tasks) == 0
    assert schedule.first_violation(walker, tasks, make_travel(tasks), START) is None


# --- выезд на аварию и разворот (блок 74) ------------------------------------


def test_dispatch_departs_not_before_the_alarm(
    make_request, make_engineer, make_travel
):
    """Свободная с 09:00 бригада получила аварию в 11:00 — выезд в 11:00, а не в 09:00."""
    emergency = make_request("9", windowEnd="17:00")
    alarm = schedule.Turn(None, to_seconds("11:00"), 0)

    (visit,) = schedule.build_schedule(
        make_engineer(),
        [emergency],
        make_travel([emergency]),
        replace(START, turns={"9": alarm}),
    )

    assert (visit.departure, visit.arrival) == (
        to_seconds("11:00"),
        to_seconds("11:10"),
    )


def test_turn_keeps_the_departure_and_counts_the_driven_piece(
    make_request, make_engineer, make_travel
):
    """Выехали к следующей заявке в 10:10, в 10:15 развернули в точке, проехав 2,5 км: выезд
    прежний, прибытие — 10 минут от точки разворота, в переезде — и проеханный кусок."""
    first, emergency = make_request("1"), make_request("9", windowEnd="17:00")
    turn = schedule.Turn(OFFICE, to_seconds("10:15"), 2500)

    _, visit = schedule.build_schedule(
        make_engineer(),
        [first, emergency],
        make_travel([first, emergency]),
        replace(START, turns={"9": turn}),
    )

    assert (visit.departure, visit.arrival) == (
        to_seconds("10:10"),
        to_seconds("10:25"),
    )
    assert (visit.travel_sec, visit.travel_m) == (15 * 60, 7500)


# --- обед (блок 79) ------------------------------------------------------------------

HUNGRY = replace(START, lunch_done=False)
LUNCH = {"lunchStart": "13:00", "lunchEnd": "16:00"}


def at(clock: str) -> int:
    return to_seconds(clock)


def test_lunch_goes_into_the_wait_before_a_later_window(
    make_request, make_engineer, make_travel
):
    """Обед уходит в ожидание окна: начало следующей работы не сдвигается."""
    tasks = [
        make_request("1"),
        make_request("2", windowStart="15:00", windowEnd="16:00"),
    ]
    first, second = schedule.build_schedule(
        make_engineer(**LUNCH), tasks, make_travel(tasks), HUNGRY
    )

    assert first.lunch is None
    assert second.lunch == (at("13:00"), at("14:00"))
    assert (second.departure, second.start) == (at("14:00"), at("15:00"))


def test_lunch_waits_for_the_last_chance_and_then_delays_the_work(
    make_request, make_engineer, make_travel
):
    """После второй работы час в окно уже не встанет — обед перед ней, работа сдвигается."""
    tasks = [
        make_request("1", serviceDurationMin=240, windowEnd="13:00"),
        make_request(
            "2", serviceDurationMin=240, windowStart="13:00", windowEnd="18:00"
        ),
    ]
    engineer = make_engineer(**LUNCH, shiftEnd="22:00")
    first, second = schedule.build_schedule(engineer, tasks, make_travel(tasks), HUNGRY)

    assert first.end == at("13:10")
    assert second.lunch == (at("13:10"), at("14:10"))
    assert second.start == at("14:20")


def test_lunch_can_push_the_next_work_out_of_its_window(
    make_request, make_engineer, make_travel
):
    """Обед обязателен между работами (блок 82): после второй работы час в окно уже не
    встанет, обед ставится перед ней и выталкивает её за окно — её этой бригаде не дают."""
    tasks = [
        make_request("1", serviceDurationMin=230, windowEnd="10:00"),
        make_request(
            "2", serviceDurationMin=200, windowStart="13:00", windowEnd="13:30"
        ),
    ]
    travel = make_travel(tasks)

    assert schedule.first_violation(make_engineer(**LUNCH), tasks, travel, HUNGRY) is (
        Violation.WINDOW_END
    )
    assert schedule.first_violation(make_engineer(), tasks, travel, HUNGRY) is None


def test_no_lunch_with_a_single_work(make_request, make_engineer, make_travel):
    """Обед бывает только между работами (блок 82): ни перед первой, ни после последней.
    Работа через всё окно обеда без второй работы допустима, простой после неё — простой."""
    long = make_request("1", serviceDurationMin=480, windowEnd="10:00")
    engineer = make_engineer(**LUNCH)
    assert (
        schedule.first_violation(engineer, [long], make_travel([long]), HUNGRY) is None
    )

    task = make_request("1")
    visits = schedule.build_schedule(engineer, [task], make_travel([task]), HUNGRY)
    assert schedule.lunch_slot(visits) is None
    assert schedule.idle_slots(engineer, visits, HUNGRY) == [(at("10:10"), at("18:00"))]


def test_lunch_is_not_due_for_who_comes_after_the_window(make_request, make_engineer):
    engineer = make_engineer(**LUNCH, shiftStart="15:30", shiftEnd="22:00")

    assert not schedule.lunch_due(engineer, at("15:30"))
    assert schedule.lunch_due(make_engineer(**LUNCH), at("09:00"))


def test_no_lunch_before_the_first_work(make_request, make_engineer, make_travel):
    """Первая работа после окна: обедать до неё нельзя, даже если бригада ждёт."""
    tasks = [
        make_request("1", windowStart="15:00", windowEnd="16:00"),
        make_request("2", windowStart="17:00", windowEnd="17:30"),
    ]
    first, second = schedule.build_schedule(
        make_engineer(**LUNCH), tasks, make_travel(tasks), HUNGRY
    )

    assert (first.lunch, second.lunch) == (None, None)
    assert first.start == at("15:00")


def test_no_lunch_when_the_work_ends_too_close_to_the_window_end(
    make_request, make_engineer, make_travel
):
    """Работа кончилась в 15:10 — часа до 16:00 уже нет, обеда нет."""
    tasks = [
        make_request("1", serviceDurationMin=360, windowEnd="10:00"),
        make_request("2", windowStart="15:00", windowEnd="17:00"),
    ]
    first, second = schedule.build_schedule(
        make_engineer(**LUNCH, shiftEnd="20:00"), tasks, make_travel(tasks), HUNGRY
    )

    assert first.end == at("15:10")
    assert second.lunch is None


def test_lunch_after_the_last_work_counts_only_for_the_continuation(
    make_request, make_engineer, make_travel
):
    """Час сразу после последней работы — обед, начатый в простое до события; выбывшая
    бригада после выбытия не обедает."""
    task = make_request("1")
    engineer = make_engineer(**LUNCH)
    visits = schedule.build_schedule(engineer, [task], make_travel([task]), HUNGRY)

    assert schedule.lunch_after(engineer, visits) == (at("13:00"), at("14:00"))
    left = make_engineer(**LUNCH, unavailableFrom="13:30")
    assert schedule.lunch_after(left, visits) is None
    assert schedule.lunch_after(engineer, []) is None


def test_free_crew_eats_while_waiting_for_the_emergency(
    make_request, make_engineer, make_travel
):
    """Свободная бригада поедет на аварию в 14:10 — час окна до неё и есть обед."""
    tasks = [
        make_request("1"),
        make_request("9", windowStart="14:10", windowEnd="15:00"),
    ]
    position = replace(HUNGRY, turns={"9": schedule.Turn(None, at("14:10"), 0)})
    _, emergency = schedule.build_schedule(
        make_engineer(**LUNCH), tasks, make_travel(tasks), position
    )

    assert emergency.lunch == (at("13:00"), at("14:00"))
    assert emergency.departure == at("14:10")


def test_lunch_fits_a_shift_that_ends_inside_the_window(
    make_request, make_engineer, make_travel
):
    """Смена до 15:00: после работы в 14:00 часа до конца смены нет — обед перед ней."""
    tasks = [
        make_request("1"),
        make_request(
            "2", serviceDurationMin=20, windowStart="14:00", windowEnd="14:30"
        ),
    ]
    engineer = make_engineer(**LUNCH, shiftEnd="15:00")
    _, visit = schedule.build_schedule(engineer, tasks, make_travel(tasks), HUNGRY)

    assert visit.lunch == (at("13:00"), at("14:00"))
    assert visit.start == at("14:10")


def test_free_crew_without_work_does_not_eat_while_waiting(
    make_request, make_engineer, make_travel
):
    """Бригада без работ ждёт аварию: обеда до первой работы нет."""
    task = make_request("9", windowStart="14:10", windowEnd="15:00")
    position = replace(HUNGRY, turns={"9": schedule.Turn(None, at("14:10"), 0)})
    (emergency,) = schedule.build_schedule(
        make_engineer(**LUNCH), [task], make_travel([task]), position
    )

    assert emergency.lunch is None


def test_continuation_after_a_work_may_eat_before_its_first_stop(
    make_request, make_engineer, make_travel
):
    """Точка продолжения у законченной работы: обед перед первым стопом остатка дня."""
    task = make_request("2", windowStart="15:00", windowEnd="16:00")
    position = replace(HUNGRY, available_from=at("12:30"), after_work=True)
    (visit,) = schedule.build_schedule(
        make_engineer(**LUNCH), [task], make_travel([task]), position
    )

    assert visit.lunch == (at("13:00"), at("14:00"))
