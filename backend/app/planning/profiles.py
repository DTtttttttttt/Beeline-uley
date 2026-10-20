"""Скорость инженера в профиле: обновляется после каждой выполненной заявки (PLAN 9.1, блок 68).

Профиль — коллекция `engineer_profiles`, а не документ набора: `seed.ensure_seeded` перезаписывает
наборы на старте. Расчёт плана берёт `Engineer.speed` набора — скорость на начало дня; профиль
только копит обновлённую скорость для показа и для будущего переключения дней.
"""

from datetime import UTC, datetime

from .. import db
from ..data import seed
from ..dictionaries import Outcome
from ..models import Plan
from ..timeutil import to_seconds
from . import duration

# Плавное обновление: speed ← 0,8·speed + 0,2·(факт работы / план работы при скорости 1).
KEEP, LEARN = 8, 2  # десятые


def namespace(dataset_id: str) -> str:
    """Люди демо-наборов одни (`crews.json`), у загруженного набора — свои."""
    built_in = dataset_id == seed.BUILTIN_ID or dataset_id.startswith("demo:")
    return seed.DEMO_SPACE if built_in else dataset_id


def updated(speed: int, planned_min: int, fact_min: int) -> int:
    """Новая скорость в тысячных. Документы и подъезд в отношение не входят — их вычитает
    вызывающий; целая арифметика, как у длительности."""
    ratio = fact_min * duration.UNIT // planned_min
    return (KEEP * speed + LEARN * ratio) // (KEEP + LEARN)


def learn(plan: Plan, before: dict) -> None:
    """Обновить профили по закрытиям `done`, которых не было у родителя (`before`).

    Только у применённой версии: черновик предпросмотра — ещё не факт.
    """
    fresh = [
        (request_id, closure)
        for request_id, closure in plan.closed.items()
        if closure.outcome is Outcome.DONE and request_id not in before
    ]
    if not fresh:
        return
    requests = {request.id: request for request in plan.input.requests}
    engineers = {engineer.id: engineer for engineer in plan.input.engineers}
    starts = {
        stop.request_id: to_seconds(stop.start)
        for route in plan.routes
        for stop in route.stops
    }
    space = namespace(plan.dataset_id)
    for request_id, closure in fresh:
        request = requests.get(request_id)
        engineer = engineers.get(closure.engineer_id)
        if request is None or engineer is None or request_id not in starts:
            continue
        overhead = duration.access_min(request) + request.documents_min
        planned = duration.at_speed(request, duration.UNIT) - overhead
        fact = (to_seconds(closure.time) - starts[request_id]) // 60 - overhead
        if planned <= 0 or fact <= 0:
            continue
        key = {"namespace": space, "engineerId": engineer.id}
        current = db.profiles.find_one(key) or {"speed": engineer.speed}
        # Одна заявка учит профиль один раз: повторный прогон того же дня (симуляция,
        # проигрывание с утра) даёт те же факты, и скорость не должна уползать от повторов.
        if any(item["requestId"] == request_id for item in current.get("history", [])):
            continue
        speed = updated(current["speed"], planned, fact)
        db.profiles.update_one(
            key,
            {
                "$set": {"speed": speed},
                "$push": {
                    "history": {
                        "requestId": request_id,
                        "planMin": planned,
                        "factMin": fact,
                        "speed": speed,
                        "at": datetime.now(UTC),
                    }
                },
            },
            upsert=True,
        )


def welcome(plan: Plan) -> None:
    """Инженер, добавленный событием, начинает со скорости по норме (правки 82).

    Номер нового (`new-N`) переиспользуется из дня в день, и без этого он получал бы профиль,
    накопленный прежним обладателем номера. Демо-человек из `crews.json` (резерв в «Что
    поможет») — тот же человек, его история остаётся.
    """
    event = plan.event
    if event is None or event.type != "add_engineer":
        return
    space = namespace(plan.dataset_id)
    known = (
        {person["id"] for person in seed.load_crew_pool()}
        if space == seed.DEMO_SPACE
        else set()
    )
    if event.engineer.id not in known:
        db.profiles.delete_one({"namespace": space, "engineerId": event.engineer.id})


def speeds(dataset_id: str) -> dict[str, int]:
    """Скорость «сейчас» по профилям: инженер → тысячные. Нет профиля — нет и записи."""
    return {
        document["engineerId"]: document["speed"]
        for document in db.profiles.find({"namespace": namespace(dataset_id)})
    }
