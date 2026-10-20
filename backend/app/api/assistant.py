"""Ассистент диспетчера: фраза → событие или ответ (PLAN 5.4, блоки 42, 60)."""

from datetime import UTC, datetime
from uuid import uuid4

from fastapi import APIRouter, HTTPException, UploadFile
from pydantic import Field

from .. import assistant, attachments, db, llm
from ..models import (
    ATTACHMENTS,
    AssistantAttachment,
    AssistantReply,
    AssistantTurn,
    Clock,
    Model,
)
from ..planning import diff, explain, replan, service
from .plans import _checked, _parent

router = APIRouter(prefix="/api/plans", tags=["assistant"])
# Вложения не привязаны к плану: файл прикрепляют до того, как написать фразу.
files_router = APIRouter(prefix="/api/assistant", tags=["assistant"])


class AssistantRequest(Model):
    """Фраза диспетчера, время дня из шапки (им датируется событие без времени) и прежняя переписка."""

    text: str = Field(min_length=1, max_length=500)
    time: Clock
    # Последние реплики переписки, от старой к новой: по ним ассистент понимает «он», «эта заявка».
    history: list[AssistantTurn] = Field(default=[], max_length=10)
    # Вложения этой фразы (блок 60): id из `POST /api/assistant/attachments`.
    attachments: list[str] = Field(default=[], max_length=ATTACHMENTS)


@router.post("/{plan_id}/assistant", response_model=AssistantReply)
def ask_assistant(plan_id: str, body: AssistantRequest) -> AssistantReply:
    """Событие или заявка — предложение, ответ — по фактам плана. План ассистент не меняет."""
    plan, dataset = _parent(plan_id)
    files = _files(body)
    try:
        reply = assistant.handle(plan, body.text, body.time, body.history, files)
    except llm.AssistantOff as error:
        raise HTTPException(503, str(error)) from error
    except llm.AssistantFailed as error:
        raise HTTPException(502, str(error)) from error
    return _counted(plan, dataset, reply) if reply.what_if else reply


def _counted(plan, dataset, reply: AssistantReply) -> AssistantReply:
    """«Что будет, если…» (блок 61): действие считается черновиком тем же путём, что
    `POST /events` и `POST /manual`, а текст ответа пишет код по двум версиям. План не
    меняется: черновик живёт час (как варианты «Пересчёта дня»), версией его делает
    `POST /commit` по «Применить». Отказ — ответом ассистента, а не ошибкой запроса."""
    try:
        if reply.reassign:
            target = reply.reassign
            draft = service.manual_plan(
                plan, dataset, target.request_id, target.engineer_id, None, draft=True
            )
        else:
            mode = reply.mode or replan.default_mode(reply.event)
            draft = service.replan_plan(plan, dataset, reply.event, mode, draft=True)
    except replan.ReplanError as error:
        return AssistantReply(kind="clarify", text=explain.what_if_refused(str(error)))
    _checked(draft)
    changed = diff.summary(plan, draft, reply.event)
    text = explain.what_if_text(
        reply.text, plan, draft, changed.reassigned, changed.shifted
    )
    return reply.model_copy(update={"text": text, "draft_plan_id": draft.id})


def _files(body: AssistantRequest) -> list[tuple[str, str]]:
    """Вложения для модели: этой фразы, затем прежних реплик от новых к старым, не больше трёх.

    Пропавшее вложение этой фразы — 404: диспетчер только что его прикрепил и ждёт ответа по
    нему. Вложение из истории пропадает молча — сутки (TTL) прошли, и спрашивают уже не о нём.
    """
    earlier = (item for turn in reversed(body.history) for item in turn.attachments)
    ids = list(dict.fromkeys([*body.attachments, *earlier]))[:ATTACHMENTS]
    if not ids:
        return []  # без вложений Mongo не нужна
    found = {doc["_id"]: doc for doc in db.attachments.find({"_id": {"$in": ids}})}
    if any(item not in found for item in body.attachments):
        raise HTTPException(404, "Вложение не найдено — прикрепите файл заново")
    return [
        (found[item]["name"], found[item].get("summary", ""), found[item]["text"])
        for item in ids
        if item in found
    ]


@files_router.post("/attachments", response_model=AssistantAttachment)
def attach(file: UploadFile) -> AssistantAttachment:
    """Файл к сообщению ассистента: CSV, JSON или TXT до 1 МБ (блок 60). 413 — велик, 415 — не тот тип."""
    # Читаем на байт больше предела — этого хватает, чтобы узнать «больше 1 МБ». От большого
    # тела запроса это не защищает: Starlette к этому моменту уже принял файл целиком.
    data = file.file.read(attachments.MAX_BYTES + 1)
    name = file.filename or "файл"
    try:
        read = attachments.read(name, data)
    except attachments.Refused as error:
        raise HTTPException(error.status, str(error)) from error
    attachment = AssistantAttachment(
        id=uuid4().hex, name=name, kind=read["kind"], summary=read["summary"]
    )
    db.attachments.insert_one(
        {
            "_id": attachment.id,
            "name": name,
            "kind": read["kind"],
            "text": read["text"],
            "summary": read["summary"],
            "size": len(data),
            "createdAt": datetime.now(UTC),
        }
    )
    return attachment
