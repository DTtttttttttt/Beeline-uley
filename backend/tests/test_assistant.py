"""Ассистент диспетчера (PLAN, блок 42).

Яндекс в тестах не зовётся: клиент подменён на `httpx.MockTransport`, как Valhalla и 2ГИС.
Модель здесь — сценарий заранее заготовленных ответов, а проверяется код вокруг неё: что он
отдаёт модели, что берёт из её ответа и чего не пропускает дальше.
"""

import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from app import assistant, llm
from app.config import settings
from app.data import seed
from app.main import app
from app.planning import service

SECRET = "AQVN-test-secret-key"


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(settings, "yandex_api_key", SECRET)
    monkeypatch.setattr(settings, "yandex_folder_id", "b1gfolder")
    monkeypatch.setattr(settings, "yandex_model", "yandexgpt/rc")
    # Тесты не должны зависеть от локального .env: там может стоять своя модель и свой effort.
    monkeypatch.setattr(settings, "yandex_reasoning_effort", "")
    monkeypatch.setattr(settings, "yandex_max_tokens", 16000)


class Yandex:
    """Сценарий ответов модели: строка или словарь — ответ 200, число — отказ с таким кодом."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, int):
            return httpx.Response(reply, text=f"тело отказа с {SECRET}")
        content = (
            reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)
        )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": content}}]}
        )

    def body(self, number: int = 0) -> dict:
        return json.loads(self.calls[number].content)


@pytest.fixture
def yandex(monkeypatch):
    def make(*replies) -> Yandex:
        model = Yandex(replies)
        monkeypatch.setattr(
            llm,
            "client",
            httpx.Client(
                base_url="https://ai.api.cloud.yandex.net",
                transport=httpx.MockTransport(model),
            ),
        )
        return model

    return make


@pytest.fixture
def plan(make_request, make_engineer, make_dataset, make_travel):
    requests = [
        make_request("1"),
        make_request("2", windowStart="10:00", windowEnd="15:00"),
        make_request("3"),
    ]
    engineers = [
        make_engineer("brigade-1", name="Соколов"),
        make_engineer("brigade-2", name="Попов"),
    ]
    return service.build_plan(
        make_dataset(requests, engineers), make_travel(requests, engineers=engineers)
    )


# --- текст → событие ----------------------------------------------------------


def test_engineer_unavailable_becomes_an_event(plan, yandex):
    yandex(
        {"intent": "engineer_unavailable", "engineerId": "brigade-1", "time": "13:00"}
    )

    reply = assistant.handle(plan, "Соколов заболел с 13:00", "11:00")

    assert reply.kind == "event"
    assert reply.event.type == "engineer_unavailable"
    assert reply.event.engineer_id == "brigade-1"
    assert reply.event.time == "13:00"
    # Диспетчер видит имя из плана, а не то, что написала модель.
    assert reply.text == "Сделать недоступным: Соколов с 13:00"


def test_time_of_day_is_used_when_the_phrase_has_no_time(plan, yandex):
    yandex({"intent": "engineer_unavailable", "engineerId": "brigade-2"})

    assert assistant.handle(plan, "Попов недоступен", "11:40").event.time == "11:40"


@pytest.mark.parametrize("given", ["25:00", "13", "тринадцать", "", None])
def test_unreadable_time_falls_back_to_the_time_of_day(plan, yandex, given):
    yandex({"intent": "cancel_request", "requestId": "1", "time": given})

    assert assistant.handle(plan, "отмена", "08:20").event.time == "08:20"


def test_dotted_time_is_normalized(plan, yandex):
    yandex({"intent": "cancel_request", "requestId": "1", "time": "8.30"})

    assert assistant.handle(plan, "отмена", "08:20").event.time == "08:30"


def test_cancel_and_defer_accept_the_number_sign(plan, yandex):
    yandex(
        {"intent": "cancel_request", "requestId": "№2", "time": "08:00"},
        {"intent": "defer_request", "requestId": "3", "time": "08:00"},
    )

    cancel = assistant.handle(plan, "клиент отменил №2", "08:00")
    defer = assistant.handle(plan, "перенеси 3 на завтра", "08:00")

    assert (cancel.event.type, cancel.event.request_ids) == ("cancel_request", ["2"])
    assert (defer.event.type, defer.event.request_ids) == ("defer_request", ["3"])
    assert defer.text == "Перенести заявку №3 на следующий день, 08:00"


def test_close_request_carries_the_outcome_and_the_real_end(plan, yandex):
    yandex(
        {
            "intent": "close_request",
            "requestId": "1",
            "outcome": "failed",
            "actualEnd": "10:45",
            "time": "11:00",
        }
    )

    reply = assistant.handle(plan, "по 1 не смогли, закончили в 10:45", "11:00")

    assert reply.event.outcome == "failed"
    assert reply.event.actual_end == "10:45"
    assert (
        "выполнить невозможно" in reply.text.lower()
        or "не выполнена" in reply.text.lower()
    )


def test_close_without_an_outcome_asks_instead_of_guessing(plan, yandex):
    yandex({"intent": "close_request", "requestId": "1"})

    reply = assistant.handle(plan, "закрой первую", "11:00")

    assert reply.kind == "clarify"
    assert reply.event is None


@pytest.mark.parametrize(
    "data",
    [
        {"intent": "engineer_unavailable", "engineerId": "brigade-99"},
        {"intent": "engineer_unavailable"},
        {"intent": "cancel_request", "requestId": "999"},
        {"intent": "defer_request"},
    ],
)
def test_an_id_that_is_not_in_the_plan_never_becomes_an_event(plan, yandex, data):
    yandex(data)

    reply = assistant.handle(plan, "что-то", "11:00")

    assert reply.kind == "clarify"
    assert reply.event is None


def test_a_dead_engineer_id_is_not_taken_from_another_intent(plan, yandex):
    """Модель могла положить в `requestId` идентификатор инженера: событие не собирается."""
    yandex({"intent": "cancel_request", "requestId": "brigade-1"})

    assert assistant.handle(plan, "отмена", "11:00").kind == "clarify"


def test_unclear_shows_the_models_question(plan, yandex):
    yandex({"intent": "unclear", "reply": "Какую заявку отменить?"})

    reply = assistant.handle(plan, "отмени", "11:00")

    assert (reply.kind, reply.text) == ("clarify", "Какую заявку отменить?")


def test_not_json_is_a_clarification_not_an_error(plan, yandex):
    yandex("Конечно, сейчас всё сделаю!")

    reply = assistant.handle(plan, "что-то", "11:00")

    assert reply.kind == "clarify"
    assert reply.event is None


def test_json_in_a_code_fence_is_read(plan, yandex):
    yandex('```json\n{"intent": "cancel_request", "requestId": "1"}\n```')

    assert assistant.handle(plan, "отмена", "08:00").kind == "event"


# --- текст → черновик заявки --------------------------------------------------


def test_new_request_becomes_a_draft_without_coordinates(plan, yandex):
    yandex(
        {
            "intent": "add_request",
            "requestType": "emergency",
            "address": "Москва, Волгоградский проспект, 128 к5",
            "windowStart": "14:00",
            "windowEnd": "16:00",
            "equipment": ["router", "router", "чайник"],
        }
    )

    reply = assistant.handle(
        plan, "авария на Волгоградском 128к5 с 14 до 16, нужен роутер", "13:00"
    )

    assert reply.kind == "add_request"
    assert reply.event is None
    assert reply.draft.request_type == "emergency"
    assert reply.draft.window_start == "14:00"
    assert reply.draft.window_end == "16:00"
    assert [e.value for e in reply.draft.equipment] == ["router"]
    assert "Авария" in reply.text


@pytest.mark.parametrize(
    "window",
    [
        {"windowStart": "16:00", "windowEnd": "14:00"},
        {"windowStart": "14:00"},
        {},
    ],
)
def test_half_or_inverted_window_is_dropped(plan, yandex, window):
    yandex(
        {"intent": "add_request", "requestType": "local", "address": "адрес", **window}
    )

    draft = assistant.handle(plan, "новая заявка", "13:00").draft

    assert (draft.window_start, draft.window_end) == (None, None)


@pytest.mark.parametrize(
    "data",
    [
        {"intent": "add_request", "requestType": "local"},
        {"intent": "add_request", "address": "адрес"},
        {"intent": "add_request", "requestType": "наладка", "address": "адрес"},
    ],
)
def test_a_draft_needs_an_address_and_a_known_type(plan, yandex, data):
    yandex(data)

    assert assistant.handle(plan, "новая заявка", "13:00").kind == "clarify"


# --- вопросы ------------------------------------------------------------------


def test_a_question_takes_two_calls_and_returns_the_answer(plan, yandex):
    model = yandex({"intent": "question"}, "Заявка №2 у Попова.")

    reply = assistant.handle(plan, "у кого заявка 2?", "11:00")

    assert (reply.kind, reply.text) == ("answer", "Заявка №2 у Попова.")
    assert len(model.calls) == 2
    # Второй вызов — без схемы: ответ человеку, а не JSON.
    assert "response_format" not in model.body(1)


def test_facts_describe_the_whole_day_and_the_named_request_in_detail(plan, yandex):
    model = yandex({"intent": "question"}, "ответ")

    assistant.handle(plan, "почему 2 не у Соколова", "11:00")

    facts = model.body(1)["messages"][1]["content"]
    assert "Соколов (brigade-1)" in facts
    assert "Попов (brigade-2)" in facts
    assert "№1 " in facts and "№3 " in facts  # обзор — все заявки
    assert "Подробно о заявке №2:" in facts
    assert plan.explanations["2"].text in facts
    assert "Подробно о заявке №1:" not in facts
    assert "Подробно о заявке №3:" not in facts


def test_a_declined_surname_still_finds_the_engineer(plan):
    facts = assistant._facts(plan, "чем занят Соколова сегодня?")

    assert "Подробно об инженере Соколов:" in facts
    assert "Подробно об инженере Попов:" not in facts


def test_a_number_inside_a_longer_number_is_not_a_mention(plan):
    assert "Подробно о заявке" not in assistant._facts(plan, "заявка 12 или 21?")


def test_free_slots_go_into_the_overview(plan):
    facts = assistant._facts(plan, "кто свободен?")

    assert "свободные окна:" in facts


def test_the_question_is_the_only_thing_that_reaches_the_model_from_the_dispatcher(
    plan, yandex
):
    model = yandex({"intent": "unclear"})

    assistant.handle(plan, "мой вопрос", "11:00")

    user = model.body(0)["messages"][1]["content"]
    assert "Фраза диспетчера: мой вопрос" in user
    assert "Время дня: 11:00" in user


# --- клиент и отказы ----------------------------------------------------------


def test_request_goes_to_the_openai_compatible_endpoint(plan, yandex):
    model = yandex({"intent": "unclear"})

    assistant.handle(plan, "что-то", "11:00")

    request = model.calls[0]
    assert str(request.url) == "https://ai.api.cloud.yandex.net/v1/chat/completions"
    assert request.headers["Authorization"] == f"Api-Key {SECRET}"
    assert request.headers["OpenAI-Project"] == "b1gfolder"
    body = model.body()
    assert body["model"] == "gpt://b1gfolder/yandexgpt/rc"
    assert body["temperature"] == 0
    assert body["response_format"]["type"] == "json_schema"
    assert body["messages"][0]["role"] == "system"


def test_a_full_model_uri_is_used_as_is(plan, yandex, monkeypatch):
    monkeypatch.setattr(settings, "yandex_model", "gpt://другой/yandexgpt-5.1")
    model = yandex({"intent": "unclear"})

    assistant.handle(plan, "что-то", "11:00")

    assert model.body()["model"] == "gpt://другой/yandexgpt-5.1"


@pytest.mark.parametrize("missing", ["yandex_api_key", "yandex_folder_id"])
def test_without_a_key_or_a_folder_the_assistant_is_off_and_asks_nothing(
    plan, yandex, monkeypatch, missing
):
    model = yandex()
    monkeypatch.setattr(settings, missing, "")

    assert not llm.enabled()
    with pytest.raises(llm.AssistantOff):
        assistant.handle(plan, "что-то", "11:00")
    assert model.calls == []


@pytest.mark.parametrize("code", [400, 401, 429, 500])
def test_a_refusal_is_one_request_and_leaks_nothing(plan, yandex, caplog, code):
    model = yandex(code)

    with caplog.at_level(logging.DEBUG), pytest.raises(llm.AssistantFailed) as error:
        assistant.handle(plan, "что-то", "11:00")

    assert len(model.calls) == 1  # повтор после отказа сжигает лимит ключа
    assert str(code) in str(error.value)
    assert SECRET not in str(error.value)
    assert SECRET not in caplog.text


def test_a_network_failure_is_one_request_too(plan, monkeypatch):
    calls = []

    def broken(request):
        calls.append(request)
        raise httpx.ConnectError("нет связи")

    monkeypatch.setattr(
        llm,
        "client",
        httpx.Client(
            base_url="https://ai.api.cloud.yandex.net",
            transport=httpx.MockTransport(broken),
        ),
    )

    with pytest.raises(llm.AssistantFailed):
        assistant.handle(plan, "что-то", "11:00")
    assert len(calls) == 1


@pytest.mark.parametrize(
    "body", [{}, {"choices": []}, {"choices": [{"message": {"content": 5}}]}]
)
def test_an_answer_of_the_wrong_shape_is_a_failure(plan, monkeypatch, body):
    monkeypatch.setattr(
        llm,
        "client",
        httpx.Client(
            base_url="https://ai.api.cloud.yandex.net",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=body)
            ),
        ),
    )

    with pytest.raises(llm.AssistantFailed):
        assistant.handle(plan, "что-то", "11:00")


# --- эндпоинт (PLAN 5.4) ------------------------------------------------------


@pytest.fixture
def client(plan, monkeypatch):
    monkeypatch.setattr(
        service, "get_plan", lambda plan_id: plan if plan_id == plan.id else None
    )
    monkeypatch.setattr(seed, "by_id", lambda _: object())
    return TestClient(app)


def test_endpoint_returns_the_event_and_does_not_touch_the_plan(client, plan, yandex):
    yandex(
        {"intent": "engineer_unavailable", "engineerId": "brigade-2", "time": "13:00"}
    )
    before = plan.model_dump()

    response = client.post(
        f"/api/plans/{plan.id}/assistant",
        json={"text": "Попов заболел", "time": "12:00"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "event"
    assert body["event"] == {
        "type": "engineer_unavailable",
        "time": "13:00",
        "engineerId": "brigade-2",
    }
    assert plan.model_dump() == before


def test_endpoint_statuses(client, plan, yandex, monkeypatch):
    assert (
        client.post(
            "/api/plans/нет-такого/assistant", json={"text": "а", "time": "12:00"}
        ).status_code
        == 404
    )
    for body in (
        {"text": "", "time": "12:00"},
        {"text": "а", "time": "полдень"},
        {"text": "а" * 501, "time": "12:00"},
        {"text": "а", "time": "12:00", "лишнее": 1},
    ):
        assert (
            client.post(f"/api/plans/{plan.id}/assistant", json=body).status_code == 422
        )

    yandex(429)
    failed = client.post(
        f"/api/plans/{plan.id}/assistant", json={"text": "а", "time": "12:00"}
    )
    assert failed.status_code == 502
    assert SECRET not in failed.text

    monkeypatch.setattr(settings, "yandex_api_key", "")
    off = client.post(
        f"/api/plans/{plan.id}/assistant", json={"text": "а", "time": "12:00"}
    )
    assert off.status_code == 503


def test_schema_requires_every_field():
    """Яндекс отвечает 400 «all fields must be required», если в схеме есть необязательное поле."""
    schema = assistant.SCHEMA

    assert set(schema["required"]) == set(schema["properties"])


def test_empty_fields_from_the_model_are_treated_as_not_named(plan, yandex):
    """Модель заполняет неназванное пустой строкой (схема требует все поля): это не значение."""
    yandex(
        {
            "intent": "cancel_request",
            "engineerId": "",
            "requestId": "1",
            "time": "",
            "outcome": "",
            "actualEnd": "",
            "requestType": "",
            "address": "",
            "windowStart": "",
            "windowEnd": "",
            "equipment": [],
            "reply": "",
        }
    )

    reply = assistant.handle(plan, "отмена первой", "08:40")

    assert (reply.kind, reply.event.request_ids, reply.event.time) == (
        "event",
        ["1"],
        "08:40",
    )


# --- контекст переписки -------------------------------------------------------


def test_history_reaches_the_model_in_both_calls(plan, yandex):
    from app.models import AssistantTurn

    model = yandex({"intent": "question"}, "ответ")
    history = [
        AssistantTurn(role="me", text="Сколько заявок у Соколова?"),
        AssistantTurn(role="bot", text="У Соколова 2 заявки."),
    ]

    assistant.handle(plan, "Какие у него заявки?", "11:00", history)

    for call in (0, 1):
        prompt = model.body(call)["messages"][1]["content"]
        assert "Прежняя переписка:" in prompt
        assert "Диспетчер: Сколько заявок у Соколова?" in prompt
        assert "Ассистент: У Соколова 2 заявки." in prompt


def test_details_follow_the_engineer_named_earlier_in_the_dialogue(plan):
    """«У него» — не имя: подробности берутся у того, о ком говорили в последних репликах."""
    from app.models import AssistantTurn

    history = [
        AssistantTurn(role="me", text="Сколько заявок у Попова?"),
        AssistantTurn(role="bot", text="У Попова 1 заявка."),
    ]

    facts = assistant._facts(plan, "Какие у него заявки?", history)

    assert "Подробно об инженере Попов:" in facts
    assert "Подробно об инженере Соколов:" not in facts


def test_the_current_phrase_wins_over_the_history(plan):
    from app.models import AssistantTurn

    history = [AssistantTurn(role="me", text="Сколько заявок у Попова?")]

    facts = assistant._facts(plan, "А у Соколова?", history)

    assert "Подробно об инженере Соколов:" in facts
    assert "Подробно об инженере Попов:" not in facts


def test_without_history_the_prompt_has_no_dialogue_section(plan, yandex):
    model = yandex({"intent": "unclear"})

    assistant.handle(plan, "что-то", "11:00")

    assert "Прежняя переписка" not in model.body()["messages"][1]["content"]


def test_endpoint_passes_the_history_and_limits_it(client, plan, yandex):
    model = yandex({"intent": "unclear"})
    turns = [{"role": "me", "text": "раз"}, {"role": "bot", "text": "два"}]

    ok = client.post(
        f"/api/plans/{plan.id}/assistant",
        json={"text": "а", "time": "12:00", "history": turns},
    )

    assert ok.status_code == 200
    assert "Ассистент: два" in model.body()["messages"][1]["content"]
    too_long = client.post(
        f"/api/plans/{plan.id}/assistant",
        json={"text": "а", "time": "12:00", "history": turns * 6},
    )
    assert too_long.status_code == 422
    unknown_role = client.post(
        f"/api/plans/{plan.id}/assistant",
        json={
            "text": "а",
            "time": "12:00",
            "history": [{"role": "system", "text": "x"}],
        },
    )
    assert unknown_role.status_code == 422


def test_help_is_a_fixed_text_and_needs_no_second_call(plan, yandex):
    model = yandex({"intent": "help"})

    reply = assistant.handle(plan, "Что ты можешь?", "11:00")

    assert (reply.kind, reply.text) == ("answer", assistant.HELP)
    assert len(model.calls) == 1
    # Возможности перечислены с примерами обоих видов: изменение дня и вопрос по плану.
    assert "недоступен" in reply.text and "Отвечать на вопросы по плану" in reply.text


def test_engineer_details_carry_the_leg_length_of_every_stop(plan):
    """Без километров переезда на «какая заявка самая дальняя» у модели нет ответа."""
    facts = assistant._facts(plan, "Какие заявки у Соколова?")

    assert "переезд 5.0 км" in facts


# --- отмена и перенос списка (блок 42) ----------------------------------------


def test_cancel_takes_a_list_and_the_text_shows_every_address(plan, yandex):
    yandex({"intent": "cancel_request", "requestIds": ["1", "3"], "time": "08:00"})

    reply = assistant.handle(plan, "Отмени все заявки в Кашире", "08:00")

    assert reply.kind == "event"
    assert reply.event.request_ids == ["1", "3"]
    # Адреса в тексте — единственное, по чему диспетчер проверит выбор модели.
    assert "2 заявки" in reply.text
    assert "• №1 — адрес" in reply.text and "• №3 — адрес" in reply.text


def test_defer_takes_a_list_too(plan, yandex):
    yandex({"intent": "defer_request", "requestIds": ["1", "2"], "time": "08:00"})

    reply = assistant.handle(plan, "Перенеси все на завтра", "08:00")

    assert (reply.event.type, reply.event.request_ids) == ("defer_request", ["1", "2"])
    assert "на следующий день" in reply.text


def test_unknown_numbers_in_the_list_are_dropped_and_none_left_is_a_question(
    plan, yandex
):
    yandex(
        {
            "intent": "cancel_request",
            "requestIds": ["1", "999", "№3", "1"],
            "time": "08:00",
        },
        {"intent": "cancel_request", "requestIds": ["999"], "time": "08:00"},
    )

    kept = assistant.handle(plan, "отмени", "08:00")
    none = assistant.handle(plan, "отмени", "08:00")

    assert kept.event.request_ids == ["1", "3"]  # дубль и чужой номер убраны
    assert none.kind == "clarify" and none.event is None


def test_started_requests_are_left_out_and_named(plan, yandex):
    """Начатую работу не отменить: одна такая отклонила бы всё событие, поэтому она отсеивается."""
    first, *rest = sorted(
        (stop for route in plan.routes for stop in route.stops),
        key=lambda stop: stop.departure,
    )
    started, other, at = first.request_id, rest[-1].request_id, first.departure
    assert rest[-1].departure > at  # к другой заявке к этому моменту ещё не выехали
    yandex({"intent": "cancel_request", "requestIds": [started, other], "time": at})

    reply = assistant.handle(plan, "отмени все", at)

    assert reply.event.request_ids == [other]
    assert (
        "Не трогаю" in reply.text and f"№{started}" in reply.text.split("Не трогаю")[1]
    )


def test_nothing_to_cancel_when_all_of_them_have_started(plan, yandex):
    started = [stop.request_id for route in plan.routes for stop in route.stops]
    yandex({"intent": "defer_request", "requestIds": started, "time": "23:00"})

    reply = assistant.handle(plan, "перенеси все", "23:00")

    assert reply.kind == "clarify"
    assert "Нечего перенести" in reply.text


def test_the_model_may_still_send_a_single_request_id(plan, yandex):
    yandex(
        {
            "intent": "cancel_request",
            "requestId": "2",
            "requestIds": [],
            "time": "08:00",
        }
    )

    assert assistant.handle(plan, "отмени 2", "08:00").event.request_ids == ["2"]


def test_the_request_list_for_the_model_names_the_engineer(plan, yandex):
    """«Все заявки Соколова» по адресам не выбрать: инженер стоит в строке каждой заявки."""
    model = yandex({"intent": "unclear"})

    assistant.handle(plan, "отмени заявки Соколова", "08:00")

    prompt = model.body()["messages"][1]["content"]
    assert "Соколов" in prompt.split("Заявки:")[1]


# --- «с начала дня» (блок 42) ---------------------------------------------------


def test_whole_day_asks_for_the_full_recalculation_mode(plan, yandex):
    yandex(
        {
            "intent": "engineer_unavailable",
            "engineerId": "brigade-1",
            "time": "19:25",
            "wholeDay": True,
        }
    )

    reply = assistant.handle(plan, "Соколов недоступен с начала дня", "19:25")

    assert reply.kind == "event"
    assert reply.mode == "full"
    assert reply.event.engineer_id == "brigade-1"
    assert "весь день с начала" in reply.text


@pytest.mark.parametrize("flag", [False, None, "true", 1])
def test_without_the_whole_day_flag_the_server_picks_the_mode(plan, yandex, flag):
    """Только настоящее `true`: строка «true» или единица — не просьба пересчитать день."""
    yandex(
        {"intent": "engineer_unavailable", "engineerId": "brigade-1", "wholeDay": flag}
    )

    reply = assistant.handle(plan, "Соколов недоступен", "11:00")

    assert reply.mode is None
    assert "весь день с начала" not in reply.text


def test_whole_day_is_kept_for_a_list_event_too(plan, yandex):
    yandex(
        {
            "intent": "defer_request",
            "requestIds": ["1", "2"],
            "time": "08:00",
            "wholeDay": True,
        }
    )

    reply = assistant.handle(plan, "перенеси 1 и 2 и пересчитай день с утра", "08:00")

    assert (reply.kind, reply.mode) == ("event", "full")


def test_an_answer_or_a_clarification_never_carries_a_mode(plan, yandex):
    yandex({"intent": "unclear", "wholeDay": True, "reply": "Что сделать?"})

    reply = assistant.handle(plan, "с начала дня", "11:00")

    assert (reply.kind, reply.mode) == ("clarify", None)


# --- отбор заявок по месту (блок 42) --------------------------------------------


@pytest.fixture
def towns(make_request, make_engineer, make_dataset, make_travel):
    addresses = {
        "1": "Кашира, улица Победы, дом 9",
        "2": "Московская область, город Кашира Центральная улица дом 21",
        "3": "Москва, шоссе Каширское, дом 136",
        "4": "Москва, проспект Пролетарский, дом 14А",
        "5": "Каширский район, деревня Ивановка",
    }
    requests = [make_request(i, address=a) for i, a in addresses.items()]
    engineers = [make_engineer("brigade-1")]
    return service.build_plan(
        make_dataset(requests, engineers), make_travel(requests, engineers=engineers)
    )


def test_place_is_matched_by_the_whole_word_in_the_address(towns, yandex):
    """«Кашира» — это город, а «Каширское шоссе» и «Каширский район» — другие слова."""
    yandex(
        {
            "intent": "cancel_request",
            "place": "Кашира",
            "requestIds": [],
            "time": "08:00",
        }
    )

    reply = assistant.handle(towns, "Отмени все заявки в Кашире", "08:00")

    assert reply.event.request_ids == ["1", "2"]
    assert "• №1 — Кашира" in reply.text and "• №2 — Московская" in reply.text


def test_the_place_filter_ignores_the_ids_the_model_guessed(towns, yandex):
    """Модель прислала и место, и номера (с чужим московским): работает только отбор по месту."""
    yandex(
        {
            "intent": "defer_request",
            "place": "кашира",
            "requestIds": ["1", "2", "4"],
            "time": "08:00",
        }
    )

    reply = assistant.handle(towns, "перенеси все заявки в Кашире", "08:00")

    assert reply.event.request_ids == ["1", "2"]


def test_a_place_that_is_not_in_any_address_is_a_question(towns, yandex):
    yandex(
        {"intent": "cancel_request", "place": "Тула", "requestIds": [], "time": "08:00"}
    )

    reply = assistant.handle(towns, "отмени все заявки в Туле", "08:00")

    assert reply.kind == "clarify" and "Тула" in reply.text
    assert reply.event is None


def test_no_place_means_the_ids_from_the_model_are_used(towns, yandex):
    yandex(
        {
            "intent": "cancel_request",
            "place": "",
            "requestIds": ["3", "4"],
            "time": "08:00",
        }
    )

    assert assistant.handle(towns, "отмени 3 и 4", "08:00").event.request_ids == [
        "3",
        "4",
    ]


# --- контекст проекта для модели (блок 42) --------------------------------------


def test_both_steps_tell_the_model_where_the_service_works(plan, yandex):
    """Без этого модель принимала адрес в Новочеркасске: карта и маршруты — только Москва и область."""
    model = yandex({"intent": "question"}, "ответ")

    assistant.handle(plan, "какие заявки бывают?", "11:00")

    for call in (0, 1):
        system = model.body(call)["messages"][0]["content"]
        assert "Московской области" in system
        assert (
            "Новочеркасск" in system
        )  # пример адреса вне зоны, чтобы модель отказывала
        assert "четырёх типов" in system


# --- рассуждающие модели (блок 42) ----------------------------------------------


def _reply_with(monkeypatch, body):
    monkeypatch.setattr(
        llm,
        "client",
        httpx.Client(
            base_url="https://ai.api.cloud.yandex.net",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=body)
            ),
        ),
    )


def test_an_answer_cut_off_by_the_token_limit_says_so(plan, monkeypatch):
    """Рассуждающая модель могла истратить лимит на рассуждении: `content` пуст, причина — `length`."""
    _reply_with(
        monkeypatch,
        {"choices": [{"finish_reason": "length", "message": {"content": None}}]},
    )

    with pytest.raises(llm.AssistantFailed, match="не уложился в лимит"):
        assistant.handle(plan, "что-то", "11:00")


def test_the_token_limit_leaves_room_for_reasoning(plan, yandex):
    model = yandex({"intent": "unclear"})

    assistant.handle(plan, "что-то", "11:00")

    assert model.body()["max_tokens"] == settings.yandex_max_tokens >= 16000


def test_reasoning_effort_is_sent_only_when_it_is_set(plan, yandex, monkeypatch):
    """Моделям без рассуждений параметр не нужен, и как они его примут, не проверено: по умолчанию нет."""
    model = yandex({"intent": "question"}, "ответ", {"intent": "question"}, "ответ")

    assistant.handle(plan, "что-то", "11:00")
    monkeypatch.setattr(settings, "yandex_reasoning_effort", "low")
    assistant.handle(plan, "что-то", "11:00")

    assert "reasoning_effort" not in model.body(
        0
    ) and "reasoning_effort" not in model.body(1)
    # Разбору фразы в JSON он не нужен и вредит: думать надо в полную силу.
    assert "reasoning_effort" not in model.body(2)
    assert model.body(3)["reasoning_effort"] == "low"
