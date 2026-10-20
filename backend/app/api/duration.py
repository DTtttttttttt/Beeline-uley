"""Длительность работы по объекту — для живой строки формы «Работа: 70 → 92 мин» (PLAN 9.1).

Формула одна, в `planning/duration.py`; на фронт она не копируется, форма спрашивает здесь.
"""

from fastapi import APIRouter

from ..models import DurationAnswer, DurationQuery, Request
from ..planning import duration, explain

router = APIRouter(prefix="/api/requests", tags=["requests"])


@router.post("/duration", response_model=DurationAnswer)
def request_duration(query: DurationQuery) -> DurationAnswer:
    # Заявка собирается без проверок: формуле нужны только эти поля, а адреса и окна у
    # формы, которая ещё заполняется, может не быть.
    fields = {name: value for name, value in query if name != "speed"}
    request = Request.model_construct(**fields)
    parts = duration.breakdown(request, query.speed)
    return DurationAnswer(
        norm_min=parts.norm_min,
        minutes=parts.minutes,
        work_min=parts.work_min,
        documents_min=parts.documents_min,
        access_min=parts.access_min,
        complexity=parts.complexity / duration.UNIT,
        level=duration.level(request),
        reasons=explain.duration_reasons(request, query.speed),
    )
