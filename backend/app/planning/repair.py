"""Досчёт вставкой: что не взял солвер, пробуем поставить в готовые маршруты (PLAN 6.6, п. 1).

Перебираем **все** неназначенные заявки в том же порядке, что и критерии цели: по цене
неназначения (`optimizer.penalty`) от дорогой к дешёвой, при равной — в исходном порядке
входных данных. В `emergency_first` это аварии, затем срочные, затем по типу работ; в
`count_first` — срочные, внутри них по типу. Порядок берётся из весов, а не записан рядом:
иначе в `emergency_first` несрочная авария разбиралась бы после срочной обычной заявки и
могла бы отдать ей последнее место (ответ 15). Дойти до обычных заявок обязательно: любая
вставка увеличивает число выполненных.

Куда вставлять, решает `schedule.best_insertion` — своей проверки ограничений здесь нет.
Из какой бригады выбирать — решают веса цели: `best_insertion` знает только километры, а
километры младше бригад во всех режимах (PLAN 6.5). Досчёт «по минимуму километров» поднял
бы простаивающую бригаду ради пары километров и сделал план хуже по собственной цели проекта.
"""

from ..models import Input, Request
from ..routing.matrices import Travel
from .manual import allows
from .optimizer import penalty
from .schedule import Position, best_insertion, start_positions


def fill(
    data: Input,
    routes: dict[str, list[Request]],
    unassigned: list[Request],
    travel: Travel,
    weights: dict[str, int],
    positions: dict[str, Position] | None = None,
    pinned: dict[str, str] | None = None,
) -> list[Request]:
    """Вставляет, что встаёт, прямо в `routes`; возвращает оставшихся неназначенных.

    `pinned` — ручные закрепления (PLAN 6.16): закреплённая заявка досчитывается только
    в маршрут своей бригады.
    """
    positions = positions or start_positions(data)
    pinned = pinned or {}
    order = {request.id: number for number, request in enumerate(data.requests)}
    failed: set[str] = set()

    for request in sorted(
        unassigned, key=lambda item: (-penalty(item, weights), order[item.id])
    ):
        best: tuple[int, str, int] | None = None
        for engineer in data.engineers:
            if not allows(pinned, request.id, engineer.id):
                continue
            spot = best_insertion(
                engineer,
                routes[engineer.id],
                request,
                travel,
                positions[engineer.id],
            )
            if spot is None:
                continue
            index, extra = spot
            # Та же цена, что у солвера: километры плюс бригада, если маршрут был пуст.
            price = extra * weights["distance"] + (
                0 if routes[engineer.id] else weights["engineers"]
            )
            # При равной цене побеждает бригада раньше по входу: два запуска на одних
            # данных обязаны дать один результат.
            if best is None or price < best[0]:
                best = (price, engineer.id, index)
        if best is None:
            failed.add(request.id)
        else:
            routes[best[1]].insert(best[2], request)

    return [request for request in unassigned if request.id in failed]
