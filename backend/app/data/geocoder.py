"""Координаты адресов: 2ГИС Geocoder и кэш в Mongo (PLAN 3.6, блок 12.1).

Геокодер вызывается в трёх местах: скрипт подготовки данных, импорт файла и форма срочной
заявки. Демо-ключ даёт 1 000 запросов на всё время, поэтому найденный адрес запоминается,
а в цикле и «на всякий случай» геокодер не зовётся.

Подсказки адресов в форме заявки — Suggest API того же каталога (блок 34).
"""

import logging
import re
import threading
import time
from collections import deque
from functools import lru_cache

import httpx

from .. import db
from ..config import settings
from ..dictionaries import LAT_RANGE, LON_RANGE

log = logging.getLogger(__name__)

# Клиент модульный: тесты подменяют его на httpx.MockTransport, внешние сервисы в тестах не зовём.
client = httpx.Client(base_url="https://catalog.api.2gis.com", timeout=15)

Point = tuple[float, float]  # (lat, lon)


class GeocodeError(Exception):
    """Геокодер недоступен: нет ключа, ключ просрочен, лимит исчерпан.

    Это не «адрес не найден»: ненайденный адрес — ответ 404 и `None`.
    """


# --- адрес (PLAN 3.4) --------------------------------------------------------

ABBR = [
    (r"\bг\b\.?\s*Город\s+Москва\b", "Москва"),
    (r"\bГород\s+Москва\b", "Москва"),
    (r"^обл\b\.?\s*", ""),  # «обл.Московская область» - область названа следом
    (r"\bобл\b\.?", "область"),  # «Московская обл., г. Химки» - область названа до
    (r"^МО\b,?", "Московская область,"),
    (r"\bул\b\.?\s*", "улица "),
    (r"\bпр-кт\b\.?\s*", "проспект "),
    (r"\bпр-зд\b\.?\s*", "проезд "),
    (r"\bпроезд\b\.\s*", "проезд "),
    (r"\bб-р\b\.?\s*", "бульвар "),
    (r"\bпер\b\.?\s*", "переулок "),
    (r"\bнаб\b\.?\s*", "набережная "),
    (r"\bш\b\.?\s*", "шоссе "),
    (r"\bпгт\b\.?\s*", "посёлок "),
    (r"\bстр\b\.?\s*", "строение "),
    (r"\bд\b\.?\s*", "дом "),
    (r"\bг\b\.?\s*", "город "),
]
APARTMENT = re.compile(r",?\s*кв\.?\s*\d+\S*")
BUILDING_PART = re.compile(r"\s+(?:корпус|строение)\s+\S+$")


def normalize_address(raw: str) -> str:
    address = " ".join(raw.split())
    address = APARTMENT.sub("", address)
    for pattern, replacement in ABBR:
        address = re.sub(pattern, replacement, address)
    # «128 к 5» и «83с 4» - корпус и строение: буква идёт сразу после номера дома.
    address = re.sub(r"(?<=\d)\s*к\s*(\d)", r" корпус \1", address)
    address = re.sub(r"(?<=\d)\s*с\s*(\d)", r" строение \1", address)
    return " ".join(address.split()).replace(" ,", ",").strip(" ,")


# --- запрос и кэш ------------------------------------------------------------


def lookup(address: str) -> Point | None:
    """Один запрос к 2ГИС. `None` — адрес не найден."""
    if not settings.dgis_api_key:
        raise GeocodeError("Нет ключа 2ГИС: задайте DGIS_API_KEY")

    response = client.get(
        "/3.0/items/geocode",
        params={"q": address, "fields": "items.point", "key": settings.dgis_api_key},
    )
    if response.status_code == 404:  # 2ГИС отвечает так, когда адрес не найден
        return None
    if response.status_code != 200:
        # Просроченный ключ и исчерпанный лимит отвечают так на каждый адрес. Записать
        # в «не найдены» весь файл - отправить искать ошибку в адресах, а не в ключе.
        raise GeocodeError(
            f"2ГИС ответил {response.status_code}: {response.text[:200]}"
        )

    items = response.json().get("result", {}).get("items", [])
    # Берём только лучшее совпадение: если оно за пределами вырезки OSM, адрес считаем
    # ненайденным, а не подставляем следующий вариант из списка.
    point = items[0].get("point") if items else None
    if point and in_region(point["lat"], point["lon"]):
        return round(point["lat"], 6), round(point["lon"], 6)
    return None


def in_region(lat: float, lon: float) -> bool:
    return LAT_RANGE[0] <= lat <= LAT_RANGE[1] and LON_RANGE[0] <= lon <= LON_RANGE[1]


def geocode(address: str) -> Point | None:
    """Координаты адреса: кэш Mongo, иначе 2ГИС. `None` — адрес не найден."""
    query = normalize_address(address)
    cached = db.geocodes.find_one({"address": query})
    if cached:
        return cached["lat"], cached["lon"]

    point = lookup(query)
    if point is None:
        # Второй заход без хвоста «корпус/строение»: 2ГИС знает дом, но не его корпус.
        without_building = BUILDING_PART.sub("", query)
        if without_building != query:
            point = lookup(without_building)
    if point is None:
        # Промахи не кэшируем: на демо-ключе это ничего не экономит, а исправленный
        # в данных адрес после такой записи уже не перезапросился бы.
        return None

    db.geocodes.update_one(
        {"address": query},
        {"$set": {"lat": point[0], "lon": point[1]}},
        upsert=True,
    )
    return point


# --- подсказки (блок 34) -----------------------------------------------------

Suggestion = tuple[
    str, float | None, float | None
]  # (адрес, lat, lon); без точки — не дом


class SuggestLimited(Exception):
    """Лимит запросов в минуту исчерпан: 2ГИС не спрашивали.

    `retry_after` — через сколько секунд освободится место: форма до тех пор молчит,
    а не шлёт запрос на каждую паузу в наборе."""

    def __init__(self, retry_after: float):
        super().__init__(retry_after)
        self.retry_after = retry_after


class SuggestFailed(Exception):
    """Разовый сбой: 2ГИС не ответил или ответил не тем. Это не отказ ключа — в отличие
    от `GeocodeError`, подсказки остаются включёнными, и форма спросит снова."""


# Скользящее окно запросов к 2ГИС за последнюю минуту. Обработчик синхронный и идёт из пула
# потоков FastAPI, поэтому окно под замком.
_sent: deque[float] = deque()
_lock = threading.Lock()
# 2ГИС ответил не 200 и не 404 — лимит ключа кончился или ключ просрочен. Дальше не
# спрашиваем до перезапуска: повторы после 429 и сожгли ключ Routing.
_refused: str | None = None

# Прямоугольник вырезки OSM: подсказка за его пределами в план всё равно не попадёт.
VIEWPOINT1 = f"{LON_RANGE[0]},{LAT_RANGE[1]}"
VIEWPOINT2 = f"{LON_RANGE[1]},{LAT_RANGE[0]}"


def suggest(query: str) -> list[Suggestion]:
    """Подсказки адресов с координатами. Пустой список — ничего не нашлось."""
    if not settings.dgis_api_key or not settings.dgis_suggest_per_min:
        raise GeocodeError(
            "Подсказки адресов выключены: нет ключа или DGIS_SUGGEST_PER_MIN"
        )
    if _refused:
        raise GeocodeError(_refused)
    return list(_suggest(" ".join(query.split()).lower()))


@lru_cache(maxsize=512)
def _suggest(query: str) -> tuple[Suggestion, ...]:
    """Запрос к 2ГИС. Кэш на строку: одинаковые префиксы ключ повторно не тратят,
    а исключение `lru_cache` не запоминает — отказ не станет «пустой подсказкой»."""
    global _refused
    _take_slot()
    try:
        # Подсказка — помощь при наборе: ждать её дольше пяти секунд незачем, а поток
        # пула на это время занят.
        response = client.get(
            "/3.0/suggests",
            params={
                "q": query,
                "suggest_type": "address",
                "fields": "items.point",
                "page_size": 5,
                "locale": "ru_RU",
                "viewpoint1": VIEWPOINT1,
                "viewpoint2": VIEWPOINT2,
                "key": settings.dgis_api_key,
            },
            timeout=5,
        )
    except httpx.HTTPError as error:
        # Сеть — не отказ ключа: следующая подсказка спросит снова.
        raise SuggestFailed("2ГИС не ответил на подсказку") from error
    if (
        response.status_code == 404
    ):  # так каталог отвечает на «ничего не найдено», как в lookup
        return ()
    if response.status_code != 200:
        # Тело ответа наружу и в лог не отдаём: в нём могут быть детали запроса (CLAUDE.md).
        _refused = (
            f"2ГИС отказал в подсказке (код {response.status_code}): "
            "подсказки выключены до перезапуска бэкенда"
        )
        log.warning(_refused)
        raise GeocodeError(_refused)

    try:
        return tuple(_parse(response.json()))
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise SuggestFailed("2ГИС прислал подсказку в неожиданном виде") from error


def _parse(body: dict) -> list[Suggestion]:
    found = []
    for item in body.get("result", {}).get("items", []):
        address = item.get("full_name") or item.get("address_name") or item.get("name")
        point = item.get("point")
        if not address or (point and not in_region(point["lat"], point["lon"])):
            continue
        # Точка есть только у дома: у улицы или района это их середина, и заявка встала бы
        # не туда. Такая подсказка только дописывает текст — дом диспетчер допечатает сам.
        if point and item.get("type") == "building":
            found.append((address, round(point["lat"], 6), round(point["lon"], 6)))
        else:
            found.append((address, None, None))
    return found


def quota() -> tuple[int, float]:
    """Сколько запросов к 2ГИС ещё можно в этой минуте и через сколько секунд освободится
    место. Едет в заголовках ответа: форма сама замолкает на последнем запросе, а не
    узнаёт о лимите из 429."""
    with _lock:
        now = time.monotonic()
        while _sent and now - _sent[0] >= 60:
            _sent.popleft()
        reset = 60 - (now - _sent[0]) if _sent else 0.0
        return max(settings.dgis_suggest_per_min - len(_sent), 0), reset


def _take_slot() -> None:
    """Место для запроса к 2ГИС: не больше `per_min` за минуту и не чаще `interval_sec`.

    Ожидание — под замком: следующий запрос встаёт за ним, так интервал и выдерживается.
    Лимит минуты проверяется до ожидания, чтобы отказ приходил сразу.
    """
    with _lock:
        now = time.monotonic()
        while _sent and now - _sent[0] >= 60:
            _sent.popleft()
        if len(_sent) >= settings.dgis_suggest_per_min:
            raise SuggestLimited(60 - (now - _sent[0]))
        if _sent:
            wait = _sent[-1] + settings.dgis_suggest_interval_sec - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
        _sent.append(now)
