"""Модели сущностей и плана (PLAN 5.1, 5.2). Наружу JSON идёт в camelCase через alias."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    model_validator,
)
from pydantic.alias_generators import to_camel

from .dictionaries import (
    LAT_RANGE,
    LON_RANGE,
    Equipment,
    Outcome,
    RequestStatus,
    Skill,
    Tool,
    Transport,
    WorkPriority,
)
from .timeutil import to_seconds


class Model(BaseModel):
    """Общая база: snake_case внутри, camelCase наружу.

    `extra="forbid"`: опечатка в загружаемом файле (блок 12) должна падать, а не терять поле молча.
    """

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid"
    )


def check_clock(value: str) -> str:
    to_seconds(value)  # формат HH:MM; сам перевод делают уже в планировании
    return value


def check_unique(values: list) -> list:
    if len(set(values)) != len(values):
        raise ValueError("значения не должны повторяться")
    return values


# Координаты — только внутри вырезки OSM: точка за её пределами недостижима для Valhalla (PLAN 6.1).
Lat = Annotated[float, Field(ge=LAT_RANGE[0], le=LAT_RANGE[1])]
Lon = Annotated[float, Field(ge=LON_RANGE[0], le=LON_RANGE[1])]
Clock = Annotated[str, AfterValidator(check_clock)]
Equipments = Annotated[list[Equipment], AfterValidator(check_unique)]
Tools = Annotated[list[Tool], AfterValidator(check_unique)]


# --- вход: заявки, бригады, офис (PLAN 5.1) ----------------------------------


class Office(Model):
    """Стартовая точка всех бригад — адрес офиса из файла данных (Дополнения, п. 4)."""

    address: str = Field(min_length=1)
    lat: Lat
    lon: Lon


class Request(Model):
    id: str = Field(min_length=1)
    address: str = Field(min_length=1)
    lat: Lat
    lon: Lon
    service_duration_min: int = Field(gt=0)  # работа на объекте: норматив минус дорога
    base_norm_min: int = Field(gt=0)  # норматив из таблицы — для объяснений
    window_start: Clock
    window_end: Clock
    work_priority: WorkPriority
    skill: Skill
    required_transport: Transport | None = None
    required_equipment: Equipments = []
    required_tools: Tools = []  # инструменты: нужны на месте, но не расходуются
    # Заявка появилась в течение дня (PLAN 6.12). Это срочность, а не тип работы.
    urgent: bool = False
    # «Тип заявки HD» из выгрузки («Конвергенция абонента», «Нет линка»): подпись работы
    # в интерфейсе, в расчёте не участвует (PLAN 2.3).
    hd_type: str | None = None

    @model_validator(mode="after")
    def check_window(self):
        if to_seconds(self.window_start) >= to_seconds(self.window_end):
            raise ValueError("окно заявки: начало должно быть раньше конца")
        return self


class Engineer(Model):
    """Бригада — один человек (Дополнения, п. 1)."""

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    start_lat: Lat
    start_lon: Lon
    shift_start: Clock
    shift_end: Clock
    skills: Annotated[
        list[Skill], Field(min_length=1, max_length=3), AfterValidator(check_unique)
    ]
    transport: Transport
    equipment: Equipments = []
    tools: Tools = []
    # Состояние дня, а не свойство бригады: в наборе данных обоих полей нет, их ставит
    # событие и несёт `plan.input` (PLAN 6.12, 6.19). Без этого «вышла в 12:00» и «выбыла
    # в 13:00» жили бы только в памяти расчёта и терялись на следующем событии цепочки.
    available_from: Clock | None = None  # бригада вышла в течение дня
    unavailable_from: Clock | None = None  # бригада выбыла: новых работ не берёт

    @model_validator(mode="after")
    def check_shift(self):
        if to_seconds(self.shift_start) >= to_seconds(self.shift_end):
            raise ValueError("смена: начало должно быть раньше конца")
        return self


class Input(Model):
    """То, по чему считается план. Порядок заявок и бригад сохраняется — он нужен базовому варианту (PLAN 6.4)."""

    office: Office
    requests: list[Request]
    engineers: list[Engineer]

    @model_validator(mode="after")
    def check_ids(self):
        for title, items in (("заявок", self.requests), ("инженеров", self.engineers)):
            ids = [item.id for item in items]
            if len(set(ids)) != len(ids):
                raise ValueError(f"идентификаторы {title} не уникальны")
        return self


class Control(Model):
    """Справочная сводка контрольной выборки участка — факт того же дня (PLAN 2.1, 5.3).

    Не вход алгоритма: в `Input` её нет, поэтому ни в расчёт, ни в `plan.input` она не
    попадает. Показывается строкой сравнения под метриками (PLAN 6.10).
    """

    engineers: int = Field(ge=0)  # сколько бригад работало в фактическом дне
    # Сколько заявок было в дне: выгрузка — снимок, часть заявок в ней ещё не закрыта,
    # и без общего числа «выполнено 36» читалось бы как итог дня.
    total: int = Field(ge=0)
    done: int = Field(ge=0)  # «Выполнена»
    cancelled: int = Field(ge=0)  # «Отменена»
    overdue: int = Field(ge=0)  # «Просрочена»
    per_engineer: float = Field(ge=0)  # выполнено на одну бригаду


class Dataset(Input):
    """Набор данных целиком: документ коллекции `datasets` (PLAN 5.3)."""

    id: str
    name: str = Field(min_length=1)
    built_in: bool = False  # встроенный или демо-набор, а не загруженный файл
    created_at: datetime
    # Последняя правка набора диспетчером (PLAN 6.19). Текущим набор остаётся по createdAt:
    # правка не должна поднимать его над загруженным позже.
    updated_at: datetime | None = None
    # Факт того же дня — только у встроенного набора: у загруженного контрольной выборки нет.
    control: Control | None = None


# --- демо-наборы: заявки и инженеры по отдельности (PLAN 6.20) ----------------


class DemoRequests(Model):
    """Заявки участка: офис и заявки одного из файлов кейса."""

    id: str
    name: str
    count: int


class DemoCrew(Model):
    """Состав инженеров: навыки и транспорт — для списка свойств в окне «Новый датасет»."""

    id: str
    name: str
    count: int
    skills: list[Skill]
    transports: list[Transport]


class DemoCatalog(Model):
    requests: list[DemoRequests]
    crews: list[DemoCrew]


class DemoChoice(Model):
    requests: str
    crew: str


# --- события (PLAN 5.1, 6.12) ------------------------------------------------


class AddRequestEvent(Model):
    """Заявка поступила в течение дня (PLAN 6.19).

    `urgent_request` — прежнее имя того же события: оно осталось принимаемым, чтобы готовые
    сценарии и примеры из README не перестали работать. Без `urgent` срочной считается
    только авария: обычная заявка, пришедшая днём, встраивается в свободное время и
    приоритета над уже обещанными работами не получает (ответы организаторов, PLAN 6.12).
    У прежнего имени срочность — всегда: его имя само это говорит.
    """

    type: Literal["add_request", "urgent_request"]
    time: Clock
    request: Request
    urgent: bool | None = None


class AddEngineerEvent(Model):
    """Бригада вышла в течение дня: `availableFrom` = время события, старт — её адрес."""

    type: Literal["add_engineer"]
    time: Clock
    engineer: Engineer


class UpdateRequestEvent(Model):
    """Заявку поправили в течение дня (PLAN 6.19): адрес, окно, оборудование, тип работ.

    Приходит целиком, как при добавлении. Времён стопов в ней нет и быть не может — они
    вычисляются расписанием (PLAN 6.2), диспетчер правит только исходные поля.
    """

    type: Literal["update_request"]
    time: Clock
    request: Request


class UpdateEngineerEvent(Model):
    """Бригаду поправили в течение дня (PLAN 6.19): смена, навыки, транспорт, старт.

    `availableFrom` и `unavailableFrom` из тела не читаются: это состояние дня, а не
    свойство бригады, и берётся оно у родителя (PLAN 6.12).
    """

    type: Literal["update_engineer"]
    time: Clock
    engineer: Engineer


class RequestIds(Model):
    """События над списком заявок: отмена и перенос (PLAN 6.12, блок 42).

    Список, а не одна заявка: «отмени все заявки в Кашире» — это одна правка дня, один пересчёт
    и одна версия плана, а не двадцать цепочкой. Событие применяется целиком или не применяется
    вовсе: одна начатая заявка в списке отклоняет его, и ничего не меняется.

    Прежний вид — одна заявка в `requestId` — остаётся принимаемым: такие события лежат в
    сохранённых планах (планы неизменяемы) и в файлах сценариев, а `curl` из README их шлёт.
    """

    request_ids: Annotated[list[str], Field(min_length=1), AfterValidator(check_unique)]

    @model_validator(mode="before")
    @classmethod
    def legacy_single(cls, data):
        if isinstance(data, dict) and not {"requestIds", "request_ids"} & data.keys():
            for key in ("requestId", "request_id"):
                if key in data:
                    rest = {k: v for k, v in data.items() if k != key}
                    return {**rest, "requestIds": [data[key]]}
        return data


class DeferRequestEvent(RequestIds):
    """Диспетчер переносит заявки на следующий день (PLAN 6.19, `Дополнения`, п. 11).

    Отличается от отмены: заявка никуда не делась, её просто не делают сегодня. Она остаётся
    в дне списком неназначенных с пометкой переноса, но в расчёт остатка не входит.
    """

    type: Literal["defer_request"]
    time: Clock


class CancelRequestEvent(RequestIds):
    """Клиент отменил заявки до начала работы (PLAN 6.19).

    Начатую работу этим событием не отменить — ответ 409: то, что бригада уже делает,
    закрывают фактом (`close_request` с исходом `cancelled`), а не отменой задним числом.
    """

    type: Literal["cancel_request"]
    time: Clock


class CloseRequestEvent(Model):
    """Диспетчер фиксирует факт со слов бригады (PLAN 6.19, ответ 1).

    `actualEnd` — когда работа действительно кончилась; времена стопа он не правит, их
    считает расписание (PLAN 6.2). Без него фактом считается время самого события.
    """

    type: Literal["close_request"]
    time: Clock
    request_id: str
    outcome: Outcome
    actual_end: Clock | None = None

    def at(self) -> str:
        """Время факта: `actualEnd`, если диспетчер его назвал, иначе момент события."""
        return self.actual_end or self.time


class EngineerUnavailableEvent(Model):
    type: Literal["engineer_unavailable"]
    time: Clock
    engineer_id: str


Event = Annotated[
    AddRequestEvent
    | AddEngineerEvent
    | UpdateRequestEvent
    | UpdateEngineerEvent
    | DeferRequestEvent
    | CancelRequestEvent
    | CloseRequestEvent
    | EngineerUnavailableEvent,
    Field(discriminator="type"),
]


# --- тела запросов правки набора (PLAN 5.4, 6.19) ----------------------------


class NewRequest(Request):
    """Заявка, добавляемая в набор: координаты необязательны — найдёт геокодер по адресу."""

    lat: Lat | None = None
    lon: Lon | None = None


class NewEngineer(Engineer):
    """Бригада, добавляемая в набор: пустой старт означает офис."""

    start_lat: Lat | None = None
    start_lon: Lon | None = None


# --- ассистент диспетчера (PLAN, блок 42) ------------------------------------


class AssistantDraft(Model):
    """Заявка из текста диспетчера: то, что модель смогла назвать.

    Координат и норматива здесь нет: таблица «тип → норматив, навык, инструменты» живёт на
    фронте (`REQUEST_TYPES`), а координаты берёт форма — ключ 2ГИС на разбор текста не тратим.
    """

    request_type: Literal[
        "connection", "emergency", "order", "local"
    ]  # REQUEST_TYPE_NAMES
    address: str = Field(min_length=1)
    window_start: Clock | None = None
    window_end: Clock | None = None
    equipment: Equipments = []


class AssistantTurn(Model):
    """Реплика прежней переписки: без неё «какие у него заявки?» не к кому отнести."""

    role: Literal["me", "bot"]
    text: str = Field(max_length=1000)


class AssistantReply(Model):
    """Ответ ассистента. Событие и черновик — только предложение: применяет их диспетчер.

    `event` — готовое событие (проверено той же моделью, что и `POST /events`), `draft` — заявка
    для формы добавления, `answer` — ответ на вопрос по плану, `clarify` — не разобрали, чего
    хочет диспетчер, и `text` говорит, что уточнить.
    """

    kind: Literal["event", "add_request", "answer", "clarify"]
    text: str
    event: Event | None = None
    draft: AssistantDraft | None = None
    # `full` — диспетчер просит пересчитать день с начала («с утра», «с начала дня»): режим
    # пересчёта события (PLAN 6.12). Без него сервер выбирает режим сам.
    mode: Literal["full"] | None = None


# --- план (PLAN 5.2) ---------------------------------------------------------


class Algorithm(StrEnum):
    BASELINE = "baseline"
    OPTIMIZED = "optimized"
    MANUAL = "manual"


class Variant(StrEnum):
    """Настройки расчёта (PLAN 6.14)."""

    BASELINE = "baseline"
    EMERGENCY_FIRST = "emergency_first"  # основная модель: OBJECTIVE_MODE по умолчанию
    MAX_REQUESTS = "max_requests"
    URGENT_FIRST = "urgent_first"
    PRIORITY_FIRST = "priority_first"
    FAST = "fast"


class ReplanMode(StrEnum):
    FROM_EVENT = "from_event"
    FULL = "full"
    # Обычная заявка днём: встать в свободное время, остальной план не трогать (PLAN 6.12).
    INSERT = "insert"


class Stop(Model):
    request_id: str
    departure: Clock
    arrival: Clock
    start: Clock
    end: Clock
    travel_km: float = Field(ge=0)
    travel_min: int = Field(ge=0)
    # Работа выполнена, идёт или бригада уже выехала (PLAN 6.12).
    committed: bool = False


class Route(Model):
    engineer_id: str
    km: float = Field(ge=0)
    geometry: list[list[float]] = []  # [lon, lat] — порядок карты 2ГИС
    stops: list[Stop] = []


class Unassigned(Model):
    request_id: str
    # PLAN 6.8: NO_SKILL, NO_TRANSPORT, NO_EQUIPMENT (и инструменты), NO_CAPACITY, NO_ROUTE,
    # NO_TIME, NOT_FITTED, DEFERRED
    reason_code: str
    reason_text: str
    defer_next_day: bool = True  # «требуется перенос на следующий день»


class Closure(Model):
    """Факт по заявке: чем кончилось, когда и у кого (PLAN 6.19).

    Бригада хранится в самой записи: без неё независимая проверка не смогла бы сверить
    «закрытая заявка осталась за своей бригадой и не переназначена» — сверять было бы не с чем.
    """

    outcome: Outcome
    time: Clock
    engineer_id: str


class Slot(Model):
    """Промежуток, когда бригада свободна (PLAN 6.9)."""

    start: Clock
    end: Clock


class Candidate(Model):
    """Бригада в объяснении: что подошло, а что нет (PLAN 6.9)."""

    engineer_id: str
    skill_ok: bool
    transport_ok: bool
    equipment_ok: bool
    time_ok: bool
    extra_km: float | None = None  # прирост пробега, если вставка возможна
    free_slots: list[Slot] = []  # когда бригада свободна в текущем плане


class Explanation(Model):
    text: str
    candidates: list[Candidate] = []


class RouteExplanation(Model):
    """Два ответа про бригаду (PLAN 6.9): почему у неё эти заявки и почему в таком порядке."""

    assignment: str = ""
    order: str


def legacy_route_explanation(value):
    """До блока 29 здесь была одна строка — «почему маршрут такой». Планы неизменяемы и
    лежат в базе как есть, поэтому строка читается как `order` с пустым `assignment`:
    иначе прежняя версия не открылась бы и событие от неё дало бы 500."""
    return {"order": value} if isinstance(value, str) else value


class Metrics(Model):
    engineers_used: int = 0
    total_km: float = 0
    km_by_engineer: dict[str, float] = {}
    assigned_count: int = 0
    unassigned_count: int = 0
    # Разбивка по типам работ и по срочности — и у назначенных, и у неназначенных (PLAN 6.10).
    assigned_by_work_priority: dict[WorkPriority, int] = {}
    unassigned_by_work_priority: dict[WorkPriority, int] = {}
    assigned_urgent: int = 0
    unassigned_urgent: int = 0
    deferred_count: int = 0  # перенесено диспетчером на следующий день (PLAN 6.10)
    # Сколько заявок закрыто фактом и с каким исходом (PLAN 6.10, 6.19). Все три исхода
    # всегда на месте, отсутствующие нулями — так их и сверяет независимая проверка.
    closed_by_outcome: dict[Outcome, int] = {}
    travel_min: int = 0
    wait_min: int = 0
    work_min: int = 0
    utilization_by_engineer: dict[str, float] = {}
    expected_late: float = 0


class Validation(Model):
    """Результат независимой проверки плана (PLAN 6.7)."""

    ok: bool
    errors: list[str] = []


# --- прогноз опозданий (PLAN 5.2, 6.18) --------------------------------------


class Risk(StrEnum):
    """Уровень риска опоздания: < 10 %, 10–30 %, > 30 % (PLAN 6.18)."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RequestForecast(Model):
    late_probability: float = Field(ge=0, le=1)
    # Время, позже которого начинают только 10 % прогонов. Не `Clock`: прогон — не план,
    # в нём работа может уехать и за полночь, и «24:10» здесь законный ответ (PLAN 6.18).
    p90_start: str
    risk: Risk


class EngineerForecast(Model):
    over_shift_probability: float = Field(ge=0, le=1)


class Forecast(Model):
    """Модельная оценка: отклонения заданы нами, а не получены из истории (PLAN 6.18).

    Заявки — только те, что бригаде ещё предстоят: у закреплённого стопа работа уже идёт
    или бригада к нему выехала, и отклонять там нечего.
    """

    requests: dict[str, RequestForecast] = {}
    engineers: dict[str, EngineerForecast] = {}


# --- изменения плана (PLAN 5.5) ----------------------------------------------


class Change(StrEnum):
    """Что случилось с заявкой между версиями плана (PLAN 6.13)."""

    REASSIGNED = "reassigned"
    TIME_CHANGED = "time_changed"
    ORDER_CHANGED = "order_changed"
    ADDED = "added"
    CANCELLED = "cancelled"
    DEFERRED = "deferred"
    BECAME_UNASSIGNED = "became_unassigned"


class RequestChange(Model):
    """Одно изменение по заявке. Заполнены поля того вида изменения, который в `change`."""

    request_id: str
    change: Change
    # «Была у brigade-1, стала у brigade-4». `from: null` — заявка была неназначенной:
    # отдельного вида «стала назначенной» в PLAN 5.5 нет, и он не нужен.
    from_engineer: str | None = Field(default=None, alias="from")
    to_engineer: str | None = Field(default=None, alias="to")
    old_start: Clock | None = None
    new_start: Clock | None = None
    engineer_id: str | None = None
    old_position: int | None = None
    new_position: int | None = None


class EngineerChange(Model):
    engineer_id: str
    old_km: float
    new_km: float
    old_count: int
    new_count: int


class MetricsDiff(Model):
    """Метрики плана было/стало: `[старое, новое]` (PLAN 5.5)."""

    engineers_used: list[int]
    total_km: list[float]
    unassigned_count: list[int]


class Diff(Model):
    requests: list[RequestChange] = []
    engineers: list[EngineerChange] = []
    metrics: MetricsDiff


class Plan(Model):
    id: str
    parent_plan_id: str | None = None  # планы неизменяемы: событие создаёт новую версию
    dataset_id: str
    algorithm: Algorithm
    variant: Variant | None = None
    compare_group_id: str | None = None
    # Сколько секунд занял расчёт этого плана: столбец сводной таблицы вариантов (PLAN 6.14).
    # Лимит поиска настенный, поэтому это замер, а не свойство алгоритма.
    compute_sec: float = 0
    event: Event | None = None
    replan_mode: ReplanMode | None = None
    input: Input
    routes: list[Route] = []
    assignments: dict[str, str | None] = {}
    pinned: dict[str, str] = {}  # ручные назначения (PLAN 6.16)
    # Заявка → время, когда диспетчер перенёс её на следующий день (PLAN 6.19). Признак
    # живёт в плане и наследуется цепочкой событий: перенесённую работу день не возвращает.
    deferred: dict[str, Clock] = {}
    # Заявка → факт от диспетчера (PLAN 6.19). Закрытая заявка закреплена за своей бригадой
    # и в перепланирование не входит ни в одном режиме; признак наследуется цепочкой событий.
    closed: dict[str, Closure] = {}
    # Заявка → где она на момент события. Вычисляется, а не хранится (PLAN 6.19): у
    # неназначенной заявки статуса нет — её состояние описывает причина в `unassigned`.
    statuses: dict[str, RequestStatus] = {}
    unassigned: list[Unassigned] = []
    explanations: dict[str, Explanation] = {}
    route_explanations: dict[
        str, Annotated[RouteExplanation, BeforeValidator(legacy_route_explanation)]
    ] = {}
    metrics: Metrics = Metrics()
    validation: Validation | None = None
    forecast: Forecast | None = None  # прогноз опозданий (PLAN 6.18)
    approximate: bool = False  # расстояния считались по прямой: Valhalla не ответила
    # Текст оповещения к флагу: чьим километрам и временам верить нельзя (PLAN 5.6).
    approximate_note: str | None = None
    fallback_to_baseline: bool = False  # вернули базовый вариант (PLAN 6.6)
    diff: Diff | None = None  # что изменилось по сравнению с родителем (PLAN 6.13)
    created_at: datetime


# --- сценарии и история (PLAN 5.3, 6.17) -------------------------------------


class Scenario(Model):
    """Готовая цепочка событий: документ коллекции `scenarios` (PLAN 5.3).

    В файле `data/scenarios/*.json` лежат только `name` и `events`: сценарий ссылается на
    конкретные заявки и бригады, то есть осмыслен ровно для одного набора, и `datasetId`
    с `id` проставляет загрузчик (`seed.load_scenarios`). Держать их ещё и в файле — лишний
    повод разъехаться.
    """

    id: str
    dataset_id: str
    name: str = Field(min_length=1)
    events: Annotated[list[Event], Field(min_length=1)]


class PlanVersion(Model):
    """Строка истории: версия плана без тяжёлых полей (PLAN 6.17).

    Документ плана несёт весь вход, объяснение на каждую заявку и геометрию маршрутов —
    цепочка из пяти версий встроенного набора это мегабайты. Истории нужны событие, режим
    и метрики, поэтому читается она проекцией. Полный план берётся `GET /api/plans/{id}`.
    """

    id: str
    parent_plan_id: str | None = None
    algorithm: Algorithm
    variant: Variant | None = None
    event: Event | None = None
    replan_mode: ReplanMode | None = None
    metrics: Metrics = Metrics()
    compute_sec: float = 0
    created_at: datetime


class History(Model):
    """Цепочка версий плана и сравнение с исходным (PLAN 6.17).

    `versions` — от первой версии к запрошенной. `diff` — «Сравнить с исходным»: изменения
    первой версии к запрошенной, а не к родителю, как `plan.diff`. У первичного плана
    версия одна и сравнивать не с чем — тогда `null`.
    """

    versions: list[PlanVersion] = []
    diff: Diff | None = None
