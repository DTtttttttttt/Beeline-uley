"""Допустимые бригады для каждой заявки (PLAN 6.3).

Отбор идёт **от текущего состояния бригады**: точка продолжения и `availableFrom`. При
первичном расчёте это офис и начало смены, при перепланировании — точка и время из PLAN 6.12,
иначе фильтр ошибочно отбросит или пропустит бригаду.

Своей проверки ограничений здесь нет — только `schedule.first_violation`.
"""

from ..models import Input
from ..routing.matrices import Travel
from .manual import allows
from .schedule import Position, first_violation, start_positions


def allowed(
    data: Input,
    travel: Travel,
    positions: dict[str, Position] | None = None,
    pinned: dict[str, str] | None = None,
) -> dict[str, list[str]]:
    """Заявка → бригады, в чей пустой маршрут она помещается. Пустой список — сразу неназначена.

    `pinned` — ручные закрепления (PLAN 6.16): закреплённая заявка разрешена только своей
    бригаде. Через этот же словарь запрет доезжает и до модели OR-Tools — она берёт домен
    переменной бригады отсюда.
    """
    positions = positions or start_positions(data)
    pinned = pinned or {}
    return {
        request.id: [
            engineer.id
            for engineer in data.engineers
            if allows(pinned, request.id, engineer.id)
            and first_violation(engineer, [request], travel, positions[engineer.id])
            is None
        ]
        for request in data.requests
    }
