"""Сравнение вариантов расчёта (PLAN 6.14, блок 17).

Настройка варианта — это порядок критериев цели и лимит поиска, поэтому проверяется не
«солвер нашёл лучшее», а то, что настройка доходит до модели и меняет готовый план: один
и тот же набор при разных настройках распределяется по-разному. Примеры маленькие и с
очевидным ответом — эвристический поиск большего не обещает (PLAN 6.5).

Лимиты вариантов в тестах укорачиваются: поиск с `GUIDED_LOCAL_SEARCH` выбирает весь свой
лимит даже на трёх заявках, и пять вариантов по 10 с превратили бы прогон в минуту.

Ни Valhalla, ни сети: таблицы переездов собираются фикстурой, линии маршрутов подменяются.
"""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.data import seed
from app.main import app
from app.models import Algorithm, CancelRequestEvent, ReplanMode, Variant
from app.planning import optimizer, service, variants
from app.routing import matrices, valhalla


@pytest.fixture
def variant_setup(monkeypatch):
    """Настройки вариантов с лимитом в секунду; возвращает настройку по её варианту."""
    short = {
        variant: replace(setup, time_limit=1)
        for variant, setup in variants.SETUPS.items()
    }
    monkeypatch.setattr(variants, "SETUPS", short)
    return short.__getitem__


@pytest.fixture
def calls(monkeypatch):
    """Аргументы каждого вызова солвера: настройка обязана доехать до модели, а не остаться
    в таблице настроек."""
    seen = []
    original = optimizer.solve

    def spy(*args, **kwargs):
        seen.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(optimizer, "solve", spy)
    return seen


@pytest.fixture
def parts(make_request, make_engineer, make_travel, make_dataset):
    """Три заявки и две бригады: заявка «3» — срочная авария, её умеет только Бригада 1."""
    requests = [
        make_request("1"),
        make_request("2", windowStart="11:00", windowEnd="13:00"),
        make_request("3", skill="emergency", workPriority="emergency", urgent=True),
    ]
    engineers = [
        make_engineer("brigade-1", skills=["local", "emergency"]),
        make_engineer("brigade-2"),
    ]
    return make_dataset(requests, engineers), make_travel(requests)


def served(plan) -> set[str]:
    return {stop.request_id for route in plan.routes for stop in route.stops}


# --- все варианты вместе (PLAN 6.14) -----------------------------------------


def test_every_variant_is_valid_and_shares_one_comparison(
    mongo, parts, monkeypatch, variant_setup
):
    """Каждый вариант — полноценный план: своя строка таблицы, свой `id`, общая группа."""
    dataset, travel = parts
    monkeypatch.setattr(matrices, "get", lambda _: travel)
    monkeypatch.setattr(valhalla, "route", lambda locations, costing: [list(locations)])

    plans = service.compare(dataset, list(Variant))

    assert [plan.variant for plan in plans] == list(Variant)
    assert len({plan.id for plan in plans}) == len(plans)
    assert len({plan.compare_group_id for plan in plans}) == 1
    for plan in plans:
        assert plan.validation.errors == []
        assert plan.compute_sec > 0
        # План сохранён и открывается по своему `id`: диспетчер делает вариант рабочим,
        # не считая его заново.
        assert service.get_plan(plan.id) is not None


def test_comparison_runs_only_the_asked_variants(
    mongo, parts, monkeypatch, variant_setup
):
    dataset, travel = parts
    monkeypatch.setattr(matrices, "get", lambda _: travel)
    monkeypatch.setattr(valhalla, "route", lambda locations, costing: [list(locations)])

    plans = service.compare(dataset, [Variant.BASELINE, Variant.FAST])

    assert [plan.variant for plan in plans] == [Variant.BASELINE, Variant.FAST]
    assert [plan.algorithm for plan in plans] == [
        Algorithm.BASELINE,
        Algorithm.OPTIMIZED,
    ]


# --- порядок критериев меняет план (PLAN 6.5, 6.14) --------------------------


@pytest.fixture
def urgent_or_engineer(make_request, make_engineer, make_travel, make_dataset):
    """Две заявки можно взять либо одной бригадой без срочной, либо двумя со срочной.

    Набор тот же, что в `test_optimizer.py`, но здесь он проверяет не цель солвера, а то,
    что вариант сравнения доводит другой порядок критериев до готового плана.
    """
    whole_day = {
        "serviceDurationMin": 480,
        "windowStart": "09:00",
        "windowEnd": "10:00",
    }
    requests = [
        make_request("regular-1"),
        make_request("regular-2", windowStart="11:00", windowEnd="12:00"),
        make_request("urgent", skill="connection", urgent=True, **whole_day),
    ]
    engineers = [
        make_engineer("brigade-1", skills=["local", "connection"]),
        make_engineer("brigade-2", shiftEnd="10:30"),
    ]
    return make_dataset(requests, engineers), make_travel(requests)


def test_urgent_first_takes_the_urgent_request_where_max_requests_saves_an_engineer(
    urgent_or_engineer, variant_setup
):
    """`max_requests` — буквально по Дополнениям, п. 2; `urgent_first` — срочность выше."""
    dataset, travel = urgent_or_engineer

    saving = service.build_plan(
        dataset, travel, Algorithm.OPTIMIZED, None, variant_setup(Variant.MAX_REQUESTS)
    )
    urgent = service.build_plan(
        dataset, travel, Algorithm.OPTIMIZED, None, variant_setup(Variant.URGENT_FIRST)
    )

    assert served(saving) == {"regular-1", "regular-2"}
    assert saving.metrics.engineers_used == 1
    assert "urgent" in served(urgent)
    assert urgent.metrics.engineers_used == 2


@pytest.fixture
def one_or_two(make_request, make_engineer, make_travel, make_dataset):
    """Одна бригада: либо авария на весь день, либо две обычные заявки по часу."""
    requests = [
        make_request("r1", windowEnd="17:00"),
        make_request(
            "emergency",
            skill="emergency",
            workPriority="emergency",
            serviceDurationMin=480,
            windowStart="09:00",
            windowEnd="10:00",
        ),
        make_request("r2", windowEnd="17:00"),
    ]
    engineers = [make_engineer("brigade-1", skills=["local", "emergency"])]
    return make_dataset(requests, engineers), make_travel(requests)


def test_priority_first_takes_the_emergency_where_max_requests_takes_two_regular(
    one_or_two, variant_setup
):
    """Тот же набор, другой порядок критериев — другой план (PLAN 6.5)."""
    dataset, travel = one_or_two

    count = service.build_plan(
        dataset, travel, Algorithm.OPTIMIZED, None, variant_setup(Variant.MAX_REQUESTS)
    )
    priority = service.build_plan(
        dataset,
        travel,
        Algorithm.OPTIMIZED,
        None,
        variant_setup(Variant.PRIORITY_FIRST),
    )

    assert served(count) == {"r1", "r2"}
    assert served(priority) == {"emergency"}


# --- настройка доходит до солвера --------------------------------------------


def test_fast_variant_asks_the_solver_for_the_first_solution_only(parts, calls):
    """«Быстрый расчёт» — это отказ от поиска, а не короткий лимит (PLAN 6.14).

    Настоящая настройка, без укорачивания: её лимит и так две секунды, и он тут страховка —
    поиск останавливается на первом решении.
    """
    dataset, travel = parts

    plan = service.build_plan(
        dataset, travel, Algorithm.OPTIMIZED, None, variants.SETUPS[Variant.FAST]
    )

    assert plan.validation.ok
    (args, kwargs) = calls[0]
    assert args[4] == variants.FAST_TIME_LIMIT_SEC
    assert kwargs["guided"] is False


def test_other_variants_keep_the_guided_search(parts, calls, variant_setup):
    dataset, travel = parts

    service.build_plan(
        dataset, travel, Algorithm.OPTIMIZED, None, variant_setup(Variant.URGENT_FIRST)
    )

    (args, kwargs) = calls[0]
    assert args[4] == 1  # лимит варианта, а не SOLVER_TIME_LIMIT_SEC
    assert kwargs["guided"] is True


def test_baseline_variant_does_not_touch_the_solver(parts, calls, variant_setup):
    """Базовый вариант — алгоритм ТЗ (PLAN 6.4): OR-Tools в нём не участвует."""
    dataset, travel = parts

    plan = service.build_plan(
        dataset, travel, Algorithm.BASELINE, None, variant_setup(Variant.BASELINE)
    )

    assert calls == []
    assert plan.variant == Variant.BASELINE
    assert plan.algorithm == Algorithm.BASELINE


def test_event_on_a_variant_keeps_its_criteria(
    mongo, parts, monkeypatch, calls, variant_setup
):
    """Вариант, ставший рабочим, остаётся собой и после события (PLAN 6.14, 11.1).

    Иначе день, выбранный как «тип работ важнее количества», молча пересчитался бы целью из
    настроек — и новая версия пришла бы без подписи варианта, то есть подмена была бы не видна.
    """
    dataset, travel = parts
    monkeypatch.setattr(matrices, "get", lambda _: travel)
    monkeypatch.setattr(valhalla, "route", lambda locations, costing: [list(locations)])
    parent = service.build_plan(
        dataset,
        travel,
        Algorithm.OPTIMIZED,
        None,
        variant_setup(Variant.PRIORITY_FIRST),
    )
    calls.clear()

    plan = service.replan_plan(
        parent,
        dataset,
        CancelRequestEvent(type="cancel_request", time="09:05", requestId="2"),
        ReplanMode.FROM_EVENT,
    )

    assert plan.variant == Variant.PRIORITY_FIRST
    assert plan.validation.ok
    (args, _) = calls[0]
    # Веса считаются от порядка критериев: у priority_first тип работ старше количества,
    # у emergency_first из настроек — наоборот.
    assert args[3]["type"] > args[3]["count"]
    assert args[4] == 1  # лимит варианта, а не REPLAN_TIME_LIMIT_SEC


# --- подмена базовым в сравнении не применяется (PLAN 6.14) ------------------


def test_variant_is_not_replaced_by_the_baseline(
    make_request, make_engineer, make_travel, make_dataset, monkeypatch, variant_setup
):
    """Вариант остаётся собой, даже если проиграл базовому: иначе сравнивать нечего.

    Обе заявки помещаются в один маршрут, и базовый вариант так и делает; подменённый солвер
    разводит их по двум бригадам — при равном количестве заявок это проигрыш по цели.
    Обычный расчёт такой план заменяет базовым и говорит об этом флагом (PLAN 6.6), расчёт
    варианта — нет.
    """
    requests = [make_request("1"), make_request("2")]
    engineers = [make_engineer("brigade-1"), make_engineer("brigade-2")]
    dataset = make_dataset(requests, engineers)
    travel = make_travel(requests)
    monkeypatch.setattr(
        optimizer,
        "solve",
        lambda *_, **__: ({"brigade-1": [requests[0]], "brigade-2": [requests[1]]}, []),
    )

    variant = service.build_plan(
        dataset, travel, Algorithm.OPTIMIZED, None, variant_setup(Variant.MAX_REQUESTS)
    )
    usual = service.build_plan(dataset, travel, Algorithm.OPTIMIZED)

    assert not variant.fallback_to_baseline
    assert variant.metrics.engineers_used == 2
    assert usual.fallback_to_baseline
    assert usual.metrics.engineers_used == 1


# --- эндпоинт (PLAN 5.4) -----------------------------------------------------


@pytest.fixture
def client(mongo, parts, monkeypatch, variant_setup):
    """Приложение с маленьким текущим набором, без Valhalla и без встроенных данных."""
    dataset, travel = parts
    monkeypatch.setattr(seed, "current", lambda: dataset)
    monkeypatch.setattr(matrices, "get", lambda _: travel)
    monkeypatch.setattr(valhalla, "route", lambda locations, costing: [list(locations)])
    return TestClient(app)


def test_compare_endpoint_returns_every_variant_in_order(client):
    response = client.post("/api/plans/compare", json={})

    assert response.status_code == 200
    plans = response.json()
    assert [plan["variant"] for plan in plans] == [variant.value for variant in Variant]
    assert len({plan["compareGroupId"] for plan in plans}) == 1
    assert all(plan["validation"]["ok"] for plan in plans)


def test_compare_endpoint_takes_a_shorter_list(client):
    response = client.post(
        "/api/plans/compare", json={"variants": ["baseline", "max_requests"]}
    )

    assert response.status_code == 200
    assert [plan["variant"] for plan in response.json()] == ["baseline", "max_requests"]


def test_unknown_variant_is_rejected(client):
    assert (
        client.post("/api/plans/compare", json={"variants": ["лучший"]}).status_code
        == 422
    )


def test_comparison_needs_a_dataset(client, monkeypatch):
    monkeypatch.setattr(seed, "current", lambda: None)

    assert client.post("/api/plans/compare", json={}).status_code == 404
