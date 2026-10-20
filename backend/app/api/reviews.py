"""Отзывы и рейтинг (PLAN 5.4, 9.1, блок 70): оценка диспетчера, ссылка клиента, рейтинг."""

from fastapi import APIRouter, HTTPException

from ..data import seed
from ..dictionaries import ReviewSource, WorkPriority
from ..models import (
    Dataset,
    EngineerRating,
    Plan,
    RateForm,
    RequestReview,
    Review,
    ReviewInput,
)
from ..planning import replan, reviews, service
from ..planning.profiles import namespace

router = APIRouter(prefix="/api", tags=["reviews"])


def _dataset() -> Dataset:
    dataset = seed.current()
    if dataset is None:
        raise HTTPException(404, "Набор данных не загружен")
    return dataset


def _space() -> str:
    """Пространство людей текущего набора: рейтинг смотрят по нему."""
    return namespace(_dataset().id)


def _plan(plan_id: str) -> Plan:
    plan = service.get_plan(plan_id)
    if plan is None:
        raise HTTPException(404, "План не найден")
    return plan


def _closure(plan: Plan, request_id: str):
    try:
        return reviews.closure_of(plan, request_id)
    except replan.ReplanError as error:
        raise HTTPException(error.status, str(error)) from error


def _name(plan: Plan, engineer_id: str) -> str:
    engineer = next(
        (item for item in plan.input.engineers if item.id == engineer_id), None
    )
    return engineer.name if engineer else engineer_id


@router.get("/ratings", response_model=list[EngineerRating])
def ratings() -> list[EngineerRating]:
    """Живой рейтинг инженеров текущего набора; у кого нет ни отзывов, ни стартового
    рейтинга из файла, того в ответе нет."""
    dataset = _dataset()
    space = namespace(dataset.id)
    return list(reviews.ratings(space, starts=reviews.start_ratings(dataset)).values())


@router.get("/engineers/{engineer_id}/reviews", response_model=list[Review])
def engineer_reviews(engineer_id: str) -> list[Review]:
    """Все отзывы об инженере, новые первыми."""
    return reviews.of_engineer(_space(), engineer_id)


@router.get("/plans/{plan_id}/reviews", response_model=list[Review])
def plan_reviews(plan_id: str) -> list[Review]:
    """Оценки заявок версии: строка выполненной заявки показывает свою или «Оценить»."""
    return reviews.of_plan(_plan(plan_id))


@router.get(
    "/plans/{plan_id}/requests/{request_id}/review", response_model=RequestReview
)
def request_review(plan_id: str, request_id: str) -> RequestReview:
    """Оценка выполненной заявки: кому уйдёт, что уже стоит и ссылка для клиента."""
    plan = _plan(plan_id)
    closure = _closure(plan, request_id)
    return RequestReview(
        engineer_id=closure.engineer_id,
        engineer_name=_name(plan, closure.engineer_id),
        review=reviews.find(plan, request_id),
        link=reviews.link(plan.id, request_id),
    )


@router.put("/plans/{plan_id}/requests/{request_id}/review", response_model=Review)
def put_review(plan_id: str, request_id: str, body: ReviewInput) -> Review:
    """Оценка диспетчера. Повторное сохранение — правка общей оценки заявки."""
    plan = _plan(plan_id)
    try:
        return reviews.save(plan, request_id, body, ReviewSource.DISPATCHER)
    except replan.ReplanError as error:
        raise HTTPException(error.status, str(error)) from error


def _rated(token: str) -> tuple[Plan, str]:
    """План и заявка по ссылке клиента. Любая неудача — 404: клиенту одна фраза
    «Не удалось открыть форму оценки», а перебирать причины по кодам незачем."""
    parsed = reviews.parse(token)
    plan = service.get_plan(parsed[0]) if parsed else None
    if plan is None:
        raise HTTPException(404, "Ссылка недействительна")
    try:
        reviews.closure_of(plan, parsed[1])
    except replan.ReplanError:
        raise HTTPException(404, "Ссылка недействительна") from None
    return plan, parsed[1]


@router.get("/rate/{token}", response_model=RateForm)
def rate_form(token: str) -> RateForm:
    """Форма оценки для клиента: кто выполнял, когда и оценена ли уже заявка."""
    plan, request_id = _rated(token)
    closure = plan.closed[request_id]
    request = next(item for item in plan.input.requests if item.id == request_id)
    return RateForm(
        engineer_name=_name(plan, closure.engineer_id),
        date=plan.created_at,
        connection=request.work_priority is WorkPriority.NEW_CONNECTION,
        rated=reviews.find(plan, request_id) is not None,
    )


@router.post("/rate/{token}", response_model=Review)
def rate(token: str, body: ReviewInput) -> Review:
    """Оценка клиента. Оценка уже есть — 409 «Эта заявка уже оценена», без перезаписи."""
    plan, request_id = _rated(token)
    try:
        return reviews.save(plan, request_id, body, ReviewSource.CLIENT)
    except replan.ReplanError as error:
        raise HTTPException(error.status, str(error)) from error
