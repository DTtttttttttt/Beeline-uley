"""«Что поможет» неназначенной заявке (PLAN 6.8, блок 55).

Каждое средство — на ручном примере с очевидным ответом: какое окно, какой бригаде, до
скольки продлить смену. И отдельно — что средство, которое не помогает, не предлагается:
проверка у всех одна, `schedule.best_insertion`, и её ответ «не встаёт» обязан доезжать сюда.
"""

from app.data import seed
from app.models import Input
from app.planning import explain, service
from app.planning.schedule import start_positions
from app.routing import matrices
from tests.conftest import OFFICE_POINT


def remedies_of(requests, engineers, routes, travel, pool=(), joined=None):
    """Средства по готовым маршрутам: неназначенные — всё, что не стоит в маршрутах."""
    data = Input(office=OFFICE_POINT, requests=requests, engineers=engineers)
    placed = {request.id for route in routes.values() for request in route}
    routes = {engineer.id: routes.get(engineer.id, []) for engineer in engineers}
    return explain.remedies(
        data,
        travel,
        routes,
        [request for request in requests if request.id not in placed],
        start_positions(data),
        list(pool),
        joined,
    )


def kinds(items) -> list[str]:
    return [item.kind for item in items]


# --- другое окно (п. 43) -----------------------------------------------------


def test_window_is_the_nearest_one_that_fits(make_request, make_engineer, make_travel):
    """Смена с 09:00, переезд 10 мин: в 08:00–08:30 не успеть, ближайшее окно той же длины
    внутри смен — 09:00–09:30 (08:30–09:00 начинается до смены). Продлить смену или взять
    бригаду 10:00–22:00 раннему окну не помогут — их и нет."""
    request = make_request("x", windowStart="08:00", windowEnd="08:30")
    items = remedies_of(
        [request], [make_engineer("brigade-1")], {}, make_travel([request])
    )["x"]

    assert kinds(items) == ["window"]
    window = items[0]
    assert (window.window_start, window.window_end) == ("09:00", "09:30")
    assert window.engineer_id == "brigade-1"
    assert window.text == (
        "Окно 09:00–09:30: возьмёт Бригада 1, +5,0 км, "
        "остальные заявки останутся в своих окнах"
    )


def test_window_does_not_push_other_stops_out_of_their_windows(
    make_request, make_engineer, make_travel
):
    """Маршрут a (09:00–09:10) → b (10:00–10:30). Окно 10:00–10:30 пустому маршруту подошло
    бы, но вытолкнуло бы b за его окно; первое честное — 11:00–11:30, после b."""
    a = make_request("a", windowStart="09:00", windowEnd="09:10")
    b = make_request("b", windowStart="10:00", windowEnd="10:30")
    x = make_request("x", windowStart="09:00", windowEnd="09:30")
    items = remedies_of(
        [a, b, x],
        [make_engineer("brigade-1")],
        {"brigade-1": [a, b]},
        make_travel([a, b, x]),
    )["x"]

    window = next(item for item in items if item.kind == "window")
    assert (window.window_start, window.window_end) == ("11:00", "11:30")


def test_request_that_already_fits_gets_no_remedies(
    make_request, make_engineer, make_travel
):
    """Базовый вариант ТЗ в середину не вставляет: такая заявка встаёт как есть, и советовать
    ей нечего — её ставит «Переназначить»."""
    request = make_request("x")
    items = remedies_of(
        [request], [make_engineer("brigade-1")], {}, make_travel([request])
    )

    assert items == {"x": []}


# --- оборудование и транспорт ------------------------------------------------


def test_missing_equipment_is_offered_to_the_brigade_that_lacks_it(
    make_request, make_engineer, make_travel
):
    request = make_request("x", requiredEquipment=["router"], requiredTools=["laptop"])
    items = remedies_of(
        [request],
        [make_engineer("brigade-1", tools=["laptop"])],
        {},
        make_travel([request]),
    )["x"]

    equipment = next(item for item in items if item.kind == "equipment")
    assert equipment.engineer_id == "brigade-1"
    assert equipment.text == "Бригада 1 возьмёт, если выдать «Роутер»: +5,0 км"


def test_equipment_is_not_offered_when_the_backpack_is_the_problem(
    make_request, make_engineer, make_travel
):
    """Всё нужное у бригады есть, но три единицы пешему не унести (NO_CAPACITY): выдать ещё
    нечего, такое средство не помогло бы."""
    request = make_request("x", requiredEquipment=["router", "set_top_box", "alice"])
    walker = make_engineer(
        "brigade-1",
        transport="public_transport",
        equipment=["router", "set_top_box", "alice"],
    )
    items = remedies_of([request], [walker], {}, make_travel([request]))["x"]

    assert "equipment" not in kinds(items)


def test_transport_is_offered_to_the_brigade_on_the_wrong_one(
    make_request, make_engineer, make_travel
):
    request = make_request("x", skill="emergency", requiredTransport="car")
    rider = make_engineer("brigade-1", skills=["emergency"], transport="bicycle")
    items = remedies_of([request], [rider], {}, make_travel([request]))["x"]

    transport = next(item for item in items if item.kind == "transport")
    assert transport.engineer_id == "brigade-1"
    assert (
        transport.text == "Бригада 1 возьмёт, если пересадить на «Автомобиль»: +5,0 км"
    )


def test_transport_needs_its_travel_table(make_request, make_engineer, make_travel):
    """Таблицы нужного транспорта нет — проверить нечем, и средства нет, а не падение."""
    request = make_request("x", skill="emergency", requiredTransport="car")
    rider = make_engineer("brigade-1", skills=["emergency"], transport="bicycle")
    travel = make_travel([request])
    del travel.matrices["auto"]
    items = remedies_of([request], [rider], {}, travel)["x"]

    assert "transport" not in kinds(items)
    assert "engineer" not in kinds(items)  # типовой бригаде на автомобиле тоже нечем


# --- ещё бригада -------------------------------------------------------------


def test_extra_engineer_counts_what_else_it_would_pick_up(
    make_request, make_engineer, make_travel
):
    """Ни у кого нет навыка: типовая бригада с навыком заявки возьмёт её и ещё одну такую же."""
    requests = [
        make_request("x", windowStart="10:00", windowEnd="18:00"),
        make_request("y", windowStart="10:00", windowEnd="18:00"),
        make_request("z", skill="emergency"),
    ]
    items = remedies_of(
        requests,
        [make_engineer("brigade-1", skills=["connection"])],
        {},
        make_travel(requests),
    )

    assert kinds(items["x"]) == ["engineer"]
    extra = items["x"][0]
    assert extra.text == (
        "Ещё один инженер с навыком «Локальные работы» (автомобиль, 10:00–22:00, "
        "из офиса) взял бы её и ещё 1 неназначенную"
    )
    engineer = extra.engineer
    # Первый свободный номер новичка: форма добавит его под этим же id, а демо-люди — `brigade-N`.
    assert engineer.id == "new-1"
    assert engineer.skills == ["local"]
    assert engineer.transport == "car"
    assert (engineer.shift_start, engineer.shift_end) == ("10:00", "22:00")
    assert (engineer.start_lat, engineer.start_lon) == (
        OFFICE_POINT["lat"],
        OFFICE_POINT["lon"],
    )
    # Аварии нужен свой навык: типовая бригада — под навык этой заявки.
    assert "ещё" not in items["z"][0].text


def test_typical_engineer_rides_the_transport_the_request_needs(
    make_request, make_engineer, make_travel
):
    """Заявке нужен велосипед: типовая бригада на автомобиле её бы не взяла."""
    request = make_request("x", requiredTransport="bicycle")
    items = remedies_of(
        [request],
        [make_engineer("brigade-1", skills=["connection"])],
        {},
        make_travel([request]),
    )["x"]

    assert items[0].engineer.transport == "bicycle"
    assert "(велосипед, 10:00–22:00, из офиса)" in items[0].text


def test_extra_engineer_does_not_count_requests_that_already_fit(
    make_request, make_engineer, make_travel
):
    """y встаёт к бригаде 1 как есть — её ставит «Переназначить», а не новый человек. Типовая
    бригада для x (навык «Локальные работы» и роутер) y взять могла бы, но в счёт она не идёт."""
    x = make_request(
        "x", windowStart="10:00", windowEnd="18:00", requiredEquipment=["router"]
    )
    y = make_request("y", windowStart="10:00", windowEnd="18:00")
    items = remedies_of([x, y], [make_engineer("brigade-1")], {}, make_travel([x, y]))

    assert items["y"] == []
    extra = next(item for item in items["x"] if item.kind == "engineer")
    assert extra.text.endswith("взял бы её")


def test_extra_engineer_comes_from_the_pool_first(
    make_request, make_engineer, make_travel
):
    """Запасные люди встроенного набора: первый, кто берёт заявку. Без навыка — мимо."""
    request = make_request("x", windowStart="10:00", windowEnd="18:00")
    pool = [
        make_engineer("brigade-7", skills=["connection"]),
        make_engineer("brigade-8", transport="bicycle", shiftStart="10:00"),
    ]
    items = remedies_of(
        [request],
        [make_engineer("brigade-1", skills=["connection"])],
        {},
        make_travel([request]),
        pool,
    )["x"]

    extra = next(item for item in items if item.kind == "engineer")
    assert extra.engineer.id == "brigade-8"
    assert extra.text == (
        "Ещё один инженер — Бригада 8 (велосипед, 10:00–18:00) — взял бы её"
    )


def test_extra_engineer_joins_at_the_event(make_request, make_engineer, make_travel):
    """Перепланирование в 13:00: вышедшая сейчас бригада к окну 09:00–12:00 не успеет."""
    request = make_request("x")
    engineers = [make_engineer("brigade-1", skills=["connection"])]
    travel = make_travel([request])

    late = remedies_of([request], engineers, {}, travel, joined="13:00")["x"]
    now = remedies_of([request], engineers, {}, travel)["x"]

    assert "engineer" not in kinds(late)
    assert kinds(now) == ["engineer"]


# --- продлить смену ----------------------------------------------------------


def test_shift_is_extended_by_the_shortest_step(
    make_request, make_engineer, make_travel
):
    """Начало в 17:20, час работы — конец в 18:20 при смене до 18:00: хватит получаса."""
    request = make_request("x", windowStart="17:20", windowEnd="17:30")
    items = remedies_of(
        [request], [make_engineer("brigade-1")], {}, make_travel([request])
    )["x"]

    shift = next(item for item in items if item.kind == "shift")
    assert shift.engineer_id == "brigade-1"
    assert shift.text == "Бригада 1 возьмёт, если продлить смену до 18:30: +5,0 км"


def test_shift_is_not_offered_when_an_hour_is_not_enough(
    make_request, make_engineer, make_travel
):
    """Два часа работы с 17:30 — конец в 19:30, а продлеваем не больше чем до 19:00."""
    request = make_request(
        "x",
        windowStart="17:30",
        windowEnd="17:40",
        serviceDurationMin=120,
        baseNormMin=140,
    )
    items = remedies_of(
        [request], [make_engineer("brigade-1")], {}, make_travel([request])
    )["x"]

    assert "shift" not in kinds(items)


def test_gone_engineer_is_never_offered(make_request, make_engineer, make_travel):
    """Выбывшая бригада новой работы не берёт (PLAN 6.12). Продлить ей смену до 19:00 значило
    бы дать ей час работы после выбытия — средство обязано это знать само."""
    request = make_request(
        "x",
        windowStart="17:00",
        windowEnd="18:30",
        serviceDurationMin=30,
        baseNormMin=50,
        requiredEquipment=["router"],
    )
    gone = make_engineer("brigade-1", unavailableFrom="12:00")
    items = remedies_of([request], [gone], {}, make_travel([request]))["x"]

    assert all(item.engineer_id != "brigade-1" for item in items)
    assert kinds(items) == ["engineer"]


# --- в плане -----------------------------------------------------------------


def test_plan_carries_remedies_only_for_unassigned(
    make_request, make_engineer, make_dataset, make_travel
):
    requests = [make_request("1"), make_request("2", skill="emergency")]
    dataset = make_dataset(requests, [make_engineer("brigade-1")])
    plan = service.build_plan(dataset, make_travel(requests))

    assert plan.explanations["1"].remedies == []
    assert kinds(plan.explanations["2"].remedies) == ["engineer"]
    assert plan.validation.ok


def test_builtin_dataset_offers_people_from_the_crew_pool(
    make_request, make_engineer, make_dataset, make_travel, monkeypatch
):
    """Запасные — из `crews.json` без тех, кто уже в наборе; старт — офис набора."""
    requests = [make_request("1", skill="emergency")]
    engineers = [make_engineer("brigade-1")]
    pool = [
        engineer.model_dump(by_alias=True, exclude={"start_lat", "start_lon"})
        for engineer in [
            make_engineer("brigade-1", skills=["emergency"]),
            make_engineer("brigade-2", skills=["emergency"]),
        ]
    ]
    monkeypatch.setattr(seed, "load_crew_pool", lambda: pool)
    dataset = make_dataset(requests, engineers).model_copy(update={"built_in": True})
    plan = service.build_plan(dataset, make_travel(requests))

    extra = plan.explanations["1"].remedies[0]
    assert extra.engineer.id == "brigade-2"
    assert (extra.engineer.start_lat, extra.engineer.start_lon) == (
        OFFICE_POINT["lat"],
        OFFICE_POINT["lon"],
    )


def test_builtin_plan_has_remedies_for_every_unassigned():
    """Встроенный набор, базовый вариант: десятки неназначенных, у каждой список собран, и
    каждое средство называет то, что можно сделать."""
    dataset = seed.load_builtin()
    travel = matrices.Travel(
        dataset.id,
        matrices.points(dataset),
        {
            document["profile"]: matrices.from_document(document)
            for document in seed.load_matrices()
        },
    )
    plan = service.build_plan(dataset, travel)

    unassigned = [item.request_id for item in plan.unassigned]
    assert unassigned
    helped = [rid for rid in unassigned if plan.explanations[rid].remedies]
    assert helped
    for rid in helped:
        for item in plan.explanations[rid].remedies:
            assert item.text
            if item.kind == "window":
                assert item.window_start and item.window_end and item.engineer_id
            if item.kind == "engineer":
                assert item.engineer is not None
