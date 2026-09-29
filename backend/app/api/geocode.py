"""Координаты адреса для импорта и формы срочной заявки (PLAN 5.4) и подсказки адресов."""

import math
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import Field

from ..data import geocoder
from ..models import Model

router = APIRouter(prefix="/api", tags=["geocode"])


class GeocodeRequest(Model):
    address: str = Field(min_length=1)


class GeocodePoint(Model):
    lat: float
    lon: float


@router.post("/geocode", response_model=GeocodePoint)
def geocode_address(body: GeocodeRequest) -> GeocodePoint:
    try:
        point = geocoder.geocode(body.address)
    except geocoder.GeocodeError as error:
        # Ключа нет, он просрочен или лимит исчерпан — это не «адрес не найден».
        raise HTTPException(503, str(error)) from error
    if point is None:
        raise HTTPException(404, "Адрес не найден")
    return GeocodePoint(lat=point[0], lon=point[1])


class AddressSuggestion(Model):
    address: str
    # Только у дома: улица подставляется текстом, координаты найдёт геокодер (блок 34).
    lat: float | None
    lon: float | None


@router.get("/geocode/suggest", response_model=list[AddressSuggestion])
def suggest_addresses(
    q: Annotated[str, Query(min_length=3, max_length=200)],
    response: Response,
) -> list[AddressSuggestion]:
    try:
        found = geocoder.suggest(q)
        # Остаток лимита минуты: на нуле форма перестаёт спрашивать до `Reset`,
        # не дожидаясь 429.
        remaining, reset = geocoder.quota()
        response.headers["X-Suggest-Remaining"] = str(remaining)
        response.headers["X-Suggest-Reset"] = str(math.ceil(reset))
    except geocoder.SuggestLimited as error:
        wait = math.ceil(error.retry_after)
        raise HTTPException(
            429,
            f"Лимит подсказок в минуту исчерпан, следующая через {wait} с",
            headers={"Retry-After": str(wait)},
        ) from error
    except geocoder.SuggestFailed as error:
        raise HTTPException(502, str(error)) from error
    # 503 — выключены или 2ГИС отказал: до перезапуска бэкенда ответ не изменится,
    # и форма по нему перестаёт спрашивать.
    except geocoder.GeocodeError as error:
        raise HTTPException(503, str(error)) from error
    return [AddressSuggestion(address=a, lat=lat, lon=lon) for a, lat, lon in found]
