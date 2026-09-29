"""Ассистент диспетчера: текст → событие и вопросы к плану (PLAN, блок 42).

Модель ничего не решает. Она переводит фразу в уже существующее событие или пересказывает
готовые факты плана, а ограничения по-прежнему проверяют `schedule.first_violation` и
`validator.py`. Ответ — предложение: событие уходит только после «Применить» диспетчера.

Разбор идёт в два шага. Первый вызов определяет, чего хочет диспетчер, по схеме JSON: фамилию в
идентификатор переводит модель (она склоняет: «Соколова» → `brigade-3`), но код проверяет, что
такой идентификатор есть в плане, и собирает событие той же моделью `Event`, что и
`POST /events`. Второй вызов — только для вопроса: контекст ему собирает код, а не модель.
"""

import json
import re

from pydantic import TypeAdapter, ValidationError

from . import llm
from .dictionaries import (
    REQUEST_STATUS_NAMES,
    REQUEST_TYPE_NAMES,
    WORK_PRIORITY_NAMES,
    Equipment,
    Outcome,
)
from .models import (
    AssistantDraft,
    AssistantReply,
    AssistantTurn,
    Engineer,
    Event,
    Plan,
)
from .planning import explain
from .timeutil import to_seconds

_EVENT = TypeAdapter(Event)

EVENTS = ("engineer_unavailable", "cancel_request", "defer_request", "close_request")
# Сколько заявок и инженеров, названных в вопросе, раскрываем подробно: остальное — обзором.
DETAILED = 5

# Яндекс принимает схему, только если все поля обязательны («all fields must be required»):
# что диспетчер не назвал, модель заполняет пустой строкой или пустым списком, а код такие
# значения не берёт — `_text`, `_clock` и проверки справочников их отбрасывают.
SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "string",
            "enum": [*EVENTS, "add_request", "question", "help", "unclear"],
        },
        "engineerId": {"type": "string"},
        "requestId": {"type": "string"},
        # Отмена и перенос принимают список: «все заявки в Кашире» — одно событие (блок 42).
        "requestIds": {"type": "array", "items": {"type": "string"}},
        "time": {"type": "string"},
        "outcome": {
            "type": "string",
            "enum": ["", *(outcome.value for outcome in Outcome)],
        },
        "actualEnd": {"type": "string"},
        "requestType": {"type": "string", "enum": ["", *REQUEST_TYPE_NAMES]},
        "address": {"type": "string"},
        "windowStart": {"type": "string"},
        "windowEnd": {"type": "string"},
        "equipment": {
            "type": "array",
            "items": {"type": "string", "enum": [e.value for e in Equipment]},
        },
        "reply": {"type": "string"},
        # Пересчитать весь день с начала («с утра», «с начала дня»), а не с момента события.
        "wholeDay": {"type": "boolean"},
        # Место, по которому диспетчер отбирает заявки («Кашира»): заявки по нему отбирает код.
        "place": {"type": "string"},
    },
    "required": [
        "intent",
        "engineerId",
        "requestId",
        "requestIds",
        "time",
        "outcome",
        "actualEnd",
        "requestType",
        "address",
        "windowStart",
        "windowEnd",
        "equipment",
        "reply",
        "wholeDay",
        "place",
    ],
}

# Что за сервис и где он работает: без этого модель принимала адреса за пределами области
# (Новочеркасск) и не знала, что такое четыре типа заявок. Общий для обоих шагов разбора.
PROJECT = """О сервисе. Это планировщик выездов инженеров оператора связи: диспетчер видит один \
рабочий день — заявки, инженеров, их маршруты на карте, и меняет день событиями (выбытие \
инженера, отмена и перенос заявок, закрытие по факту, новая заявка). Заявки четырёх типов: \
подключение клиента, авария на сети (нужен автомобиль), дозаказ оборудования, локальная заявка \
(ремонт у клиента). Сервис работает ТОЛЬКО по Москве и Московской области: карта и маршруты \
построены только для них. Адрес в другом регионе (Ростов, Новочеркасск, Санкт-Петербург, Тула, \
Тверь, Рязань и любой другой город вне Москвы и области) принять нельзя — заявку для него не \
предлагай, а объясни, что он вне зоны обслуживания. Инженеров добавлять и править ты не умеешь: \
это делают формы в карточках.

"""

INTERPRET = (
    PROJECT
    + """Ты помощник диспетчера выездных инженеров. По фразе диспетчера определи, чего он \
хочет, и верни JSON. Поле intent:
- engineer_unavailable — инженер выбыл (заболел, уехал, недоступен) и просьбы убрать самого инженера: \
«удали Семёнова», «убери Никитина», «сними Фролова» — это про инженера, а не про его заявки: нужен engineerId;
- cancel_request — клиент отменил заявки; только когда во фразе речь именно о заявках (слово «заявка» \
или номера): нужен requestIds — номера ВСЕХ подходящих заявок, одной или \
нескольких. Если заявки отбираются по месту («все заявки в Кашире», «в Домодедове») — в place \
напиши название места в именительном падеже («Кашира», «Домодедово»), а requestIds оставь пустым: \
заявки по месту отберёт программа, сам их не перечисляй. «Все заявки Никитина» — по инженеру в \
списке, в requestIds;
- defer_request — заявки переносят на следующий день: нужен requestIds, как у отмены;
- close_request — заявку закрывают по факту: нужны requestId и outcome (done — выполнена, \
cancelled — отменена клиентом, failed — выполнить невозможно); actualEnd — во сколько кончилась \
работа, если названо;
- add_request — появилась новая заявка: нужны requestType и address (адрес в Москве или Московской \
области; адрес вне них — не add_request, а unclear с объяснением в reply); windowStart и windowEnd — \
окно заявки, equipment — оборудование, если названо. Фраза, которая поправляет или дополняет только \
что предложенную новую заявку («нет, в городе …», «адрес: …», «с 14 до 16»), — не unclear и не новая \
заявка с нуля: возьми тип, окно и оборудование из прежнего предложения ассистента в переписке \
(«Новая заявка: Авария на ТКД, …, окно 14:00–16:00»), а адрес и поправки — из новой фразы;
- question — любой вопрос о плане, инженерах или заявках, менять ничего не нужно. Вопрос с «он», «у него», «из них», «эта» — тоже question: о ком речь, определят по прежней переписке на следующем шаге, поэтому не уточняй;
- help — диспетчер спрашивает, что ты умеешь и как с тобой работать;
- unclear — только команда изменить день, для которой не хватает данных (например, «отмени» без заявки); в reply коротко напиши, что уточнить. Вопрос никогда не unclear.
engineerId, requestId и requestIds бери строго из списков ниже; фамилии склоняются, ищи по основе слова. \
Короткий ответ диспетчера («все», «да», «эти») продолжает его прежнюю просьбу из переписки: \
«Отмени заявки в Кашире» — «Все» значит «отмени все заявки в Кашире». \
requestType: connection — подключение клиента, emergency — авария, order — дозаказ оборудования, \
local — локальная заявка (ремонт). Оборудование: router — роутер, set_top_box — приставка, \
alice — Алиса. time — время события в формате ЧЧ:ММ, только если диспетчер его назвал; голый час — \
тоже время: «после 13», «с 13», «в 13» — это «13:00», «с 9 утра» — «09:00», «в 15.30» — «15:30». \
wholeDay — true, если диспетчер просит пересчитать день с начала («с утра», «с начала дня», «заново \
весь день»), иначе false. Фраза, которая только уточняет прошлое предложение («с утра», «с начала \
дня», «на 15:00», «все», «да», «эти»), — не unclear: повтори то событие целиком, вместе с place или \
requestIds, если они были (кого или что — из прошлого \
предложения ассистента в переписке, оно записано как «Сделать недоступным: Фамилия с ЧЧ:ММ», \
«Отменить заявку №…», «Перенести заявку №…») и добавь уточнение. Пример: ассистент предложил \
«Сделать недоступным: Семёнов с 19:24», диспетчер пишет «с начала дня» — ответ: intent \
engineer_unavailable, engineerId — идентификатор Семёнова из списка инженеров (вида brigade-N, \
не фамилия), wholeDay true. \
Если во фразе «он», «у него», «эта заявка», «её» — бери, о ком шла речь в прежней переписке. \
Ничего не выдумывай: чего нет ни во фразе, ни в переписке, то в ответе — пустая строка \
(у equipment — пустой список), а не догадка."""
)

WHOLE_DAY = "\nПересчитать весь день с начала — как будто это было известно с утра."

# Про возможности отвечает не модель: по фактам плана этого не узнать, и она отвечала бы про смены.
HELP = """Я помощник диспетчера. Умею две вещи.

1. Менять день по вашей фразе — вы подтверждаете кнопкой, сам я ничего не применяю:
• «Соколов недоступен с 13:00» или «…с начала дня» — тогда весь день считается заново
• «Отмени заявку 74198» или «Перенеси 74198 на завтра»
• «Заявку 74198 выполнили в 12:30» (или «не смогли выполнить»)
• «Новая авария на Волгоградском проспекте 128 к5, нужен роутер» (адреса — только Москва и Московская область)

2. Отвечать на вопросы по плану: у кого какие заявки и во сколько, почему заявка назначена именно так или осталась без исполнителя, кто когда свободен. Отвечаю только по данным плана; я помню переписку, поэтому можно спрашивать «а у него?»."""

ANSWER = (
    PROJECT
    + """Ты помощник диспетчера выездных инженеров. Ответь на вопрос по-русски, коротко и \
по делу, только по приведённым фактам. Если фактов для ответа нет — так и скажи, ничего не \
выдумывай и сам ничего не пересчитывай. Не предлагай действий, которых из фактов не следует. Про свои возможности не рассуждай: \nесли спрашивают, что ты умеешь, скажи, что можешь отвечать на вопросы по плану и разбирать \nфразы про изменения дня."""
)


def handle(
    plan: Plan, text: str, time: str, history: list[AssistantTurn] | None = None
) -> AssistantReply:
    """Фраза диспетчера → ответ. Отказ и сбой Яндекса — `llm.AssistantOff` / `AssistantFailed`.

    `history` — прежняя переписка: без неё «какие у него заявки?» и «отмени её» не к кому
    отнести. Модель получает её текстом, а подробные факты о названных заявках и инженерах
    берутся и из неё тоже: иначе «он» понимала бы только модель, а не код, собирающий контекст.
    """
    history = history or []
    data = _json(
        llm.chat(INTERPRET, _interpret_prompt(plan, text, time, history), SCHEMA)
    )
    intent = data.get("intent")
    if intent == "question":
        answer = llm.chat(
            ANSWER,
            f"Факты:\n{_facts(plan, text, history)}\n\n{_dialogue(history)}Вопрос: {text}",
        )
        return AssistantReply(kind="answer", text=answer.strip())
    if intent == "help":
        return AssistantReply(kind="answer", text=HELP)
    if intent == "add_request":
        return _draft(data)
    if intent in EVENTS:
        reply = _event(plan, intent, data, time)
        if reply.kind == "event" and data.get("wholeDay") is True:
            reply = reply.model_copy(
                update={"mode": "full", "text": reply.text + WHOLE_DAY}
            )
        return reply
    return _clarify(
        _text(data.get("reply")) or "Не понял, что нужно сделать — переформулируйте"
    )


# --- разбор ответа модели -----------------------------------------------------


def _json(content: str) -> dict:
    """Ответ модели как словарь; пустой — если это не JSON (тогда `intent` не найдётся)."""
    # Модель любит обернуть ответ в ```json … ```: берём от первой скобки до последней.
    start, end = content.find("{"), content.rfind("}")
    try:
        data = json.loads(content[start : end + 1]) if start != -1 else None
    except ValueError:
        data = None
    return data if isinstance(data, dict) else {}


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _clock(value) -> str | None:
    """`13:00`, `9.30`, `13:00:00` не берём — только ЧЧ:ММ; остальное считаем «не названо»."""
    found = re.fullmatch(r"(\d{1,2})[:.](\d{2})", _text(value))
    if not found or int(found[1]) > 23 or int(found[2]) > 59:
        return None
    return f"{int(found[1]):02d}:{found[2]}"


def _clarify(text: str) -> AssistantReply:
    return AssistantReply(kind="clarify", text=text)


def _event(plan: Plan, intent: str, data: dict, time: str) -> AssistantReply:
    body: dict = {"type": intent, "time": _clock(data.get("time")) or time}
    if intent == "engineer_unavailable":
        engineer_id = _text(data.get("engineerId"))
        if engineer_id not in {engineer.id for engineer in plan.input.engineers}:
            return _clarify("Такого инженера в плане нет — назовите фамилию точнее")
        body["engineerId"] = engineer_id
    elif intent in ("cancel_request", "defer_request"):
        return _many(plan, intent, data, body["time"])
    else:
        # «№74198» и «74198» — одно и то же.
        request_id = _text(data.get("requestId")).lstrip("№# ")
        if request_id not in {request.id for request in plan.input.requests}:
            return _clarify("Такой заявки в плане нет — назовите её номер точнее")
        body["requestId"] = request_id
        if data.get("outcome") not in {outcome.value for outcome in Outcome}:
            return _clarify(
                "Чем кончилась работа: выполнена, отменена клиентом или выполнить невозможно?"
            )
        body["outcome"] = data["outcome"]
        if actual_end := _clock(data.get("actualEnd")):
            body["actualEnd"] = actual_end
    return _proposal(plan, body)


def _proposal(plan: Plan, body: dict, note: str = "") -> AssistantReply:
    """Событие из собранного тела: та же проверка, что у `POST /events`, и фраза для диспетчера."""
    try:
        event = _EVENT.validate_python(body)
    except ValidationError:
        return _clarify(
            "Не получилось собрать событие — уточните заявку, инженера и время"
        )
    return AssistantReply(
        kind="event",
        text=explain.event_text(event, plan.input) + note,
        event=event,
    )


def _many(plan: Plan, intent: str, data: dict, time: str) -> AssistantReply:
    """Отмена и перенос списка заявок (блок 42).

    Номера называет модель, а код оставляет из них то, что есть в плане и что вообще можно:
    начатую работу и закрытую факт отменять нельзя (PLAN 6.12), и одна такая в списке отклонила
    бы событие целиком. Оставшееся отправляется как есть, а отсеянное названо в подписи — иначе
    диспетчер решил бы, что отменено всё, о чём он просил.
    """
    place = _text(data.get("place"))
    if place:
        # Отбор по месту — дело кода, а не модели: массовое действие не должно зависеть от того,
        # что модель на этот раз сочла «Каширой» (московский проспект, «шоссе Каширское»). Место
        # ищется целым словом: адреса пишутся в именительном падеже, а «Каширское» — другое слово.
        word = re.compile(rf"(?<![\w-]){re.escape(place)}(?![\w-])", re.IGNORECASE)
        found = [r.id for r in plan.input.requests if word.search(r.address)]
        if not found:
            return _clarify(f"В адресах заявок нет места «{place}» — уточните название")
    else:
        said = [data.get("requestId"), *(data.get("requestIds") or [])]
        wanted = list(
            dict.fromkeys(
                str(item).strip().lstrip("№# ")
                for item in said
                if isinstance(item, str | int) and str(item).strip()
            )
        )
        known = {request.id for request in plan.input.requests}
        found = [item for item in wanted if item in known]
        if not found:
            return _clarify(
                "Не нашёл таких заявок в плане — назовите номера или город точнее"
            )

    now = to_seconds(time)
    started = {
        stop.request_id
        for route in plan.routes
        for stop in route.stops
        if to_seconds(stop.departure) <= now
    }
    skipped = [item for item in found if item in started or item in plan.closed]
    ids = [item for item in found if item not in skipped]
    action = "отменить" if intent == "cancel_request" else "перенести"
    if not ids:
        return _clarify(
            f"Нечего {action}: по этим заявкам работа уже началась или они закрыты — "
            + ", ".join(f"№{item}" for item in skipped)
        )
    note = (
        "Не трогаю — работа уже началась или заявка закрыта: "
        + ", ".join(f"№{item}" for item in skipped)
        if skipped
        else ""
    )
    return _proposal(
        plan,
        {"type": intent, "time": time, "requestIds": ids},
        f"\n{note}" if note else "",
    )


def _draft(data: dict) -> AssistantReply:
    address, kind = _text(data.get("address")), data.get("requestType")
    if not address or kind not in REQUEST_TYPE_NAMES:
        return _clarify(
            "Для новой заявки нужны адрес и тип: подключение, авария, дозаказ или локальная заявка"
        )
    start, end = _clock(data.get("windowStart")), _clock(data.get("windowEnd"))
    if not (start and end and start < end):
        start = end = (
            None  # половина окна хуже, чем окно по умолчанию, которое поставит форма
        )
    equipment = data.get("equipment")
    try:
        draft = AssistantDraft(
            request_type=kind,
            address=address,
            window_start=start,
            window_end=end,
            equipment=list(
                dict.fromkeys(
                    item
                    for item in (equipment if isinstance(equipment, list) else [])
                    if item in {e.value for e in Equipment}
                )
            ),
        )
    except ValidationError:
        return _clarify("Не получилось разобрать заявку — уточните адрес и тип")
    window = f", окно {start}–{end}" if start else ""
    return AssistantReply(
        kind="add_request",
        text=f"Новая заявка: {REQUEST_TYPE_NAMES[kind]}, {address}{window}",
        draft=draft,
    )


# --- что видит модель ---------------------------------------------------------


def _dialogue(history: list[AssistantTurn]) -> str:
    """Прежняя переписка для модели; пустая — пустая строка."""
    if not history:
        return ""
    lines = (
        f"{'Диспетчер' if turn.role == 'me' else 'Ассистент'}: {turn.text}"
        for turn in history
    )
    return "Прежняя переписка:\n" + "\n".join(lines) + "\n\n"


def _interpret_prompt(
    plan: Plan, text: str, time: str, history: list[AssistantTurn]
) -> str:
    engineers = "\n".join(f"{e.id} — {e.name}" for e in plan.input.engineers)
    names = {e.id: e.name for e in plan.input.engineers}
    # Инженер в строке заявки нужен для «отмени все заявки Никитина»: по адресам этого не выбрать.
    requests = "\n".join(
        f"{r.id} — {r.address}, окно {r.window_start}–{r.window_end}, "
        f"{names.get(plan.assignments.get(r.id) or '', 'не назначена')}"
        for r in plan.input.requests
    )
    return (
        f"Время дня: {time}\n\nИнженеры:\n{engineers}\n\nЗаявки:\n{requests}\n\n"
        f"{_dialogue(history)}Фраза диспетчера: {text}"
    )


def _mentions(text: str, name: str) -> bool:
    """Назван ли инженер в тексте. Фамилия склоняется («Соколова»), поэтому у одного слова
    берём основу без последней буквы; имя из нескольких слов («Бригада 3») — целиком."""
    low, word = text.lower(), name.lower()
    if " " in word:
        return re.search(rf"\b{re.escape(word)}\b", low) is not None
    return word[: max(len(word) - 1, 4)] in low


def _facts(plan: Plan, text: str, history: list[AssistantTurn] | None = None) -> str:
    """Факты для ответа: обзор всего дня и подробности о том, что названо в вопросе.

    Всё берётся из готового плана — расписание, причины и объяснения посчитаны без модели.
    Свободные окна инженеров лежат у кандидатов любого объяснения: они считаются от текущего
    маршрута бригады и одни и те же у всех заявок (PLAN 6.9).
    """
    engineers = plan.input.engineers
    routes = {route.engineer_id: route for route in plan.routes}
    stops = {stop.request_id: stop for route in plan.routes for stop in route.stops}
    names = {engineer.id: engineer.name for engineer in engineers}
    unassigned = {item.request_id: item for item in plan.unassigned}
    free: dict[str, str] = {}
    for explanation in list(plan.explanations.values())[:1]:
        for candidate in explanation.candidates:
            slots = ", ".join(f"{s.start}–{s.end}" for s in candidate.free_slots)
            free[candidate.engineer_id] = slots or "нет"

    lines = ["Инженеры:"]
    for engineer in engineers:
        route = routes.get(engineer.id)
        line = (
            f"{engineer.name} ({engineer.id}): смена {engineer.shift_start}–{engineer.shift_end}, "
            f"заявок {len(route.stops) if route else 0}, {route.km if route else 0} км"
        )
        if engineer.unavailable_from:
            line += f", недоступен с {engineer.unavailable_from}"
        if engineer.id in free:
            line += f", свободные окна: {free[engineer.id]}"
        lines.append(line)

    lines.append("Заявки:")
    for request in plan.input.requests:
        head = (
            f"№{request.id} ({WORK_PRIORITY_NAMES[request.work_priority]}) {request.address}, "
            f"окно {request.window_start}–{request.window_end}"
        )
        if request.id in unassigned:
            tail = f"не назначена: {unassigned[request.id].reason_text}"
        else:
            stop = stops.get(request.id)
            who = names.get(plan.assignments.get(request.id) or "", "?")
            tail = f"{who}, {stop.start}–{stop.end}" if stop else who
        status = plan.statuses.get(request.id)
        lines.append(
            f"{head}: {tail}" + (f" [{REQUEST_STATUS_NAMES[status]}]" if status else "")
        )

    # Названо в самом вопросе — раскрываем его; не названо («а какие у него заявки?») — берём то,
    # о чём шла речь в последних репликах, от новых к старым.
    asked: list[str] = []
    named: list[Engineer] = []
    for phrase in [text, *(turn.text for turn in reversed(history or []))]:
        asked = [
            r.id
            for r in plan.input.requests
            if re.search(rf"(?<!\d){r.id}(?!\d)", phrase)
        ]
        named = [e for e in engineers if _mentions(phrase, e.name) or e.id in phrase]
        if asked or named:
            break
    for request_id in asked[:DETAILED]:
        if explanation := plan.explanations.get(request_id):
            lines += ["", f"Подробно о заявке №{request_id}:", explanation.text]
    for engineer in named[:DETAILED]:
        lines += ["", f"Подробно об инженере {engineer.name}:"]
        route = routes.get(engineer.id)
        if route and route.stops:
            lines.append(
                "Маршрут: "
                + "; ".join(
                    f"№{s.request_id} {s.start}–{s.end}, переезд {s.travel_km} км"
                    for s in route.stops
                )
            )
        if details := plan.route_explanations.get(engineer.id):
            lines += [details.assignment, details.order]
    return "\n".join(lines)
