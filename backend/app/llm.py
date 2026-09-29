"""Клиент YandexGPT для ассистента диспетчера (PLAN, блок 42).

OpenAI-совместимый API Yandex AI Studio: `POST /v1/chat/completions`, ключ в заголовке
`Authorization: Api-Key`, каталог облака — в `OpenAI-Project`, модель — `gpt://<каталог>/<имя>`.

Без повторов, как у подсказок 2ГИС (блок 34): любая ошибка отдаётся наверх как есть, потому что
повтор после отказа сжигает лимит ключа, а не чинит отказ. Тело ответа Яндекса и ключ ни в лог,
ни в ответ не попадают: в теле могут быть детали запроса (CLAUDE.md).
"""

import logging

import httpx

from .config import settings

log = logging.getLogger(__name__)

# Клиент модульный: тесты подменяют его на httpx.MockTransport, внешние сервисы в тестах не зовём.
client = httpx.Client(
    base_url="https://ai.api.cloud.yandex.net", timeout=settings.yandex_timeout_sec
)


class AssistantOff(Exception):
    """Ассистент выключен: не заданы ключ или каталог облака."""


class AssistantFailed(Exception):
    """Разовый сбой: Яндекс не ответил, отказал или ответил не тем. Текст безопасен для диспетчера."""


def enabled() -> bool:
    return bool(settings.yandex_api_key and settings.yandex_folder_id)


def _model_uri() -> str:
    model = settings.yandex_model
    return (
        model
        if model.startswith("gpt://")
        else f"gpt://{settings.yandex_folder_id}/{model}"
    )


def chat(system: str, user: str, schema: dict | None = None) -> str:
    """Один вызов модели, температура 0. С `schema` ответ — JSON по этой схеме (строкой)."""
    if not enabled():
        raise AssistantOff(
            "Ассистент выключен: задайте YANDEX_API_KEY и YANDEX_FOLDER_ID"
        )

    body: dict = {
        "model": _model_uri(),
        "temperature": 0,
        # Рассуждающие модели (gpt-oss) тратят на скрытые рассуждения тот же лимит: при 1000 токенов
        # каждый десятый ответ обрывался на рассуждении и приходил пустым. Лимит — потолок, а не
        # расход: платится только то, что модель написала.
        "max_tokens": settings.yandex_max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    # Только свободный ответ по фактам (без схемы): зацикливание на рассуждении бывает там, где
    # длинный запрос и текст в ответ. Разбор фразы в JSON — быстрый, и ему нужно думать в полную
    # силу: на `low` модель теряла «13» во фразе «после 13» и отказывала московским адресам.
    if schema is None and settings.yandex_reasoning_effort:
        body["reasoning_effort"] = settings.yandex_reasoning_effort
    if schema is not None:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "reply", "schema": schema},
        }

    try:
        response = client.post(
            "/v1/chat/completions",
            json=body,
            headers={
                "Authorization": f"Api-Key {settings.yandex_api_key}",
                "OpenAI-Project": settings.yandex_folder_id,
            },
        )
    except httpx.HTTPError as error:
        # Тип сбоя — в лог (не тело и не ключ): «не ответил» одинаково для таймаута и обрыва связи.
        log.warning("YandexGPT: %s", type(error).__name__)
        raise AssistantFailed("YandexGPT не ответил") from error
    if response.status_code != 200:
        log.warning("YandexGPT ответил %s", response.status_code)
        raise AssistantFailed(
            f"YandexGPT отказал в запросе (код {response.status_code})"
        )

    try:
        choice = response.json()["choices"][0]
        content = choice["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as error:
        raise AssistantFailed("YandexGPT ответил в неожиданном виде") from error
    if not isinstance(content, str):
        # Лимит выбран на рассуждении: `content` пуст, `finish_reason` — `length`.
        if choice.get("finish_reason") == "length":
            raise AssistantFailed(
                "YandexGPT не уложился в лимит ответа — повторите запрос"
            )
        raise AssistantFailed("YandexGPT ответил в неожиданном виде")
    return content
