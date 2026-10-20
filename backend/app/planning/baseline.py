"""Базовый вариант распределения — дословно по п. 2.3 ТЗ (PLAN 6.4).

Заявки разбираются в исходном порядке, бригады перебираются в исходном порядке, заявка
добавляется в конец первого маршрута, который остаётся допустимым. Это не оптимизация,
а точка отсчёта: с ней сравнивается результат блока 8 (PLAN 6.6).

Допустимость проверяет `schedule.first_violation` — своей проверки здесь нет.
"""

from ..models import Input, Request
from ..routing.matrices import Travel
from .manual import allows
from .schedule import Position, first_violation, start_positions


def assign(
    data: Input,
    travel: Travel,
    positions: dict[str, Position] | None = None,
    pinned: dict[str, str] | None = None,
) -> tuple[dict[str, list[Request]], list[Request]]:
    """Заявки по бригадам и список неназначенных.

    Ключи — **все** бригады набора в исходном порядке: простаивающая бригада тоже попадает
    в план пустым маршрутом (PLAN 5.2), и по ней считается загрузка смены.

    `positions` — состояние бригад при перепланировании (PLAN 6.12): без них базовый
    вариант считался бы от офиса и начала смены, и сравнение «не хуже базового» (PLAN 6.6)
    сравнивало бы остаток дня с несуществующим полным днём.

    `pinned` — ручные закрепления (PLAN 6.16): закреплённую заявку перебор предлагает только
    её бригаде. Готовый список кандидатов сюда не передаётся намеренно: он считается по
    пустому маршруту от точки старта, и при непроходимом плече (PLAN 3.4) его плечи — не те,
    что перебирает ТЗ; фильтровать им базовый вариант значило бы молча его изменить.
    """
    positions = positions or start_positions(data)
    pinned = pinned or {}
    routes: dict[str, list[Request]] = {engineer.id: [] for engineer in data.engineers}
    unassigned: list[Request] = []

    for request in data.requests:
        for engineer in data.engineers:
            if not allows(pinned, request.id, engineer.id):
                continue
            route = routes[engineer.id]
            position = positions[engineer.id]
            if first_violation(engineer, route + [request], travel, position) is None:
                route.append(request)
                break
        else:
            unassigned.append(request)

    return routes, unassigned
