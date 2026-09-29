"""Причины неназначения, объяснения и текст маршрута (PLAN 6.8, 6.9, блок 9.6).

Каждый код причины проверяется отдельно: «есть текст» ничего не значит, значение имеет
именно код, который увидит диспетчер. Ни Mongo, ни Valhalla тестам не нужны — таблицы
переездов собираются фикстурой `make_travel`.
"""

import pytest

from app.config import settings
from app.data import seed
from app.models import Algorithm, Plan
from app.planning import service
from app.routing import matrices
from tests.conftest import OFFICE_POINT


def plan_of(make_dataset, make_travel, requests, engineers, **travel_options):
    dataset = make_dataset(requests, engineers)
    return service.build_plan(dataset, make_travel(requests, **travel_options))


def reason(plan, request_id: str):
    return next(item for item in plan.unassigned if item.request_id == request_id)


# --- коды причин (PLAN 6.8) --------------------------------------------------


def test_no_skill_when_nobody_has_it(
    make_request, make_engineer, make_dataset, make_travel
):
    plan = plan_of(
        make_dataset,
        make_travel,
        [make_request("1", skill="emergency")],
        [make_engineer("brigade-1")],
    )

    item = reason(plan, "1")
    assert item.reason_code == "NO_SKILL"
    assert "Аварийные работы" in item.reason_text
    assert item.defer_next_day


def test_no_transport_when_the_skilled_brigade_rides_another(
    make_request, make_engineer, make_dataset, make_travel
):
    plan = plan_of(
        make_dataset,
        make_travel,
        [make_request("1", requiredTransport="car")],
        [make_engineer("brigade-1", transport="bicycle")],
    )

    item = reason(plan, "1")
    assert item.reason_code == "NO_TRANSPORT"
    assert "Автомобиль" in item.reason_text


def test_no_equipment_names_exactly_what_is_missing(
    make_request, make_engineer, make_dataset, make_travel
):
    """Каскад PLAN 6.8: бригада без навыка до оборудования не доходит, код — по самой дальней."""
    plan = plan_of(
        make_dataset,
        make_travel,
        [make_request("1", requiredEquipment=["set_top_box", "router"])],
        [
            make_engineer("brigade-1", skills=["emergency"]),
            make_engineer("brigade-2", equipment=["router"]),
        ],
    )

    item = reason(plan, "1")
    assert item.reason_code == "NO_EQUIPMENT"
    assert "Приставка" in item.reason_text
    assert "Роутер" not in item.reason_text  # роутер у бригады-2 есть


def test_no_route_when_there_is_no_way_to_the_address(
    make_request, make_engineer, make_dataset, make_travel
):
    requests = [make_request("1")]
    plan = plan_of(
        make_dataset,
        make_travel,
        requests,
        [make_engineer("brigade-1", transport="bicycle")],
        legs={("office", "1"): None},
    )

    item = reason(plan, "1")
    assert item.reason_code == "NO_ROUTE"
    assert "Велосипед" in item.reason_text


def test_no_time_when_the_window_closes_before_arrival(
    make_request, make_engineer, make_dataset, make_travel
):
    """Смена с 09:00, переезд 10 минут — в окно 08:00–08:30 не попасть никак."""
    plan = plan_of(
        make_dataset,
        make_travel,
        [make_request("1", windowStart="08:00", windowEnd="08:30")],
        [make_engineer("brigade-1")],
    )

    item = reason(plan, "1")
    assert item.reason_code == "NO_TIME"
    assert "08:00–08:30" in item.reason_text
    assert "прибудет в 09:10" in item.reason_text


def test_no_time_when_the_work_runs_past_the_shift(
    make_request, make_engineer, make_dataset, make_travel
):
    """Окно открыто до 18:00, но работа на час заканчивается после смены — это тоже NO_TIME."""
    plan = plan_of(
        make_dataset,
        make_travel,
        [make_request("1", windowStart="17:30", windowEnd="18:00")],
        [make_engineer("brigade-1")],
    )

    item = reason(plan, "1")
    assert item.reason_code == "NO_TIME"
    assert "— окончание работы в 18:30" in item.reason_text
    assert "при смене до 18:00" in item.reason_text


def test_not_fitted_names_the_brigades_that_could_have_taken_it(
    make_request, make_engineer, make_dataset, make_travel
):
    """Три заявки в одном окне на одну бригаду: третья влезла бы в пустой маршрут, но не в этот."""
    requests = [
        make_request("1", windowStart="09:00", windowEnd="18:00"),
        make_request("2", windowStart="09:00", windowEnd="18:00"),
        make_request("3", windowStart="09:00", windowEnd="12:00"),
    ]
    plan = plan_of(
        make_dataset,
        make_travel,
        requests,
        [make_engineer("brigade-1", shiftEnd="12:00")],
    )

    item = reason(plan, "3")
    assert item.reason_code == "NOT_FITTED"
    assert "Бригада 1 (на заявке №2 до 11:20)" in item.reason_text


def test_every_unassigned_request_is_deferred_to_the_next_day(
    make_request, make_engineer, make_dataset, make_travel
):
    plan = plan_of(
        make_dataset,
        make_travel,
        [make_request("1", skill="emergency"), make_request("2", skill="connection")],
        [make_engineer("brigade-1")],
    )

    assert len(plan.unassigned) == 2
    assert all(item.defer_next_day for item in plan.unassigned)


# --- кандидаты и текст объяснения (PLAN 6.9) ---------------------------------


@pytest.fixture
def explained(make_request, make_engineer, make_dataset, make_travel):
    """Заявка «1» достаётся бригаде 1; остальные бригады не подходят каждая по-своему."""
    requests = [make_request("1", requiredEquipment=["router"])]
    engineers = [
        make_engineer("brigade-1", equipment=["router"]),
        make_engineer("brigade-2", skills=["emergency"]),
        make_engineer("brigade-3"),
        make_engineer("brigade-4", equipment=["router"], shiftEnd="09:30"),
    ]
    return plan_of(make_dataset, make_travel, requests, engineers)


def test_candidates_cover_every_brigade_with_its_own_verdict(explained):
    candidates = {
        item.engineer_id: item for item in explained.explanations["1"].candidates
    }

    assert list(candidates) == ["brigade-1", "brigade-2", "brigade-3", "brigade-4"]
    assert (candidates["brigade-1"].skill_ok, candidates["brigade-1"].time_ok) == (
        True,
        True,
    )
    # Прирост пробега назначенной бригады — её фактическая цена: 5 км туда (PLAN 6.9).
    assert candidates["brigade-1"].extra_km == 5.0
    assert not candidates["brigade-2"].skill_ok
    assert not candidates["brigade-3"].equipment_ok
    # Смена до 09:30: по ресурсу бригада подходит, по времени — нет.
    assert candidates["brigade-4"].equipment_ok
    assert not candidates["brigade-4"].time_ok
    assert candidates["brigade-4"].extra_km is None


def test_assignment_text_names_the_norm_the_road_and_the_window(explained):
    text = explained.explanations["1"].text

    assert text.startswith("Заявка №1 (обычная) назначена: Бригада 1.")
    assert "навык «Локальные работы» есть" in text
    assert "нужно оборудование «Роутер» — есть" in text
    assert "транспорт не требуется" in text
    assert "переезд 10 мин, прибытие 09:10, начало 09:10 в окне 09:00–12:00" in text
    assert "норматив 80 мин = 20 дорога + 60 работа, фактическая дорога 10 мин" in text
    assert "окончание 10:10 при смене до 18:00" in text
    assert "прирост пробега +5,0 км" in text


def test_assignment_text_compares_the_other_brigades(explained):
    text = explained.explanations["1"].text.split("Другие инженеры: ")[1]

    assert "Бригада 2 — нет навыка «Локальные работы»" in text
    assert "Бригада 3 — нет оборудования «Роутер»" in text
    assert "Бригада 4 — не помещается в день (смена до 09:30)" in text
    assert "Бригада 1" not in text  # назначенная бригада уже описана выше


def test_assignment_text_shows_waiting_before_the_window(
    make_request, make_engineer, make_dataset, make_travel
):
    plan = plan_of(
        make_dataset,
        make_travel,
        [make_request("1", windowStart="11:00", windowEnd="13:00")],
        [make_engineer("brigade-1")],
    )

    assert "ожидание до 11:00, начало в окне 11:00–13:00" in plan.explanations["1"].text


def test_unassigned_request_gets_the_same_table_and_the_same_reason(
    make_request, make_engineer, make_dataset, make_travel
):
    plan = plan_of(
        make_dataset,
        make_travel,
        [make_request("1", requiredTransport="car")],
        [
            make_engineer("brigade-1", transport="bicycle"),
            make_engineer("brigade-2", transport="public_transport"),
        ],
    )

    explanation = plan.explanations["1"]
    assert [item.engineer_id for item in explanation.candidates] == [
        "brigade-1",
        "brigade-2",
    ]
    assert not explanation.candidates[0].transport_ok
    assert explanation.text.startswith(
        "Заявка №1 (обычная) не назначена: нужен транспорт"
    )
    # Причина в объяснении и в `unassigned` — одна строка, разъехаться они не могут.
    assert reason(plan, "1").reason_text[1:] in explanation.text
    assert "Бригада 1 — транспорт «Велосипед», нужен «Автомобиль»" in explanation.text


# --- текст маршрута и перестановки (PLAN 6.9) --------------------------------


def test_route_text_reports_a_swap_that_breaks_a_window(
    make_request, make_engineer, make_dataset, make_travel
):
    """Окно заявки «1» закрывается в 09:30: второй в маршруте она уже не может стоять."""
    requests = [
        make_request("1", windowStart="09:00", windowEnd="09:30"),
        make_request("2", windowStart="09:00", windowEnd="12:00"),
    ]
    plan = plan_of(make_dataset, make_travel, requests, [make_engineer("brigade-1")])

    text = plan.route_explanations["brigade-1"].order
    assert text.startswith(
        "Бригада 1: 2 заявки, 10,0 км, окончание 11:20 при смене до 18:00."
    )
    assert "№1 ↔ №2 — нарушит окно заявки" in text


def test_route_text_reports_the_kilometres_a_swap_would_add(
    make_request, make_engineer, make_dataset, make_travel
):
    """Плечи подобраны так, что обратный порядок длиннее на 2 км."""
    requests = [make_request("1"), make_request("2")]
    plan = plan_of(
        make_dataset,
        make_travel,
        requests,
        [make_engineer("brigade-1")],
        legs={("office", "2"): (600, 7000)},
    )

    assert "№1 ↔ №2 — +2,0 км" in plan.route_explanations["brigade-1"].order


def test_route_text_of_an_idle_brigade_says_so(
    make_request, make_engineer, make_dataset, make_travel
):
    plan = plan_of(
        make_dataset,
        make_travel,
        [make_request("1")],
        [make_engineer("brigade-1"), make_engineer("brigade-2")],
    )

    assert (
        plan.route_explanations["brigade-2"].order
        == "Бригада 2: заявок нет, инженер не задействован."
    )
    assert (
        "Перестановка" not in plan.route_explanations["brigade-1"].order
    )  # одна заявка


# --- почему эти заявки у этой бригады (PLAN 6.9, блок 29.4) ------------------


def test_assignment_explains_what_the_brigade_can_do_and_took(
    make_request, make_engineer, make_dataset, make_travel
):
    requests = [
        make_request("1", workPriority="new_connection", skill="connection"),
        make_request("2", windowEnd="18:00"),
    ]
    engineer = make_engineer(
        skills=["local", "connection"], equipment=["router"], tools=["laptop"]
    )
    plan = plan_of(make_dataset, make_travel, requests, [engineer])

    text = plan.route_explanations["brigade-1"].assignment
    assert text.startswith(
        "Бригада 1: навыки «Локальные работы», «Работы на подключение и дозаказы»; "
        "транспорт «Автомобиль»; оборудование «Роутер»; инструменты «Ноутбук». "
    )
    assert (
        "По навыкам открыты локальные заявки, подключения и дозаказы; "
        "закрыты — аварии; дальше решают транспорт, оборудование и инструменты, "
        "которые требует заявка." in text
    )
    # 2 × (10 мин переезда + 60 мин работы) = 140 мин из 540 — 26 % смены.
    assert (
        "В маршруте 2 заявки (новое подключение — 1, обычная — 1), 10,0 км; "
        "переезды и работы занимают 26 % смены 09:00–18:00." in text
    )


def test_assignment_of_an_idle_brigade_names_its_resources(
    make_request, make_engineer, make_dataset, make_travel
):
    engineer = make_engineer(skills=["emergency"], transport="bicycle")
    plan = plan_of(make_dataset, make_travel, [make_request("1")], [engineer])

    text = plan.route_explanations["brigade-1"].assignment
    assert "По навыкам открыты аварии; закрыты — локальные заявки" in text
    assert "оборудование нет; инструменты нет" in text
    assert text.endswith("Заявок нет — инженер не задействован.")


def test_assignment_of_a_gone_brigade_says_since_when(
    make_request, make_engineer, make_dataset, make_travel
):
    engineer = make_engineer(unavailableFrom="13:00")
    plan = plan_of(make_dataset, make_travel, [make_request("1")], [engineer])

    assert "С 13:00 не работает — новых заявок не берёт." in (
        plan.route_explanations["brigade-1"].assignment
    )


def test_route_explanation_stored_as_a_string_still_reads():
    """План до блока 29 хранит строку «почему такой порядок» — он обязан открываться."""
    explanation = Plan.model_validate(
        {
            "id": "old",
            "datasetId": "builtin",
            "algorithm": "baseline",
            "input": {"office": OFFICE_POINT, "requests": [], "engineers": []},
            "routeExplanations": {"brigade-1": "Бригада 1: заявок нет."},
            "createdAt": "2026-09-20T10:00:00Z",
        }
    ).route_explanations["brigade-1"]

    assert explanation.order == "Бригада 1: заявок нет."
    assert explanation.assignment == ""


# --- встроенный набор --------------------------------------------------------


@pytest.fixture
def builtin():
    dataset = seed.load_builtin()
    documents = seed.load_matrices()
    assert documents, (
        "нет data/seed/matrices.json — запустите prepare_data.py --matrices"
    )
    travel = matrices.Travel(
        dataset.id,
        matrices.points(dataset),
        {
            document["profile"]: matrices.from_document(document)
            for document in documents
        },
    )
    return service.build_plan(dataset, travel, Algorithm.BASELINE)


def test_builtin_plan_explains_every_request(builtin):
    """PLAN 5.2: объяснение у каждой заявки набора, причина — у каждой неназначенной."""
    codes = {
        "NO_SKILL",
        "NO_TRANSPORT",
        "NO_EQUIPMENT",
        "NO_ROUTE",
        "NO_TIME",
        "NOT_FITTED",
    }

    assert set(builtin.explanations) == {
        request.id for request in builtin.input.requests
    }
    assert all(item.reason_code in codes for item in builtin.unassigned)
    assert all(
        item.assignment and item.order for item in builtin.route_explanations.values()
    )
    assert set(builtin.route_explanations) == {
        engineer.id for engineer in builtin.input.engineers
    }


def test_builtin_not_fitted_always_names_a_brigade(builtin):
    names = [engineer.name for engineer in builtin.input.engineers]
    for item in builtin.unassigned:
        if item.reason_code == "NOT_FITTED":
            assert any(f"Подходят: {name} (" in item.reason_text for name in names)


# --- вырожденные наборы и непроезжие плечи (ревью блока 9) -------------------


def test_reason_survives_work_that_would_end_after_midnight(
    make_request, make_engineer, make_dataset, make_travel
):
    """Окно до 23:59 и час работы: окончание уходит за сутки, а причину показать надо."""
    plan = plan_of(
        make_dataset,
        make_travel,
        [make_request("1", windowStart="23:00", windowEnd="23:59")],
        [make_engineer("brigade-1", shiftStart="13:00", shiftEnd="22:00")],
    )

    item = reason(plan, "1")
    assert item.reason_code == "NO_TIME"
    assert "— окончание работы в 24:00" in item.reason_text


def test_reason_survives_a_trip_longer_than_the_day(
    make_request, make_engineer, make_dataset, make_travel
):
    """Запасной расчёт по прямой на дальнем адресе даёт сутки пути (PLAN 3.4)."""
    plan = plan_of(
        make_dataset,
        make_travel,
        [make_request("1")],
        [make_engineer("brigade-1", transport="public_transport")],
        leg=(22 * 3600, 110_000),
    )

    item = reason(plan, "1")
    assert item.reason_code == "NO_TIME"
    assert "прибудет в 31:00" in item.reason_text


def test_explanation_survives_a_route_that_breaks_without_this_request(
    make_request, make_engineer, make_dataset, make_travel
):
    """Снятие среднего стопа открывает плечо, которого в матрице нет (PLAN 3.4).

    Считать прирост тогда не от чего — но объяснение обязано быть, а план не имеет права
    упасть целиком из-за одного необъяснимого числа.
    """
    requests = [
        make_request("1", windowEnd="18:00"),
        make_request("2", windowEnd="18:00"),
        make_request("3", windowEnd="18:00"),
    ]
    plan = plan_of(
        make_dataset,
        make_travel,
        requests,
        [make_engineer("brigade-1")],
        legs={("1", "3"): None},
    )

    explanation = plan.explanations["2"]
    assert explanation.candidates[0].time_ok  # заявка стоит в маршруте, это факт плана
    assert explanation.candidates[0].extra_km is None
    assert "прирост пробега" not in explanation.text


def test_assigned_extra_km_is_the_real_cost_not_the_cheapest_slot(
    make_request, make_engineer, make_dataset, make_travel
):
    """Заявку «2» дешевле было бы поставить первой, но план поставил её второй.

    Показываем цену того места, где стоп стоит на самом деле: `best_insertion` вернул бы
    минимум по всем позициям, и «прирост» разошёлся бы с километрами маршрута.
    """
    requests = [
        make_request("1", windowEnd="18:00"),
        make_request("2", windowEnd="18:00"),
    ]
    plan = plan_of(
        make_dataset,
        make_travel,
        requests,
        [make_engineer("brigade-1")],
        legs={("office", "2"): (600, 1000), ("2", "1"): (600, 1000)},
    )

    candidate = plan.explanations["2"].candidates[0]
    assert plan.routes[0].km == 10.0  # office -> 1 -> 2, по 5 км
    assert (
        candidate.extra_km == 5.0
    )  # столько добавил второй стоп, а не −3,0 лучшей вставки
    assert "прирост пробега +5,0 км" in plan.explanations["2"].text


def test_equipment_reason_names_everything_the_brigades_lack(
    make_request, make_engineer, make_dataset, make_travel
):
    """У одной бригады нет роутера, у другой Алисы: полного набора нет ни у кого."""
    plan = plan_of(
        make_dataset,
        make_travel,
        [make_request("1", requiredEquipment=["router", "alice"])],
        [
            make_engineer("brigade-1", equipment=["alice"]),
            make_engineer("brigade-2", equipment=["router"]),
        ],
    )

    item = reason(plan, "1")
    assert item.reason_code == "NO_EQUIPMENT"
    assert "«Роутер», «Алиса»" in item.reason_text


def test_missing_tool_is_named_as_a_tool(
    make_request, make_engineer, make_dataset, make_travel
):
    """Код тот же, NO_EQUIPMENT, но текст говорит, чего нет — инструмента, а не
    оборудования (PLAN 6.8, блок 27.8)."""
    plan = plan_of(
        make_dataset,
        make_travel,
        [make_request("1", requiredEquipment=["router"], requiredTools=["laptop"])],
        [
            make_engineer("brigade-1", equipment=["router"], tools=["cable_tester"]),
            make_engineer("brigade-2", tools=["laptop"]),
        ],
    )

    item = reason(plan, "1")
    assert item.reason_code == "NO_EQUIPMENT"
    assert item.reason_text == (
        "Не хватает оборудования «Роутер» и инструмента «Ноутбук» — "
        "такого набора нет ни у одного подходящего инженера"
    )
    text = plan.explanations["1"].text
    assert "Бригада 1 — нет инструмента «Ноутбук»" in text
    assert "Бригада 2 — нет оборудования «Роутер»" in text
    assert not any(item.equipment_ok for item in plan.explanations["1"].candidates)


def test_assignment_text_names_the_tools(
    make_request, make_engineer, make_dataset, make_travel
):
    plan = plan_of(
        make_dataset,
        make_travel,
        [make_request("1", requiredTools=["cable_tester"])],
        [make_engineer(tools=["cable_tester"])],
    )

    assert plan.assignments == {"1": "brigade-1"}
    assert (
        "оборудование не требуется; нужен инструмент «Кабельный тестер» — есть"
        in plan.explanations["1"].text
    )

    two = plan_of(
        make_dataset,
        make_travel,
        [make_request("1", requiredTools=["cable_tester", "laptop"])],
        [make_engineer(tools=["cable_tester", "laptop"])],
    )
    assert (
        "нужны инструменты «Кабельный тестер», «Ноутбук» — есть"
        in two.explanations["1"].text
    )


def test_a_dataset_without_brigades_leaves_every_request_unassigned(
    make_request, make_dataset, make_travel
):
    """Набор без бригад — вход, который блок 12 примет: причина есть у каждой заявки."""
    plan = plan_of(
        make_dataset, make_travel, [make_request("1"), make_request("2")], []
    )

    assert plan.validation.ok
    assert [item.reason_code for item in plan.unassigned] == ["NO_SKILL", "NO_SKILL"]
    assert plan.explanations["1"].candidates == []


# --- оповещение о расчёте по прямой (PLAN 5.6) -------------------------------


def test_note_names_the_transports_counted_by_straight_line(
    make_request, make_engineer, make_dataset, make_travel
):
    """Отказал один профиль — оповещение называет транспорт, а не «pedestrian».

    Общественный транспорт выводится из пешехода (PLAN 3.3), поэтому отказ пешеходного
    профиля тянет за собой и его: в тексте должен стоять он, а не имя профиля.
    """
    requests = [make_request("1")]
    engineers = [
        make_engineer("brigade-1", transport="car"),
        make_engineer("brigade-2", transport="bicycle"),
        make_engineer("brigade-3", transport="public_transport"),
    ]
    travel = make_travel(requests)
    travel.approximate_profiles |= {"pedestrian", "public_transport"}

    plan = service.build_plan(make_dataset(requests, engineers), travel)

    assert plan.approximate
    assert plan.approximate_note == (
        "Valhalla не ответила, расстояния посчитаны по прямой (× 1.3 и городская "
        "скорость): у инженеров с транспортом «Пешеход / общественный транспорт» "
        "километры и времена приблизительные, у остальных — по дорогам."
    )


def test_note_says_the_whole_plan_when_every_transport_is_struck(
    make_request, make_engineer, make_dataset, make_travel
):
    """Все бригады на одном профиле — перечислять их незачем, приблизителен весь план."""
    requests = [make_request("1")]
    engineers = [make_engineer("brigade-1", transport="bicycle")]
    travel = make_travel(requests)
    travel.approximate_profiles.add("bicycle")

    plan = service.build_plan(make_dataset(requests, engineers), travel)

    assert "километры и времена всего плана приблизительные" in plan.approximate_note


def test_candidate_shows_when_the_engineer_is_free(
    make_request, make_engineer, make_dataset, make_travel
):
    """Свободные окна бригады в таблице кандидатов (PLAN 6.9, блок 28)."""
    plan = plan_of(
        make_dataset,
        make_travel,
        [make_request("1", windowStart="09:00", windowEnd="12:00")],
        [make_engineer("brigade-1"), make_engineer("brigade-2")],
    )

    busy, free = plan.explanations["1"].candidates
    # Занятая бригада работает 09:10–10:10, свободна с 10:10 до конца смены.
    assert [(slot.start, slot.end) for slot in busy.free_slots] == [("10:10", "18:00")]
    # У второй бригады заявок нет — свободна вся смена.
    assert [(slot.start, slot.end) for slot in free.free_slots] == [("09:00", "18:00")]


# --- вместимость оборудования (PLAN 6.8, 6.9, блок 27) -----------------------


def test_no_capacity_when_the_request_needs_more_than_anyone_carries(
    make_request, make_engineer, make_dataset, make_travel
):
    plan = plan_of(
        make_dataset,
        make_travel,
        [make_request("1", requiredEquipment=["router", "set_top_box", "alice"])],
        [
            make_engineer(
                "brigade-1",
                transport="public_transport",
                equipment=["router", "set_top_box", "alice"],
            )
        ],
    )

    item = reason(plan, "1")
    assert item.reason_code == "NO_CAPACITY"
    assert item.reason_text == (
        "Нужно 3 ед. оборудования — инженеры с транспортом "
        "«Пешеход / общественный транспорт» на день унесут не больше 2"
    )


def test_full_backpack_is_an_equipment_verdict_not_a_time_one(
    make_request, make_engineer, make_dataset, make_travel, monkeypatch
):
    """Пешему с местом на одну единицу вторая заявка не достаётся — и сказано почему."""
    monkeypatch.setattr(settings, "equipment_capacity", {"public_transport": 1})
    requests = [
        make_request(str(number), requiredEquipment=["router"], windowEnd="18:00")
        for number in (1, 2)
    ]
    plan = plan_of(
        make_dataset,
        make_travel,
        requests,
        [
            make_engineer(
                "brigade-1", transport="public_transport", equipment=["router"]
            ),
            make_engineer("brigade-2", equipment=["router"]),
        ],
    )

    assert plan.assignments == {"1": "brigade-1", "2": "brigade-2"}
    walker = next(
        item
        for item in plan.explanations["2"].candidates
        if item.engineer_id == "brigade-1"
    )
    assert not walker.equipment_ok
    assert "Бригада 1 — не унесёт: за день берёт не больше 1 ед." in (
        plan.explanations["2"].text
    )
    assert "Оборудование: 1 из 1 ед. на день." in str(
        plan.route_explanations["brigade-1"].order
    )
