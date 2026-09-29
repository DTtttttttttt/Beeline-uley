"""Готовые цепочки событий (PLAN 5.4, 6.17)."""

from fastapi import APIRouter, HTTPException

from ..data import seed
from ..models import Scenario

router = APIRouter(prefix="/api", tags=["scenarios"])


@router.get("/scenarios", response_model=list[Scenario])
def list_scenarios() -> list[Scenario]:
    """Сценарии текущего набора (PLAN 6.17).

    Сценарий перечисляет конкретные заявки и бригады, поэтому отдаём только его набор:
    у загруженного диспетчером списка не будет, и это честнее, чем предлагать запуск,
    который упадёт на первом же событии.
    """
    dataset = seed.current()
    if dataset is None:
        raise HTTPException(404, "Набор данных не загружен")

    return seed.scenarios_for(dataset.id)
