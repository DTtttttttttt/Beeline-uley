"""Что изменилось между версиями плана (PLAN 6.13, 5.5).

Считается по двум готовым планам, без Mongo и без пересчёта расписания: всё нужное уже
лежит в `assignments`, `routes` и `metrics`. На заявку приходится **одна** запись — первая
подходящая по порядку ниже, иначе список изменений перестал бы читаться: перестановка
почти всегда сдвигает и время, а переназначение — и то и другое.
"""

from ..models import Change, Diff, EngineerChange, MetricsDiff, Plan, RequestChange
from ..timeutil import to_seconds

# Сдвиг начала работы, который стоит показывать диспетчеру (PLAN 6.13).
TIME_SHIFT_SEC = 5 * 60


def compare(old: Plan, new: Plan) -> Diff:
    """Изменения по заявкам, по бригадам и по метрикам плана."""
    was, now = _places(old), _places(new)
    old_ids = {request.id for request in old.input.requests}
    new_ids = {request.id for request in new.input.requests}
    # Порядок чтения: сначала заявки прежнего дня как они шли на входе, потом добавленные.
    order = [request.id for request in old.input.requests] + [
        request.id for request in new.input.requests if request.id not in old_ids
    ]

    # Перенос на следующий день — не отмена и не «стала неназначенной»: заявка осталась
    # в дне, но её сознательно отложили (PLAN 6.19).
    deferred = set(new.deferred) - set(old.deferred)
    changes = [
        change
        for request_id in order
        if (change := _request(request_id, old_ids, new_ids, was, now, deferred))
        is not None
    ]
    return Diff(
        requests=changes,
        engineers=_engineers(old, new),
        metrics=MetricsDiff(
            engineers_used=[old.metrics.engineers_used, new.metrics.engineers_used],
            total_km=[old.metrics.total_km, new.metrics.total_km],
            unassigned_count=[
                old.metrics.unassigned_count,
                new.metrics.unassigned_count,
            ],
        ),
    )


def _places(plan: Plan) -> dict[str, tuple[str, int, str]]:
    """Заявка → (бригада, место в маршруте с 1, начало работы). Только назначенные."""
    return {
        stop.request_id: (route.engineer_id, number, stop.start)
        for route in plan.routes
        for number, stop in enumerate(route.stops, 1)
    }


def _request(
    request_id: str,
    old_ids: set[str],
    new_ids: set[str],
    was: dict[str, tuple[str, int, str]],
    now: dict[str, tuple[str, int, str]],
    deferred: set[str],
) -> RequestChange | None:
    if request_id not in new_ids:
        return RequestChange(request_id=request_id, change=Change.CANCELLED)
    if request_id in deferred:
        return RequestChange(request_id=request_id, change=Change.DEFERRED)
    if request_id not in old_ids:
        # Кто взял новую заявку — первое, что нужно диспетчеру, поэтому `to` заполняем сразу.
        return RequestChange(
            request_id=request_id,
            change=Change.ADDED,
            to_engineer=now[request_id][0] if request_id in now else None,
        )

    before, after = was.get(request_id), now.get(request_id)
    if before is None and after is None:
        return None
    if after is None:
        return RequestChange(
            request_id=request_id,
            change=Change.BECAME_UNASSIGNED,
            from_engineer=before[0],
        )
    if before is None or before[0] != after[0]:
        return RequestChange(
            request_id=request_id,
            change=Change.REASSIGNED,
            from_engineer=None if before is None else before[0],
            to_engineer=after[0],
        )
    if before[1] != after[1]:
        return RequestChange(
            request_id=request_id,
            change=Change.ORDER_CHANGED,
            engineer_id=after[0],
            old_position=before[1],
            new_position=after[1],
        )
    if abs(to_seconds(after[2]) - to_seconds(before[2])) > TIME_SHIFT_SEC:
        return RequestChange(
            request_id=request_id,
            change=Change.TIME_CHANGED,
            engineer_id=after[0],
            old_start=before[2],
            new_start=after[2],
        )
    return None


def _engineers(old: Plan, new: Plan) -> list[EngineerChange]:
    """Пробег и число заявок по бригадам — только у тех, у кого они изменились."""
    was = {route.engineer_id: (route.km, len(route.stops)) for route in old.routes}
    now = {route.engineer_id: (route.km, len(route.stops)) for route in new.routes}
    empty = (0.0, 0)  # бригады не было в плане: ни километров, ни заявок
    return [
        EngineerChange(
            engineer_id=engineer_id,
            old_km=was.get(engineer_id, empty)[0],
            new_km=now.get(engineer_id, empty)[0],
            old_count=was.get(engineer_id, empty)[1],
            new_count=now.get(engineer_id, empty)[1],
        )
        for engineer_id in dict.fromkeys([*was, *now])
        if was.get(engineer_id, empty) != now.get(engineer_id, empty)
    ]
