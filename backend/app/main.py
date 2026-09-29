import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from pymongo.errors import PyMongoError

from . import db, llm
from .api import assistant, dataset, geocode, plans, scenarios
from .config import settings
from .data import seed
from .routing import valhalla

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Сервис должен подниматься и без Mongo: тогда /api/health и покажет, что она недоступна.
    try:
        db.ensure_indexes()
    except PyMongoError as error:
        log.warning("Индексы не созданы: %s", error)
    # Набор читается с диска и проверяется моделями, поэтому кроме Mongo подвести могут
    # отсутствующий файл (в контейнер не приехал том с data/) и испорченный JSON.
    # Падать на этом нельзя: /api/health и /docs нужны как раз чтобы разобраться.
    try:
        seed.ensure_seeded()
    except (PyMongoError, OSError, ValueError) as error:
        log.warning("Встроенный набор не загружен: %s", error)
    yield
    db.client.close()


app = FastAPI(title="Планировщик выездов инженеров", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
    # Без этого браузер не отдаёт заголовки скрипту: по ним форма сама замолкает, когда
    # минутный лимит подсказок исчерпан, а не шлёт запрос на каждую паузу (блок 34).
    expose_headers=["Retry-After", "X-Suggest-Remaining", "X-Suggest-Reset"],
)

app.include_router(dataset.router)
app.include_router(geocode.router)
app.include_router(plans.router)
app.include_router(assistant.router)
app.include_router(scenarios.router)


class Health(BaseModel):
    status: str
    mongo: str
    valhalla: str
    assistant: str


@app.get("/api/health", response_model=Health)
def health() -> Health:
    mongo_ok = db.ping()
    return Health(
        # Valhalla в состояние сервиса не входит: без неё работает запасной расчёт по прямой
        status="ok" if mongo_ok else "degraded",
        mongo="ok" if mongo_ok else "unavailable",
        valhalla="ok" if _valhalla_alive() else "unavailable",
        # Только «включён ли»: в Яндекс ради health не ходим, каждый вызов — расход лимита.
        assistant="ok" if llm.enabled() else "off",
    )


def _valhalla_alive() -> bool:
    # Тем же клиентом, что и матрицы: иначе системный прокси ответит за Valhalla (блок 5).
    try:
        return valhalla.client.get("/status", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False
