"""Статус заявки на момент события (PLAN 6.19, ответ 8).

Статус **вычисляется**, а не хранится: все четыре времени стопа уже есть в плане, и
хранимое значение разошлось бы с ними на первом же перепланировании. Считается по готовому
расписанию во внутренних секундах — `HH:MM` для этого разбирать не нужно.

`committed` рядом означает другое — «назначение не пересматривается». Смешивать их нельзя:
`committed` управляет расчётом, статус описывает день диспетчеру.
"""

from ..dictionaries import Outcome, RequestStatus
from ..models import Closure
from .schedule import Visit

# Исход факта и статус называются одинаково: закрытая заявка дальше не движется.
STATUS_BY_OUTCOME = {outcome: RequestStatus(outcome.value) for outcome in Outcome}


def compute(
    visits: dict[str, list[Visit]],
    closed: dict[str, Closure],
    deferred: dict[str, str],
    time: int | None,
) -> dict[str, RequestStatus]:
    """Статусы всех заявок, у которых он есть: назначенных, перенесённых и закрытых.

    `time` — момент события в секундах; у первичного расчёта события нет, и тогда день ещё
    не начался: все назначенные заявки «Отправлено» (PLAN 6.19).
    """
    statuses = {
        visit.request_id: _by_times(visit, time)
        for stops in visits.values()
        for visit in stops
    }
    statuses |= dict.fromkeys(deferred, RequestStatus.DEFERRED)
    # Факт старше расписания и переноса: заявку закрыли, дальше её состояние не меняется.
    statuses |= {
        request_id: STATUS_BY_OUTCOME[closure.outcome]
        for request_id, closure in closed.items()
    }
    return statuses


def _by_times(visit: Visit, time: int | None) -> RequestStatus:
    if time is None or visit.departure > time:
        return RequestStatus.SENT
    if time < visit.arrival:
        return RequestStatus.EN_ROUTE
    if time < visit.end:
        return RequestStatus.IN_PROGRESS
    return RequestStatus.DONE
