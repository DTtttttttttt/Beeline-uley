"""Справочники и русские названия (PLAN 5.1). Единственная копия значений в проекте."""

from enum import StrEnum


class Skill(StrEnum):
    LOCAL = "local"
    CONNECTION = "connection"
    EMERGENCY = "emergency"


class Transport(StrEnum):
    """Транспорт бригады. Пешехода отдельным видом нет: короткие плечи уже идутся пешком
    внутри `public_transport` (PLAN 3.3), а `pedestrian` остался служебным профилем Valhalla."""

    CAR = "car"
    BICYCLE = "bicycle"
    PUBLIC_TRANSPORT = "public_transport"


class Equipment(StrEnum):
    ROUTER = "router"
    SET_TOP_BOX = "set_top_box"
    ALICE = "alice"


class Tool(StrEnum):
    """Инструмент бригады (PLAN 5.1). В отличие от оборудования не расходуется: проверяется
    только наличие, поэтому вместимость и измерение OR-Tools на него не заводятся."""

    CABLE_TESTER = "cable_tester"
    CRIMPING_TOOL = "crimping_tool"
    LAPTOP = "laptop"


class WorkPriority(StrEnum):
    """Тип работы. Срочность заявки (`urgent`) — отдельный признак, это не она."""

    EMERGENCY = "emergency"
    NEW_CONNECTION = "new_connection"
    REGULAR = "regular"


class Outcome(StrEnum):
    """Чем кончилась работа по заявке — со слов бригады, глазами диспетчера (PLAN 6.19)."""

    DONE = "done"
    CANCELLED = "cancelled"
    FAILED = "failed"


class RequestStatus(StrEnum):
    """Где заявка на момент события (PLAN 6.19).

    Вычисляется по стопу плана и времени события, в документе не хранится: все четыре времени
    стопа уже есть, а хранимый статус разошёлся бы с ними на первом же перепланировании.
    `committed` — не статус: он про расчёт («назначение не пересматривается»), а эти значения
    описывают день диспетчеру.
    """

    SENT = "sent"
    EN_ROUTE = "en_route"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    CANCELLED = "cancelled"
    FAILED = "failed"
    DEFERRED = "deferred"


SKILL_NAMES = {
    Skill.LOCAL: "Локальные работы",
    Skill.CONNECTION: "Работы на подключение и дозаказы",
    Skill.EMERGENCY: "Аварийные работы",
}
TRANSPORT_NAMES = {
    Transport.CAR: "Автомобиль",
    Transport.BICYCLE: "Велосипед",
    Transport.PUBLIC_TRANSPORT: "Пешеход / общественный транспорт",
}
EQUIPMENT_NAMES = {
    Equipment.ROUTER: "Роутер",
    Equipment.SET_TOP_BOX: "Приставка",
    Equipment.ALICE: "Алиса",
}
TOOL_NAMES = {
    Tool.CABLE_TESTER: "Кабельный тестер",
    Tool.CRIMPING_TOOL: "Обжимной инструмент",
    Tool.LAPTOP: "Ноутбук",
}
WORK_PRIORITY_NAMES = {
    WorkPriority.EMERGENCY: "Авария",
    WorkPriority.NEW_CONNECTION: "Новое подключение",
    WorkPriority.REGULAR: "Обычная",
}
OUTCOME_NAMES = {
    Outcome.DONE: "Выполнена",
    Outcome.CANCELLED: "Отменена",
    Outcome.FAILED: "Не выполнена",
}
REQUEST_STATUS_NAMES = {
    RequestStatus.SENT: "Отправлено",
    RequestStatus.EN_ROUTE: "В пути",
    RequestStatus.IN_PROGRESS: "Выполняется",
    RequestStatus.DONE: "Завершено",
    RequestStatus.CANCELLED: "Отменена",
    RequestStatus.FAILED: "Не выполнена",
    RequestStatus.DEFERRED: "Перенесена на следующий день",
}
# Типы заявки, которые ассистент называет в черновике (блок 42): ключ — то же, что у
# `REQUEST_TYPES` на фронте, где по нему берутся навык, норматив и инструменты.
REQUEST_TYPE_NAMES = {
    "connection": "Подключение клиента",
    "emergency": "Авария на ТКД",
    "order": "Дозаказ оборудования",
    "local": "Локальная заявка",
}
# Составы демо-наборов (PLAN 6.20): ключ — идентификатор состава в API.
CREW_NAMES = {
    "reduced": "Сокращённый состав",
    "staff": "Штатный состав",
    "extended": "Расширенный состав",
}

# Границы вырезки OSM для Valhalla: точка за ними недостижима, адрес считаем ненайденным (PLAN 6.1).
LAT_RANGE = (54.2, 57.0)
LON_RANGE = (35.1, 40.3)
