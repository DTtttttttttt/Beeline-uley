"""Выгрузка плана в CSV для Excel (PLAN 5.4, блок 56).

Разделитель `;`, UTF-8 с BOM и дробная часть через запятую — так Excel с русской локалью
открывает файл двойным щелчком, без мастера импорта. Сводка идёт строками `#` сверху, за ней
маршруты и неназначенные — каждая часть со своей строкой заголовков. Числа берутся из плана
как есть: выгрузка ничего не пересчитывает.
"""

import csv
import io
import re
from datetime import timedelta, timezone

from ..dictionaries import (
    ALGORITHM_NAMES,
    REQUEST_STATUS_NAMES,
    VARIANT_NAMES,
    WORK_PRIORITY_NAMES,
    WorkPriority,
)
from ..models import Input, Plan
from . import explain

# Москва без перехода на летнее время с 2014 года: смещения хватает, а `zoneinfo` на Windows
# без пакета `tzdata` базы поясов не находит.
MOSCOW = timezone(timedelta(hours=3))

ROUTE_HEADER = [
    "Бригада",
    "Инженер",
    "№",
    "Заявка",
    "Адрес",
    "Тип работы",
    "Срочная",
    "Окно",
    "Выезд",
    "Прибытие",
    "Начало",
    "Окончание",
    "Км",
    "Переезд, мин",
    "Статус",
    "Закреплена",
]
UNASSIGNED_HEADER = [
    "Не назначена",
    "Адрес",
    "Код причины",
    "Причина",
    "Перенос на следующий день",
]


def filename(plan: Plan, dataset_name: str) -> str:
    """`plan-Восток-штатный-состав-1430.csv`: набор и московское время версии."""
    name = re.sub(r"\W+", "-", dataset_name).strip("-")
    stamp = f"{plan.created_at.astimezone(MOSCOW):%H%M}"
    # Имя из одних знаков препинания дало бы «plan--1430.csv».
    return f"plan-{name}-{stamp}.csv" if name else f"plan-{stamp}.csv"


def plan_csv(plan: Plan, dataset_name: str, before: Input | None = None) -> str:
    """CSV плана. `before` — вход до события: в `plan.input` отменённых заявок уже нет,
    а строка события называет их адреса. Без него берётся вход самого плана."""
    out = io.StringIO()
    out.write("﻿")
    writer = csv.writer(out, delimiter=";", lineterminator="\r\n")
    writer.writerows(_summary(plan, dataset_name, before or plan.input))
    writer.writerow([])

    requests = {request.id: request for request in plan.input.requests}
    names = {engineer.id: engineer.name for engineer in plan.input.engineers}
    writer.writerow(ROUTE_HEADER)
    for route in plan.routes:
        for number, stop in enumerate(route.stops, start=1):
            request = requests[stop.request_id]
            status = plan.statuses.get(stop.request_id)
            writer.writerow(
                [
                    route.engineer_id,
                    _text(names[route.engineer_id]),
                    number,
                    _text(request.id),
                    _text(request.address),
                    WORK_PRIORITY_NAMES[request.work_priority],
                    # Как на экране (PLAN 7): авария или заявка, появившаяся днём.
                    _yes(
                        request.urgent
                        or request.work_priority == WorkPriority.EMERGENCY
                    ),
                    f"{request.window_start}–{request.window_end}",
                    stop.departure,
                    stop.arrival,
                    stop.start,
                    stop.end,
                    _decimal(stop.travel_km),
                    stop.travel_min,
                    REQUEST_STATUS_NAMES[status] if status else "",
                    _yes(stop.committed),
                ]
            )
    writer.writerow([])

    writer.writerow(UNASSIGNED_HEADER)
    for item in plan.unassigned:
        writer.writerow(
            [
                _text(item.request_id),
                _text(requests[item.request_id].address),
                item.reason_code,
                item.reason_text,
                _yes(item.defer_next_day),
            ]
        )
    return out.getvalue()


def _summary(plan: Plan, dataset_name: str, before: Input) -> list[list[str]]:
    metrics = plan.metrics
    # Текст события про несколько заявок многострочный, а строка сводки — одна.
    event = (
        " ".join(explain.event_text(plan.event, before).split()) if plan.event else ""
    )
    rows = [
        ["# Набор", _text(dataset_name)],
        ["# Время версии", f"{plan.created_at.astimezone(MOSCOW):%d.%m.%Y %H:%M}"],
        ["# Алгоритм", ALGORITHM_NAMES[plan.algorithm]],
        ["# Вариант", VARIANT_NAMES[plan.variant] if plan.variant else ""],
        ["# Событие", _text(event)],
        # Статус вычисляется на момент события версии (PLAN 6.19), а не на время,
        # выставленное в шапке экрана: сервер его не знает.
        ["# Статусы на", plan.event.time if plan.event else "начало дня"],
        ["# Бригад", metrics.engineers_used],
        ["# Назначено", metrics.assigned_count],
        ["# Не назначено", metrics.unassigned_count],
        ["# Км", _decimal(metrics.total_km)],
        ["# Переезд, мин", metrics.travel_min],
        ["# Ожидание, мин", metrics.wait_min],
        ["# Работа, мин", metrics.work_min],
    ]
    # Километры по прямой без пометки выдавались бы за дорожные (PLAN 3.1).
    if plan.approximate:
        rows.append(["# Расстояния", plan.approximate_note or "по прямой"])
    return rows


def _text(value: str) -> str:
    """Текст из чужого файла: Excel прочёл бы «=…», «+…», «-…», «@…» как формулу."""
    return f"'{value}" if value[:1] in ("=", "+", "-", "@") else value


def _decimal(value: float) -> str:
    return f"{value:.1f}".replace(".", ",")


def _yes(flag: bool) -> str:
    return "да" if flag else "нет"
