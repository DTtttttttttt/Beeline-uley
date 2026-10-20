"""Отзывы клиентов и рейтинг инженера (PLAN 9.1, блок 70).

Отзывы — коллекция `reviews`, одна оценка на заявку в пространстве людей (`profiles.namespace`):
у демо-наборов люди одни (`crews.json`), у загруженного — свои. Оценка относится к тому, кто
работу выполнил, — к `Closure.engineer_id`, а не к тому, кто стоял в утреннем плане.

Рейтинг в плане — снимок (`Engineer.rating`): его ставит `snapshot` до расчёта, а `build_plan`
остаётся чистым и в базу не ходит. Живой рейтинг для списков отдаёт `ratings`.

У загруженного набора `Engineer.rating` в самом наборе — стартовый рейтинг из файла (блок 76):
средняя и число оценок до этого дня, без самих отзывов. Он складывается с отзывами базы как
оценки старше месяца; распределение 5…1 и последние отзывы — только по отзывам базы.
"""

import hmac
import random
from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from pymongo.errors import DuplicateKeyError

from .. import db
from ..config import settings
from ..dictionaries import Outcome, ReviewSource, ReviewTag
from ..models import Closure, Dataset, EngineerRating, Plan, Rating, Review, ReviewInput
from . import explain
from .profiles import namespace
from .replan import ReplanError

LAST = 5  # последних отзывов в рейтинге
MONTH = timedelta(days=30)

# Тексты демо-истории (`prepare_data.py --reviews`) — фиксированные.
COMMENTS = {
    5: [
        "Спасибо за качественную работу, всё подробно объяснил.",
        "Приехал вовремя, всё сделал быстро и аккуратно.",
        "Отличный мастер, рекомендую.",
        "",
    ],
    4: ["В целом всё хорошо, немного задержался.", "Работой доволен.", ""],
    3: ["Сделал, но пришлось ждать.", "Нормально, без восторга.", ""],
    2: ["Опоздал и не всё объяснил.", "Пришлось вызывать повторно.", ""],
    1: ["Работа не сделана до конца.", "Очень недоволен визитом.", ""],
}
CLIENT_NAMES = ["Мария", "Алексей", "Ольга", "Дмитрий", "Наталья", "Сергей", "", ""]


def applied(plan: Plan) -> None:
    """Черновик предпросмотра — ещё не факт дня: его закрытия не оцениваются."""
    if plan.draft:
        raise ReplanError(409, "Вариант ещё не применён — сначала «Применить»")


def closure_of(plan: Plan, request_id: str) -> Closure:
    """Закрытие, которое можно оценить: только у применённой версии и только `done`."""
    applied(plan)
    closure = plan.closed.get(request_id)
    if closure is None or closure.outcome is not Outcome.DONE:
        raise ReplanError(409, explain.review_refusal(request_id, closure))
    return closure


def _key(plan: Plan, request_id: str) -> dict:
    return {"namespace": namespace(plan.dataset_id), "requestId": request_id}


def _document(plan: Plan, review: Review) -> dict:
    return {"namespace": namespace(plan.dataset_id)} | review.model_dump(by_alias=True)


def _review(document: dict) -> Review:
    return Review.model_validate(
        {
            key: value
            for key, value in document.items()
            if key not in ("_id", "namespace")
        }
    )


def find(plan: Plan, request_id: str) -> Review | None:
    document = db.reviews.find_one(_key(plan, request_id))
    return _review(document) if document else None


def of_plan(plan: Plan) -> list[Review]:
    """Оценки выполненных заявок версии — для их строк в списке."""
    ids = [
        request_id
        for request_id, closure in plan.closed.items()
        if closure.outcome is Outcome.DONE
    ]
    key = {"namespace": namespace(plan.dataset_id), "requestId": {"$in": ids}}
    return [_review(document) for document in db.reviews.find(key)]


def save(
    plan: Plan, request_id: str, given: ReviewInput, source: ReviewSource
) -> Review:
    """Сохранить оценку выполненной заявки.

    Диспетчер правит общую оценку заявки, кто бы её ни поставил. Клиент ставит её только
    один раз: оценка, появившаяся, пока он заполнял форму, не перезаписывается — 409.
    """
    closure = closure_of(plan, request_id)
    review = Review(
        **given.model_dump(),
        request_id=request_id,
        engineer_id=closure.engineer_id,
        source=source,
        at=datetime.now(UTC),
    )
    if source is ReviewSource.DISPATCHER:
        db.reviews.replace_one(
            _key(plan, request_id), _document(plan, review), upsert=True
        )
        return review
    try:
        db.reviews.insert_one(_document(plan, review))
    except DuplicateKeyError:
        raise ReplanError(409, explain.review_taken()) from None
    return review


# --- ссылка клиента ----------------------------------------------------------


def _signature(payload: bytes) -> str:
    digest = hmac.new(settings.rating_secret.encode(), payload, sha256).digest()
    return urlsafe_b64encode(digest).decode()[:22]


def token(plan_id: str, request_id: str) -> str:
    """Подпись версии плана и заявки, без хранения: версия неизменяема, исполнитель — в ней."""
    payload = f"{plan_id}:{request_id}".encode()
    return f"{urlsafe_b64encode(payload).decode().rstrip('=')}.{_signature(payload)}"


def parse(value: str) -> tuple[str, str] | None:
    """`(planId, requestId)` подписанной ссылки; подделанная или битая — `None`."""
    body, _, signature = value.partition(".")
    try:
        payload = urlsafe_b64decode(body + "=" * (-len(body) % 4))
    except ValueError:
        return None
    if not hmac.compare_digest(_signature(payload), signature):
        return None
    plan_id, colon, request_id = payload.decode().partition(":")
    return (plan_id, request_id) if colon else None


def link(plan_id: str, request_id: str) -> str:
    return f"{settings.public_base_url.rstrip('/')}/rate/{token(plan_id, request_id)}"


# --- рейтинг -----------------------------------------------------------------


def _mean(reviews: list[Review], start: Rating | None) -> float:
    """Средняя отзывов вместе со стартовым рейтингом из файла (его оценки — как отзывы)."""
    total = sum(review.stars for review in reviews)
    count = len(reviews)
    if start:
        total += start.stars * start.count
        count += start.count
    return total / count


def ratings(
    space: str, now: datetime | None = None, starts: dict[str, Rating] | None = None
) -> dict[str, EngineerRating]:
    """Живой рейтинг каждого инженера пространства, у кого есть отзывы или стартовый
    рейтинг из файла (`starts`, блок 76)."""
    now = now or datetime.now(UTC)
    starts = starts or {}
    grouped: dict[str, list[Review]] = {engineer_id: [] for engineer_id in starts}
    for document in db.reviews.find({"namespace": space}):
        review = _review(document)
        grouped.setdefault(review.engineer_id, []).append(review)
    result = {}
    for engineer_id, reviews in grouped.items():
        start = starts.get(engineer_id)
        reviews.sort(key=lambda review: review.at, reverse=True)
        old = [review for review in reviews if review.at <= now - MONTH]
        result[engineer_id] = EngineerRating(
            engineer_id=engineer_id,
            stars=round(_mean(reviews, start), 2),
            count=len(reviews) + (start.count if start else 0),
            distribution=[
                sum(review.stars == stars for review in reviews)
                for stars in range(5, 0, -1)
            ],
            month_change=round(_mean(reviews, start) - _mean(old, start), 2)
            if old or start
            else None,
            last=reviews[:LAST],
        )
    return result


def start_ratings(dataset: Dataset) -> dict[str, Rating]:
    """Стартовый рейтинг бригад набора из файла загрузки (блок 76)."""
    return {
        engineer.id: engineer.rating
        for engineer in dataset.engineers
        if engineer.rating
    }


def of_engineer(space: str, engineer_id: str) -> list[Review]:
    documents = db.reviews.find({"namespace": space, "engineerId": engineer_id})
    return sorted(map(_review, documents), key=lambda review: review.at, reverse=True)


def snapshot(dataset: Dataset) -> Dataset:
    """Набор с рейтингом бригад на сейчас (PLAN 9.1): снимок едет в `plan.input` и за день
    не меняется — перепланирование берёт бригады родителя."""
    current = ratings(namespace(dataset.id), starts=start_ratings(dataset))
    engineers = [
        engineer.model_copy(
            update={
                "rating": Rating(
                    stars=current[engineer.id].stars, count=current[engineer.id].count
                )
                if engineer.id in current
                else None
            }
        )
        for engineer in dataset.engineers
    ]
    return dataset.model_copy(update={"engineers": engineers})


# --- демо-история ------------------------------------------------------------


def written(rnd: random.Random, stars: int) -> ReviewInput:
    """Метки, комментарий и имя к оценке демо-истории (`prepare_data.py --reviews`) — из
    фиксированных списков."""
    # Меток жалоб нет: низкая оценка — без меток, похвалы у средней — одна.
    tags = list(ReviewTag)
    if stars >= 4:
        tags = rnd.sample(tags, rnd.randint(1, 3))
    elif stars == 3:
        tags = [rnd.choice(tags)]
    else:
        tags = []
    return ReviewInput(
        stars=stars,
        tags=tags,
        comment=rnd.choice(COMMENTS[stars]),
        client_name=rnd.choice(CLIENT_NAMES),
    )
