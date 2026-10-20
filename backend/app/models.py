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
    field_validator,
    model_validator,
)
from pydantic.alias_generators import to_camel

from .dictionaries import (
    LAT_RANGE,
    LON_RANGE,
    LUNCH_MIN,
    LUNCH_WINDOW,
    Access,
    BuildingType,
    DurationMode,
    Equipment,
    Outcome,
    RequestStatus,
    ReviewSource,
    ReviewTag,
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


class RequestObject(Model):
    """Объект заявки: то, от чего зависит сложность работы (PLAN 9.1).

    Все поля необязательны: незаполненное — «не знаем», и добавки за него нет. `flats` —
    квартир в подъезде (блок 78); без значения — «?» в форме: сколько квартир, диспетчер не
    всегда знает. Подъезд и номер квартиры — куда идти инженеру, в расчёте не участвуют.
    """

    building_type: BuildingType = BuildingType.APARTMENT
    floor: int | None = Field(None, ge=1)
    floors: int | None = Field(None, ge=1)
    entrance: int | None = Field(None, ge=1)
    flat_number: int | None = Field(None, ge=1)
    flats: int | None = Field(None, ge=1)
    elevator: bool = False
    access: Access = Access.NORMAL

    @field_validator("building_type", mode="before")
    @classmethod
    def _no_new_building(cls, value):
        """«Новостройки» больше нет (блок 78): планы и файлы до него читаются как
        многоквартирный дом, а не падают на проверке."""
        return BuildingType.APARTMENT if value == "new_building" else value


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
    # Сложность объекта (PLAN 9.1): в режиме `complexity` меняет длительность работы, в
    # `norm` не участвует. Умолчания — у планов, посчитанных до блока 66.
    object: RequestObject | None = None
    gigabit: bool = False
    repeat_visit: bool = False
    # Условия работы окна «Условия работы» (блок 78): каждое удлиняет работу.
    cable: bool = False
    drilling: bool = False
    # Документы из норматива (10 / 0 / 10 / 0 мин): в `serviceDurationMin` они уже входят, но
    # на сложность не умножаются.
    documents_min: int = Field(0, ge=0)

    @model_validator(mode="after")
    def check_window(self):
        if to_seconds(self.window_start) >= to_seconds(self.window_end):
            raise ValueError("окно заявки: начало должно быть раньше конца")
        if self.documents_min >= self.service_duration_min:
            raise ValueError("документы должны быть короче работы на объекте")
        return self


class Rating(Model):
    """Рейтинг инженера по отзывам клиентов (PLAN 9.1): средняя оценка и число отзывов."""

    stars: float = Field(ge=1, le=5)
    count: int = Field(ge=1)


class Home(Model):
    """Дом инженера (PLAN 9.1, блок 73): адрес текстом и оборудование, которое лежит дома.

    Старт по-прежнему задают `startLat/startLon`: «начало дня из дома» — это старт в точке
    дома, отдельного флага нет.
    """

    address: str = Field(min_length=1)
    lat: Lat
    lon: Lon
    equipment: Equipments = []


class Dispatch(Model):
    """Бригаду отправили на аварию (блок 74): состояние дня, как `availableFrom`.

    В `time` бригада получила аварию `emergencyId` и выезжает к ней не раньше этого момента.
    Если её развернули в пути, `lat/lon` — где она стояла на плече к заявке `requestId`,
    проехав по нему `km`; у «доделает и поедет» и «поедет сразу» этих полей нет. Авария —
    якорь: перед её стопом маршрут переходит в точку разворота. По временам место в маршруте
    не найти: факты двигают концы работ.
    """

    time: Clock
    emergency_id: str
    lat: Lat | None = None
    lon: Lon | None = None
    km: float | None = Field(None, ge=0)
    request_id: str | None = None


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
    # Скорость работы на начало дня, в тысячных (PLAN 9.1): 1120 — на 12 % медленнее нормы.
    # В режиме `complexity` умножает работу на объекте; в течение дня не меняется.
    speed: int = Field(1000, ge=500, le=2000)
    available_from: Clock | None = None  # бригада вышла в течение дня
    unavailable_from: Clock | None = None  # бригада выбыла: новых работ не берёт
    # Снимок рейтинга на момент расчёта (PLAN 9.1, блок 70): ставит `reviews.snapshot` до
    # расчёта, в течение дня не меняется. Нет отзывов — `None`, как у новичка.
    rating: Rating | None = None
    # Из резерва (PLAN 9.1, блок 71): человек вне набора, которого берёт только «Рейтинг в
    # приоритете». В набор не входит — его добавляет расчёт, и он едет в `plan.input`.
    reserve: bool = False
    home: Home | None = None  # у загруженного набора дома может не быть
    # Выезды на аварию за день, с разворотом и без (блок 74). Список: вторая авария может
    # достаться тому же человеку, и одно поле затёрло бы первую вместе с пересчётом сделанного.
    dispatched: list[Dispatch] = []
    # Окно обеда (блок 79): обед — час внутри него, между работами. Умолчание — общее окно,
    # оно же у наборов и планов до блока 79.
    lunch_start: Clock = LUNCH_WINDOW[0]
    lunch_end: Clock = LUNCH_WINDOW[1]

    @model_validator(mode="after")
    def check_shift(self):
        if to_seconds(self.shift_start) >= to_seconds(self.shift_end):
            raise ValueError("смена: начало должно быть раньше конца")
        if to_seconds(self.lunch_end) - to_seconds(self.lunch_start) < LUNCH_MIN * 60:
            raise ValueError(f"окно обеда: не короче {LUNCH_MIN} минут")
        return self


class Input(Model):
    """То, по чему считается план. Порядок заявок и бригад сохраняется — он нужен базовому варианту (PLAN 6.4)."""

    office: Office
    requests: list[Request]
    engineers: list[Engineer]
    # Длительность работы — по нормативу или с учётом сложности и скорости (PLAN 9.1). Едет в
    # `plan.input` и в отпечаток кэша решений. Умолчание `norm` — у планов до блока 66.
    duration_mode: DurationMode = DurationMode.NORM

    @model_validator(mode="after")
    def check_ids(self):
        for title, items in (("заявок", self.requests), ("инженеров", self.engineers)):
            ids = [item.id for item in items]
            if len(set(ids)) != len(ids):
                raise ValueError(f"идентификаторы {title} не уникальны")
        return self


class ControlRun(Model):
    """День людей: распределение контрольного дня нашим расписанием при мягких допущениях
    (PLAN 6.10, блок 51, `planning/control.py`)."""

    # Бригад с заявками дня людей: бригада, у которой были только отменённые, сюда не входит.
    engineers: int = Field(ge=0)
    assigned: int = Field(ge=0)  # заявок с бригадой, кроме отменённых
    km: float = Field(ge=0)  # переезды между заявками; дорога до первой не считается
    late: int = Field(ge=0)  # начало позже конца окна (ТЗ, п. 2.2)
    over_shift: int = Field(ge=0)  # бригад, закончивших позже 23:00


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
    run: ControlRun


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
    # Учебный состав: людей заведомо меньше, чем заявок, — показать отказы и «Что поможет».
    training: bool = False


class DemoCatalog(Model):
    requests: list[DemoRequests]
    crews: list[DemoCrew]


class DemoChoice(Model):
    requests: str
    crew: str
    # Демо-наборы по умолчанию считаются со сложностью (PLAN 9.1): коэффициент виден сразу.
    duration_mode: DurationMode = DurationMode.COMPLEXITY


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
    # Кого диспетчер отправил на аварию (блок 74, «Кто поедет на аварию»). Без него едет тот,
    # кто прибудет раньше всех. Только у аварии.
    engineer_id: str | None = None


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
    сохранённых планах (планы неизменяемы), а `curl` из README их шлёт.
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


class ClockClosure(Model):
    """Работа, которую часы дня застали законченной: когда она кончилась на самом деле."""

    request_id: str
    actual_end: Clock


class ClockEvent(Model):
    """Часы дня (блок 68): что сервер узнал о начатых работах к `time`.

    Событие собирает `POST /api/plans/{id}/clock`, а не диспетчер: `closed` — работы, чья
    скрытая длительность истекла (исход «выполнена»), `running` — затянувшиеся сверх плана,
    из-за которых следующая заявка инженера стала недопустимой. Пересчитывается режимом
    `delay`: остаток дня не переоптимизируется, только сдвигается и чинится.
    """

    type: Literal["clock"]
    time: Clock
    closed: list[ClockClosure] = []
    running: list[str] = []


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
    | EngineerUnavailableEvent
    | ClockEvent,
    Field(discriminator="type"),
]


# --- тела запросов правки набора (PLAN 5.4, 6.19) ----------------------------


class NewRequest(Request):
    """Заявка, добавляемая в набор: координаты необязательны — найдёт геокодер по адресу."""

    lat: Lat | None = None
    lon: Lon | None = None


# --- длительность по объекту для формы (PLAN 9.1) ---------------------------


class DurationQuery(Model):
    """Поля заявки, от которых зависит длительность; `speed` — инженера, без него норма."""

    service_duration_min: int = Field(gt=0)
    documents_min: int = Field(0, ge=0)
    object: RequestObject | None = None
    gigabit: bool = False
    repeat_visit: bool = False
    cable: bool = False
    drilling: bool = False
    required_equipment: Equipments = []
    speed: int = Field(1000, ge=500, le=2000)


class DurationReason(Model):
    """Строка «Почему будет выполняться дольше?» (блок 78): «+10 мин — гигабитное подключение»
    и пояснение серым."""

    minutes: int
    text: str
    hint: str


class DurationAnswer(Model):
    norm_min: int  # работа на объекте по нормативу
    minutes: int  # с учётом сложности и скорости
    work_min: int
    documents_min: int
    access_min: int
    complexity: float  # ×1,2
    level: str  # ключ `planning.duration.LEVELS`
    reasons: list[
        DurationReason
    ]  # добавки, подъезд к дому и скорость; сумма — `minutes − normMin`


class NewEngineer(Engineer):
    """Бригада, добавляемая в набор: пустой старт означает офис."""

    start_lat: Lat | None = None
    start_lon: Lon | None = None


# --- ассистент диспетчера (PLAN, блок 42) ------------------------------------


# Тип заявки, который называет ассистент (блоки 42, 59): ключи `REQUEST_TYPE_NAMES`.
RequestTypeCode = Literal["connection", "emergency", "order", "local"]


class AssistantDraft(Model):
    """Заявка из текста диспетчера: то, что модель смогла назвать.

    Координат и норматива здесь нет: таблица «тип → норматив, навык, инструменты» живёт на
    фронте (`REQUEST_TYPES`), а координаты берёт форма — ключ 2ГИС на разбор текста не тратим.
    """

    request_type: RequestTypeCode
    address: str = Field(min_length=1)
    window_start: Clock | None = None
    window_end: Clock | None = None
    equipment: Equipments = []


class AssistantEngineerDraft(Model):
    """Новый инженер из текста диспетчера (блок 59): то, что модель смогла назвать.

    Событием он не уходит — во фразе почти никогда нет смены, оборудования и инструментов, —
    а открывает форму «Добавление инженера»: недостающее заполнит и покажет она.
    """

    name: str | None = None
    shift_start: Clock | None = None
    shift_end: Clock | None = None
    skills: list[Skill] = []
    transport: Transport | None = None
    equipment: Equipments = []
    tools: Tools = []


class AssistantEdit(Model):
    """Правка заявки, которую доделывает её карточка (блок 59): адрес нужно найти геокодером, а
    тип тянет навык, норматив и инструменты — таблица типов живёт на фронте (`REQUEST_TYPES`).

    `request` — заявка с правками, которые ассистент разобрал сам; карточка кладёт поверх них
    `address` и `request_type`.
    """

    request: Request
    request_type: RequestTypeCode | None = None
    address: str | None = None


# Реплика истории обрезается, а не отклоняется: длинный ответ ассистента (список заявок) иначе
# давал 422 на каждый следующий запрос, пока он оставался в истории. Модели из переписки нужно
# понять, о ком речь, — для этого хватает начала реплики.
HISTORY_TEXT = 3000


# Сколько вложений ассистент берёт в один запрос (блок 60): все они едут к модели вместе с планом.
ATTACHMENTS = 3


class AssistantTurn(Model):
    """Реплика прежней переписки: без неё «какие у него заявки?» не к кому отнести.

    `attachments` — id вложений реплики (блок 60): в истории едут они, а не содержимое, поэтому
    «а сколько из них в Кашире?» следующей репликой видит тот же файл.
    """

    role: Literal["me", "bot"]
    text: Annotated[
        str,
        BeforeValidator(lambda v: v[:HISTORY_TEXT] if isinstance(v, str) else v),
    ]
    attachments: list[str] = Field(default=[], max_length=ATTACHMENTS)


class AssistantAttachment(Model):
    """Принятое вложение ассистента (блок 60): что показать чипом в чате."""

    id: str
    name: str
    kind: Literal["csv", "json", "txt"]
    summary: str


class AssistantReassign(Model):
    """Переназначение из фразы ассистента (блок 61): в лучшее место маршрута, как «Переназначить»."""

    request_id: str
    engineer_id: str


class AssistantReply(Model):
    """Ответ ассистента. Событие и черновик — только предложение: применяет их диспетчер.

    `event` — готовое событие (проверено той же моделью, что и `POST /events`), `draft` — заявка
    для формы добавления, `engineer` — инженер для формы добавления, `edit` — правка для
    карточки, `time` — переход ко времени дня, `answer` — ответ на вопрос по плану, `clarify` —
    не разобрали, чего хочет диспетчер, и `text` говорит, что уточнить.
    """

    kind: Literal[
        "event",
        "add_request",
        "add_engineer",
        "edit_request",
        "time",
        "reassign",
        "answer",
        "clarify",
    ]
    text: str
    event: Event | None = None
    draft: AssistantDraft | None = None
    engineer: AssistantEngineerDraft | None = None
    edit: AssistantEdit | None = None
    # `kind: time` — время дня, на которое перейти; `null` — вернуться к текущему (блок 59).
    time: Clock | None = None
    reassign: AssistantReassign | None = None
    # «Что будет, если…» (блок 61): событие или переназначение уже посчитано черновиком
    # `draft_plan_id`, текст — что изменится; «Применить» делает черновик версией (`commit`).
    what_if: bool = False
    draft_plan_id: str | None = None
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
    EMERGENCY_ASAP = "emergency_asap"  # она же поэтапно, аварии вперёд (блок 49)
    MAX_REQUESTS = "max_requests"
    URGENT_FIRST = "urgent_first"
    PRIORITY_FIRST = "priority_first"
    RATING_FIRST = (
        "rating_first"  # больше работы лучшим по рейтингу, с резервом (блок 71)
    )
    FAST = "fast"


class ReplanMode(StrEnum):
    FROM_EVENT = "from_event"
    FULL = "full"
    # Обычная заявка днём: встать в свободное время, остальной план не трогать (PLAN 6.12).
    INSERT = "insert"
    # Часы дня (блок 68): факты закрытия и задержки; остаток сдвигается, при конфликте —
    # перестановка у того же инженера, передача другому или «не удалось после задержки».
    DELAY = "delay"


class Stop(Model):
    request_id: str
    departure: Clock
    arrival: Clock
    start: Clock
    end: Clock
    travel_km: float = Field(ge=0)
    travel_min: int = Field(ge=0)
    # Расчётная работа пары по скорости на начало дня (блок 69): у закрытой фактом и
    # затянувшейся работы `end` — факт, а план — здесь. `None` — планы до блока 69.
    work_min: int | None = Field(None, ge=0)
    # Работа выполнена, идёт или бригада уже выехала (PLAN 6.12).
    committed: bool = False


class Lunch(Model):
    """Обед бригады (блок 79): когда и где — после какой заявки; `None` — до первой, в точке
    старта."""

    start: Clock
    end: Clock
    after: str | None = None


class Route(Model):
    engineer_id: str
    km: float = Field(ge=0)
    geometry: list[list[float]] = []  # [lon, lat] — порядок карты 2ГИС
    # Индекс точки `geometry`, с которой начинается переезд к каждому стопу (6.11): по
    # нему проигрывание дня ведёт инженера по своему участку. Пусто у планов до блока 63
    # и когда Valhalla склеила плечи — тогда карта ведёт по прямой между точками.
    leg_starts: list[int] = []
    stops: list[Stop] = []
    lunch: Lunch | None = None  # у планов до блока 79 и у бригады без работы обеда нет


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
    # Сколько работа займёт у этой бригады — по скорости на начало дня (блок 69);
    # `None` — планы до блока 69 в базе.
    duration_min: int | None = None
    free_slots: list[Slot] = []  # когда бригада свободна в текущем плане
    # Когда бригада приедет к заявке (блок 80, «Переназначение заявки» — кадр 1791): у своей —
    # по плану, у чужой — после лучшей вставки; `None` — вставки нет или планы до блока 80.
    arrival: Clock | None = None


class Remedy(Model):
    """Что поможет неназначенной заявке (PLAN 6.8): одно ослабленное условие, проверенное
    вставкой. Окно — для «Перенести окно», `engineer` — готовая бригада для «Добавить бригаду»."""

    kind: Literal["window", "equipment", "transport", "engineer", "shift"]
    text: str
    engineer_id: str | None = None
    window_start: Clock | None = None
    window_end: Clock | None = None
    engineer: Engineer | None = None


class Explanation(Model):
    text: str
    candidates: list[Candidate] = []
    remedies: list[Remedy] = []


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
    # Сколько ждут назначенные аварии, в сумме (PLAN 6.10, блок 49): от начала окна, но не
    # раньше начала рабочего дня. `None` — план посчитан до блока 49, и считать её было нечем.
    emergency_wait_min: int | None = None
    # Меньше стольких бригад назначенные заявки не выполнить (PLAN 6.10, блок 50): оценка
    # снизу, а не доказанный оптимум. `None` — план посчитан до блока 50.
    engineers_lower_bound: int | None = None
    # Сколько заявок план мог взять вообще: назначенные и неназначенные, которые помещаются
    # в пустой маршрут хоть одной бригады (`NOT_FITTED`). Валидатор обе не сверяет.
    requests_ceiling: int | None = None
    # Сколько минут добавили сложность объектов и скорость инженеров к нормативу у
    # назначенных заявок (PLAN 9.1). В режиме `norm` — ноль.
    complexity_extra_min: int = 0
    # Сколько заявок входа простых, средних и сложных по коэффициенту сложности: ключи —
    # `duration.LEVELS`.
    objects_by_level: dict[str, int] = {}
    # Средний рейтинг исполнителя по назначенным заявкам и сколько заявок у инженеров с
    # рейтингом `TOP_RATING` и выше (блок 71). Снимок плана, без поправки на число отзывов.
    # `None` — ни у одного исполнителя нет отзывов.
    rating_avg: float | None = None
    top_rated_count: int = 0
    # Дорога до первой заявки в среднем по инженерам с работой (блок 73): видно, что даёт
    # начало дня из дома. `None` — назначенных нет.
    first_leg_min: int | None = None


class NormMetrics(Model):
    """Основной вариант того же дня по нормативу (PLAN 9.1): с чем сравнить учёт сложности."""

    assigned_count: int
    engineers_used: int
    total_km: float


class Validation(Model):
    """Результат независимой проверки плана (PLAN 6.7)."""

    ok: bool
    errors: list[str] = []


class SearchStats(Model):
    """Как прошёл поиск OR-Tools (PLAN 6.5): чем кончился и как сходился.

    `solutions` — принятые решения, включая худшие: GLS ими выбирается из локальных минимумов.
    При тёплом старте их на одно больше предела: считается и прочитанное стартовое решение.
    `trace` — точки «секунда от старта → лучшая цель», только улучшения, не больше 50.
    У плана, взятого из кэша решений, это статистика исходного поиска (PLAN 6.5).
    """

    objective: int
    solutions: int
    stopped_by: Literal["solutions", "time"]
    warm_started: bool
    sec: float
    trace: list[tuple[float, int]]


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


class RequestComplexity(Model):
    """Сложность объекта заявки для бейджа (PLAN 9.1): коэффициент и уровень — ключ
    `duration.LEVELS`. Считает бэкенд: формула на фронт не копируется."""

    complexity: float
    level: str


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


class EmergencyCandidate(Model):
    """Кто может поехать на аварию, пришедшую днём (блок 74, «Кто поедет на аварию»).

    `now` — что бригада делает в момент аварии: едет к заявке `requestId`, на адресе этой
    заявки до `until` или свободна. `action` — что будет: развернём по дороге, доделает и
    поедет, поедет сразу. `recommended` — тот, кто прибудет раньше всех: его и отправляет
    расчёт, если диспетчер не выбрал другого. Флаг есть только у кандидатов предпросмотра:
    `diff.dispatch` читается по готовой версии, где других кандидатов уже нет, и там он `false`.
    """

    engineer_id: str
    now: Literal["en_route", "on_site", "free"]
    request_id: str | None = None
    until: Clock | None = None
    action: Literal["divert", "finish", "go"]
    arrival: Clock
    recommended: bool = False


class Diff(Model):
    requests: list[RequestChange] = []
    engineers: list[EngineerChange] = []
    metrics: MetricsDiff
    # Доля незакреплённых заявок, назначенных в обеих версиях, у которых не изменились бригада
    # и время (PLAN 6.13, блок 50). `None` — таких заявок нет или diff посчитан до блока 50.
    stability: float | None = None
    # Кто поехал на аварию события и что для этого бросил (блок 74, «Разворот»).
    dispatch: EmergencyCandidate | None = None


class Plan(Model):
    id: str
    parent_plan_id: str | None = None  # планы неизменяемы: событие создаёт новую версию
    dataset_id: str
    algorithm: Algorithm
    variant: Variant | None = None
    compare_group_id: str | None = None
    # Сколько секунд занял расчёт этого плана: столбец сводной таблицы вариантов (PLAN 6.14).
    # Это замер, а не свойство алгоритма: план из кэша решений собирается за доли секунды,
    # а время исходного поиска лежит в `search.sec` (PLAN 6.5).
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
    # Заявка → момент, когда затянувшаяся работа ещё шла (блок 68): раньше него она не
    # кончится. Наследуется цепочкой, как факт, и снимается закрытием.
    running: dict[str, Clock] = {}
    # Заявка → где она на момент события. Вычисляется, а не хранится (PLAN 6.19): у
    # неназначенной заявки статуса нет — её состояние описывает причина в `unassigned`.
    statuses: dict[str, RequestStatus] = {}
    unassigned: list[Unassigned] = []
    explanations: dict[str, Explanation] = {}
    route_explanations: dict[
        str, Annotated[RouteExplanation, BeforeValidator(legacy_route_explanation)]
    ] = {}
    metrics: Metrics = Metrics()
    # Только у вариантов сравнения в режиме `complexity` (PLAN 9.1): тот же день по нормативу.
    norm_metrics: NormMetrics | None = None
    # Сложность каждой заявки входа (блок 67) — для бейджа и фильтра «сложные объекты».
    complexity: dict[str, RequestComplexity] = {}
    validation: Validation | None = None
    forecast: Forecast | None = None  # прогноз опозданий (PLAN 6.18)
    # С учётом сложности (блок 67): риск тех же маршрутов по нормативу — «было» в подсказке
    # «риск было → стало». По нормативу пусто.
    norm_forecast: dict[str, RequestForecast] = {}
    approximate: bool = False  # расстояния считались по прямой: Valhalla не ответила
    # Текст оповещения к флагу: чьим километрам и временам верить нельзя (PLAN 5.6).
    approximate_note: str | None = None
    fallback_to_baseline: bool = False  # вернули базовый вариант (PLAN 6.6)
    # Как шёл поиск OR-Tools (PLAN 6.5). Есть только у оптимизированного расчёта: базовый
    # вариант, встраивание и ручная правка солвер не зовут.
    search: SearchStats | None = None
    diff: Diff | None = None  # что изменилось по сравнению с родителем (PLAN 6.13)
    created_at: datetime
    # Вариант предпросмотра, ещё не применённый (PLAN 6.12, блок 53): в цепочку не входит,
    # родителем быть не может и удаляется TTL-индексом после `draftExpiresAt`.
    draft: bool = False
    draft_expires_at: datetime | None = None


# --- предпросмотр события (PLAN 6.12, блок 53) -------------------------------


class SummaryRequest(Model):
    """Выполненная заявка в «Итогах дня»: работа по плану и по факту, минуты (блок 68)."""

    request_id: str
    plan_min: int
    fact_min: int


class EngineerSummary(Model):
    engineer_id: str
    name: str
    plan_min: int  # сумма работ выполненных заявок по плану, без дороги и ожидания
    fact_min: int
    speed_start: int  # тысячные: скорость на начало дня — по ней считался план
    speed_now: int  # тысячные: по профилю после выполненных заявок
    requests: list[SummaryRequest] = []


class DaySummary(Model):
    """«Итоги дня» (PLAN 9.1, блок 68): по инженерам и отдельно — что не сделано."""

    engineers: list[EngineerSummary] = []
    cancelled: list[str] = []
    deferred: list[str] = []
    failed: list[str] = []
    unassigned: list[str] = []


class PreviewTaken(Model):
    """Кто взял заявку события, через сколько минут после события приедет и когда начнёт.

    `arrival_min` — `null`, если прибытие раньше события: так бывает только в `full`, где
    день считается с утра, «как если бы событие было известно заранее».
    """

    engineer_id: str
    arrival_min: int | None
    start: Clock


class PreviewSummary(Model):
    """Сводка варианта для карточки. Пишет её код по двум версиям плана, а не модель.

    Имена и адреса фронт берёт из `plan.diff` и `plan.input` — так же, как «План обновлён».
    """

    taken: PreviewTaken | None = None
    reassigned: int = 0  # заявок сменили бригаду
    shifted: int = 0  # у той же бригады начало сдвинулось больше чем на 5 минут
    unassigned: int = 0  # были назначены и остались без бригады


class PreviewOption(Model):
    mode: ReplanMode
    plan: Plan
    summary: PreviewSummary


class Preview(Model):
    """Варианты реакции на событие (PLAN 6.12): черновики, рекомендация и отказы режимов."""

    options: list[PreviewOption]
    recommended: ReplanMode
    # Режим → почему он события не принял (например, правка бригады, сдвигающая начатую работу,
    # не проходит в `from_event`, но проходит в `full`).
    refused: dict[ReplanMode, str] = {}
    # Авария днём (блок 74): кто может поехать, по времени прибытия. Пусто — некому.
    emergency_candidates: list[EmergencyCandidate] = []


# --- отзывы и рейтинг (PLAN 9.1, блок 70) ------------------------------------


class ReviewInput(Model):
    """Оценка заявки: от диспетчера (карточка) или клиента (ссылка)."""

    stars: int = Field(ge=1, le=5)
    tags: Annotated[list[ReviewTag], AfterValidator(check_unique)] = []
    comment: str = Field("", max_length=500)
    client_name: str = Field("", max_length=60)


class Review(ReviewInput):
    """Отзыв о работе по заявке. Одна оценка на заявку; относится к исполнителю."""

    request_id: str
    engineer_id: str
    source: ReviewSource
    at: datetime


class EngineerRating(Model):
    """Живой рейтинг инженера (`GET /api/ratings`)."""

    engineer_id: str
    stars: float
    count: int
    # Сколько оценок 5, 4, 3, 2, 1 — в этом порядке, как полосы на кадре 1753.
    distribution: list[int]
    # Средняя сейчас минус средняя месяц назад; нет отзывов старше месяца — `None`.
    month_change: float | None = None
    last: list[Review]


class RequestReview(Model):
    """Оценка выполненной заявки для карточки: кому уйдёт, что уже стоит, ссылка клиенту."""

    engineer_id: str
    engineer_name: str
    review: Review | None = None
    link: str


class RateForm(Model):
    """Что видит клиент по ссылке (`GET /api/rate/{token}`)."""

    engineer_name: str
    date: datetime
    connection: bool
    rated: bool


# --- история версий (PLAN 6.17) ------------------------------------------------


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
