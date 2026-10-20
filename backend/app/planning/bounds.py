"""Нижняя граница числа бригад (PLAN 6.10, блок 50).

Это оценка снизу, а не доказанный оптимум: меньше стольких бригад заявки не выполнить ни при
каком расписании, но и столько может не хватить. Каждая оценка ослабляет расписание PLAN 6.2.
Бригаде даётся самая длинная смена группы, а в каждую заявку она въезжает самым коротким
плечом из возможных, поэтому настоящий план меньше оценки не бывает.

Кто может взять заявку, решает `schedule.resource_violation` — то же правило, что у расписания.
Своей проверки ограничений здесь нет: граница только считает, сколько работы и дороги заявкам
нужно как минимум.
"""

from math import ceil

from ..dictionaries import DurationMode, Transport
from ..models import Engineer, Input, Request
from ..routing.matrices import Travel, point_id, start_id
from ..timeutil import to_seconds
from .duration import seconds
from .schedule import latest_start, resource_violation


def lower_bound(data: Input, travel: Travel, request_ids: set[str]) -> int:
    """Меньше стольких бригад заявки `request_ids` не выполнить.

    Оценка считается по группам. Группа — бригады, которые по ресурсу могут взять хоть одну из
    заявок, вместе со всеми заявками, которые могут взять только они. Заявки группы достаются
    её бригадам, поэтому граница группы — граница и всего плана. Самая широкая группа — все
    бригады и все заявки. Узкие группы ловят дефицит аварийного навыка, автомобиля и
    оборудования.
    """
    requests = [request for request in data.requests if request.id in request_ids]
    engineers = {engineer.id: engineer for engineer in data.engineers}
    crews = {
        request.id: frozenset(
            engineer.id
            for engineer in data.engineers
            if resource_violation(request, engineer) is None
        )
        for request in requests
    }
    entries = _entries(data, travel, requests)
    return max(
        (
            _estimate(
                [request for request in requests if crews[request.id] <= group],
                [engineers[engineer_id] for engineer_id in group],
                entries,
                data.duration_mode,
            )
            for group in set(crews.values()) | {frozenset(engineers)}
            if group
        ),
        default=0,
    )


def _estimate(
    requests: list[Request],
    engineers: list[Engineer],
    entries: dict[str, dict[Transport, int]],
    mode: DurationMode,
) -> int:
    """Оценка по окнам для заявок, которые могут взять только `engineers`.

    Возьмём заявки, которые обязаны начаться внутри [a, b]. Пусть бригада взяла часть из них.
    Все её работы и въезды после первой работы этой части укладываются между a и концом
    последней работы, то есть в (b − a) + самая длинная работа. Въезд в первую работу может
    идти раньше a, поэтому к промежутку прибавляется самый длинный из кратчайших въездов. Всё
    это ещё и внутри смены, поэтому промежуток не длиннее самой длинной смены. Начало заявки
    не раньше начала первой смены, а конец работы не позже конца последней.

    Оценка по объёму — частный случай: промежуток от самого раннего начала до самого позднего
    охватывает все заявки, и бригада вмещает не больше смены.

    Длительность зависит от бригады (PLAN 9.1): заявке достаётся самая быстрая бригада группы —
    у неё и работа короче, и начать при `fit_in_window` можно позже, так что граница остаётся
    нижней.
    """
    opens = min(to_seconds(engineer.shift_start) for engineer in engineers)
    closes = max(to_seconds(engineer.shift_end) for engineer in engineers)
    shift = max(
        to_seconds(engineer.shift_end) - to_seconds(engineer.shift_start)
        for engineer in engineers
    )
    transports = {engineer.transport for engineer in engineers}
    spans = []
    for request in requests:
        fastest = min(engineers, key=lambda e: seconds(request, e, mode))
        work = seconds(request, fastest, mode)
        legs = [entries[request.id][t] for t in transports if t in entries[request.id]]
        spans.append(
            (
                max(to_seconds(request.window_start), opens),
                min(latest_start(request, fastest, mode), closes - work),
                work,
                min(legs, default=0),
            )
        )

    best = 0
    for first, *_ in spans:
        inside = sorted(
            (last, work, entry) for start, last, work, entry in spans if start >= first
        )
        total = longest = farthest = 0
        for last, work, entry in inside:
            total += work + entry
            longest, farthest = max(longest, work), max(farthest, entry)
            capacity = min(last - first + longest + farthest, shift)
            best = max(best, ceil(total / capacity))
    return best


def _entries(
    data: Input, travel: Travel, requests: list[Request]
) -> dict[str, dict[Transport, int]]:
    """Кратчайший въезд в каждую заявку каждым транспортом набора. Въезжают из старта любой
    бригады или из любой другой заявки. Транспорта, которым к заявке не доехать, в словаре
    нет; группе без плеча достаётся ноль, и оценка от этого только слабее."""
    starts = {start_id(data.office, engineer) for engineer in data.engineers}
    points = {request.id: point_id(request.id) for request in data.requests}
    entries: dict[str, dict[Transport, int]] = {}
    for request in requests:
        target = points[request.id]
        sources = starts | {point for rid, point in points.items() if rid != request.id}
        entries[request.id] = {}
        for transport in {engineer.transport for engineer in data.engineers}:
            legs = [travel.travel(transport, source, target) for source in sources]
            reachable = [leg[0] for leg in legs if leg is not None]
            if reachable:
                entries[request.id][transport] = min(reachable)
    return entries
