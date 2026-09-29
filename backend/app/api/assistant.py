"""Ассистент диспетчера: фраза → событие или ответ (PLAN 5.4, блок 42)."""

from fastapi import APIRouter, HTTPException
from pydantic import Field

from .. import assistant, llm
from ..models import AssistantReply, AssistantTurn, Clock, Model
from .plans import _parent

router = APIRouter(prefix="/api/plans", tags=["assistant"])


class AssistantRequest(Model):
    """Фраза диспетчера, время дня из шапки (им датируется событие без времени) и прежняя переписка."""

    text: str = Field(min_length=1, max_length=500)
    time: Clock
    # Последние реплики переписки, от старой к новой: по ним ассистент понимает «он», «эта заявка».
    history: list[AssistantTurn] = Field(default=[], max_length=10)


@router.post("/{plan_id}/assistant", response_model=AssistantReply)
def ask_assistant(plan_id: str, body: AssistantRequest) -> AssistantReply:
    """Событие или заявка — предложение, ответ — по фактам плана. План ассистент не меняет."""
    plan, _dataset = _parent(plan_id)
    try:
        return assistant.handle(plan, body.text, body.time, body.history)
    except llm.AssistantOff as error:
        raise HTTPException(503, str(error)) from error
    except llm.AssistantFailed as error:
        raise HTTPException(502, str(error)) from error
