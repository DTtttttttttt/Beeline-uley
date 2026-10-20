"""Выгрузка плана в CSV (PLAN 5.4, блок 56)."""

import csv
import io

import pytest
from fastapi.testclient import TestClient

from app.api import plans as api_plans
from app.data import seed
from app.main import app
from app.models import CancelRequestEvent, CloseRequestEvent, ReplanMode
from app.planning import export, replan, service
from app.routing import matrices


def rows(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text.removeprefix("﻿")), delimiter=";"))


def parts(text: str) -> tuple[dict[str, str], list[dict], list[dict]]:
    """Сводка, маршруты и неназначенные — каждая часть со своей строкой заголовков."""
    lines = rows(text)
    summary = {line[0]: line[1] for line in lines if line and line[0].startswith("#")}
    tables, current = [], None
    for line in lines:
        if not line:
            current = None
        elif not line[0].startswith("#"):
            if current is None:
                current = (line, [])
                tables.append(current)
            else:
                current[1].append(dict(zip(current[0], line, strict=True)))
    (_, routes), (_, unassigned) = tables
    return summary, routes, unassigned


@pytest.fixture(scope="module")
def builtin():
    dataset = seed.load_builtin()
    travel = matrices.Travel(
        dataset.id,
        matrices.points(dataset),
        {
            document["profile"]: matrices.from_document(document)
            for document in seed.load_matrices()
        },
    )
    return dataset, service.build_plan(dataset, travel)


def test_builtin_plan_reads_back(builtin):
    _, plan = builtin
    text = export.plan_csv(plan, "Восток")

    assert text.startswith("﻿")
    _, routes, unassigned = parts(text)
    stops = [stop for route in plan.routes for stop in route.stops]
    assert len(routes) == len(stops)
    assert len(unassigned) == len(plan.unassigned)
    assert [row["Заявка"] for row in routes] == [stop.request_id for stop in stops]
    assert [row["Не назначена"] for row in unassigned] == [
        item.request_id for item in plan.unassigned
    ]


def test_summary_matches_metrics(builtin):
    _, plan = builtin
    summary, _, _ = parts(export.plan_csv(plan, "Восток"))
    metrics = plan.metrics

    assert summary["# Набор"] == "Восток"
    assert summary["# Алгоритм"] == "Базовый"
    assert summary["# Бригад"] == str(metrics.engineers_used)
    assert summary["# Назначено"] == str(metrics.assigned_count)
    assert summary["# Не назначено"] == str(metrics.unassigned_count)
    assert summary["# Км"] == f"{metrics.total_km:.1f}".replace(".", ",")
    assert summary["# Переезд, мин"] == str(metrics.travel_min)
    assert summary["# Ожидание, мин"] == str(metrics.wait_min)
    assert summary["# Работа, мин"] == str(metrics.work_min)
    assert summary["# Событие"] == ""


def test_route_row_carries_names_and_times(builtin):
    _, plan = builtin
    _, routes, unassigned = parts(export.plan_csv(plan, "Восток"))
    route = next(route for route in plan.routes if route.stops)
    stop = route.stops[0]
    request = next(r for r in plan.input.requests if r.id == stop.request_id)
    row = next(row for row in routes if row["Заявка"] == stop.request_id)

    engineer = next(e for e in plan.input.engineers if e.id == route.engineer_id)
    assert row["Бригада"] == route.engineer_id
    assert row["Инженер"] == engineer.name
    assert row["№"] == "1"
    assert row["Адрес"] == request.address
    assert row["Окно"] == f"{request.window_start}–{request.window_end}"
    assert (row["Выезд"], row["Прибытие"], row["Начало"], row["Окончание"]) == (
        stop.departure,
        stop.arrival,
        stop.start,
        stop.end,
    )
    assert row["Статус"] == "Отправлено"  # у первичного плана день не начался
    assert row["Закреплена"] == "нет"
    assert [row["Перенос на следующий день"] for row in unassigned] == [
        "да" if item.defer_next_day else "нет" for item in plan.unassigned
    ]


def test_urgent_column_follows_the_screen_rule(
    make_request, make_engineer, make_dataset, make_travel
):
    """«Срочная» — авария или заявка, появившаяся днём (PLAN 7)."""
    requests = [
        make_request("1"),
        make_request("2", windowStart="11:00", windowEnd="13:00", urgent=True),
        make_request(
            "3", windowStart="13:00", windowEnd="15:00", workPriority="emergency"
        ),
    ]
    engineer = make_engineer()
    plan = service.build_plan(make_dataset(requests, [engineer]), make_travel(requests))
    _, routes, _ = parts(export.plan_csv(plan, "набор"))

    assert {row["Заявка"]: row["Срочная"] for row in routes} == {
        "1": "нет",
        "2": "да",
        "3": "да",
    }


def test_event_version_carries_statuses_and_committed(
    make_request, make_engineer, make_dataset, make_travel
):
    requests = [
        make_request(str(n), windowStart="09:00", windowEnd="17:00") for n in (1, 2)
    ]
    dataset = make_dataset(requests, [make_engineer()])
    travel = make_travel(requests)
    parent = service.build_plan(dataset, travel)
    event = CloseRequestEvent(
        type="close_request", time="10:15", requestId="1", outcome="done"
    )
    state = replan.prepare(parent, event, ReplanMode.FROM_EVENT, travel)
    plan = service.build_plan(dataset, travel, parent.algorithm, state)

    summary, routes, _ = parts(export.plan_csv(plan, "набор"))
    first = next(row for row in routes if row["Заявка"] == "1")
    assert first["Статус"] == "Завершено"
    assert first["Закреплена"] == "да"
    assert summary["# Событие"].startswith("Закрыть заявку №1")
    assert summary["# Статусы на"] == "10:15"


def test_filename_names_the_dataset_and_moscow_time(builtin):
    _, plan = builtin
    stamp = plan.created_at.astimezone(export.MOSCOW).strftime("%H%M")

    assert export.filename(plan, "Восток · штатный состав") == (
        f"plan-Восток-штатный-состав-{stamp}.csv"
    )


def test_endpoint_returns_the_file(builtin, monkeypatch):
    dataset, plan = builtin
    monkeypatch.setattr(service, "get_plan", lambda plan_id: plan)
    monkeypatch.setattr(seed, "by_id", lambda dataset_id: dataset)

    answer = TestClient(app).get(f"/api/plans/{plan.id}/export.csv")

    assert answer.status_code == 200
    assert answer.headers["content-type"].startswith("text/csv")
    assert answer.content.startswith("﻿".encode())
    assert "filename*=UTF-8''plan-" in answer.headers["content-disposition"]
    assert answer.content.decode("utf-8") == export.plan_csv(plan, dataset.name)


def test_unknown_plan_is_404(monkeypatch):
    monkeypatch.setattr(service, "get_plan", lambda plan_id: None)

    answer = TestClient(app).get("/api/plans/нет/export.csv")

    assert answer.status_code == 404
    assert answer.json()["detail"] == "План не найден"


def test_cancelled_requests_keep_their_addresses_in_the_event_line(
    make_request, make_engineer, make_dataset, make_travel
):
    """В плане после отмены заявок нет — адреса берутся из входа до события."""
    requests = [
        make_request(
            str(n), address=f"улица {n}", windowStart="09:00", windowEnd="17:00"
        )
        for n in (1, 2, 3)
    ]
    dataset = make_dataset(requests, [make_engineer()])
    travel = make_travel(requests)
    parent = service.build_plan(dataset, travel)
    event = CancelRequestEvent(
        type="cancel_request", time="09:00", requestIds=["2", "3"]
    )
    state = replan.prepare(parent, event, ReplanMode.FROM_EVENT, travel)
    plan = service.build_plan(dataset, travel, parent.algorithm, state)

    summary, _, _ = parts(export.plan_csv(plan, "набор", parent.input))
    assert "№2 — улица 2" in summary["# Событие"]
    assert "№3 — улица 3" in summary["# Событие"]


def test_text_from_a_file_is_not_a_formula(
    make_request, make_engineer, make_dataset, make_travel
):
    requests = [make_request("1", address="=HYPERLINK(1)")]
    plan = service.build_plan(
        make_dataset(requests, [make_engineer(name="+7 Иванов")]), make_travel(requests)
    )
    summary, routes, _ = parts(export.plan_csv(plan, "@набор"))

    assert routes[0]["Адрес"] == "'=HYPERLINK(1)"
    assert routes[0]["Инженер"] == "'+7 Иванов"
    assert summary["# Набор"] == "'@набор"


def test_filename_without_word_characters(builtin):
    _, plan = builtin
    stamp = plan.created_at.astimezone(export.MOSCOW).strftime("%H%M")

    assert export.filename(plan, "«—»") == f"plan-{stamp}.csv"


def test_endpoint_takes_the_input_before_the_event_through_manual_edits(
    make_request, make_engineer, make_dataset, make_travel, monkeypatch
):
    """Ручная правка наследует событие родителя (PLAN 6.16): вход — у версии до события."""
    requests = [
        make_request(str(n), windowStart="09:00", windowEnd="17:00") for n in (1, 2)
    ]
    dataset = make_dataset(requests, [make_engineer()])
    travel = make_travel(requests)
    parent = service.build_plan(dataset, travel)
    event = CancelRequestEvent(type="cancel_request", time="09:00", requestIds=["2"])
    state = replan.prepare(parent, event, ReplanMode.FROM_EVENT, travel)
    child = service.build_plan(dataset, travel, parent.algorithm, state)
    edited = child.model_copy(update={"id": "edited", "parent_plan_id": child.id})
    plans = {plan.id: plan for plan in (parent, child, edited)}
    monkeypatch.setattr(service, "get_plan", plans.get)

    assert child.parent_plan_id == parent.id
    assert api_plans._before_event(edited) == parent.input
    assert api_plans._before_event(parent) is None
