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


class BuildingType(StrEnum):
    """Тип здания объекта (PLAN 9.1): от него зависит, учитываются ли этаж и квартиры."""

    APARTMENT = "apartment"
    PRIVATE = "private"
    OFFICE = "office"


class Access(StrEnum):
    """Подъезд к дому (PLAN 9.1): добавляет минуты, а не процент."""

    NORMAL = "normal"
    FAR_PARKING = "far_parking"
    CLOSED = "closed"


class DurationMode(StrEnum):
    """Как считается длительность работы (PLAN 9.1): по нормативу или с учётом сложности."""

    NORM = "norm"
    COMPLEXITY = "complexity"


class Outcome(StrEnum):
    """Чем кончилась работа по заявке — со слов бригады, глазами диспетчера (PLAN 6.19)."""

    DONE = "done"
    CANCELLED = "cancelled"
    FAILED = "failed"


class ReviewSource(StrEnum):
    """Кто поставил оценку (PLAN 9.1, блок 70): диспетчер в карточке, клиент по ссылке или демо."""

    DISPATCHER = "dispatcher"
    CLIENT = "client"
    DEMO = "demo"


class ReviewTag(StrEnum):
    """Метка отзыва (кадр 1779) — только похвала: жалоб меткой клиенту не предлагаем,
    для них есть звёзды и комментарий (решение заказчика, блок 70)."""

    FAST = "fast"
    NEAT = "neat"
    EXPLAINED = "explained"
    FRIENDLY = "friendly"
    POLITE = "polite"
    PUNCTUAL = "punctual"


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
BUILDING_TYPE_NAMES = {
    BuildingType.APARTMENT: "Многоквартирный дом",
    BuildingType.PRIVATE: "Частный дом",
    BuildingType.OFFICE: "Офис",
}
ACCESS_NAMES = {
    Access.NORMAL: "Можно подъехать близко",
    Access.FAR_PARKING: "Парковка далеко",
    Access.CLOSED: "Закрытая территория или пропуск",
}
REVIEW_TAG_NAMES = {
    ReviewTag.FAST: "быстро",
    ReviewTag.NEAT: "аккуратно",
    ReviewTag.EXPLAINED: "всё объяснил",
    ReviewTag.FRIENDLY: "доброжелательный",
    ReviewTag.POLITE: "вежливый",
    ReviewTag.PUNCTUAL: "пунктуальный",
}
# Порог «рейтинг 4,5+» (блок 71): метрика плана и чип списка инженеров.
TOP_RATING = 4.5
# Типовой инженер резерва у загруженного набора (PLAN 9.1, блок 71): своих людей вне набора
# у него нет. Номер — первый свободный.
RESERVE_NAME = "Резерв {}"
# Подписи добавок к длительности (PLAN 9.1, «Почему 101 минута»): ключ — фактор `duration.py`.
DURATION_FACTOR_NAMES = {
    "floor": "{}-й этаж без лифта",
    "flats": "{} квартир в подъезде",
    "gigabit": "Гигабитное подключение",
    "repeat_visit": "Повторный выезд",
    "cable": "Прокладка кабеля",
    "drilling": "Сверление",
    "devices": "Устройств: {}",
}
# Пояснение серым под причиной «Почему будет выполняться дольше?» (блок 78, кадр 1522): ключ —
# фактор `duration.py`, тип здания или подъезд к дому.
DURATION_FACTOR_HINTS = {
    "floor": "подъём без лифта и перенос оборудования по лестнице",
    "flats": "дополнительное время на поиск нужного подключения в щитке",
    "gigabit": "потребуется дополнительная проверка кабеля и скорости соединения",
    "repeat_visit": "потребуется повторная диагностика и проверка причины неисправности",
    "cable": "требуется проложить новый кабель до оборудования",
    "drilling": "потребуется сделать новые отверстия для кабеля",
    "devices": "настройка каждого устройства",
    "private": "в частном доме короче разводка до оборудования",
    "office": "согласование доступа и разводка по офису",
    "far_parking": "оборудование нести от дальней парковки",
    "closed": "оформление пропуска или ожидание у входа",
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
    "shortage": "Нехватка",
}
# Графики смен (PLAN 2.4, ответ организаторов 29.09): смена 2/2 — 10:00–22:00, допустимо
# 09:30–23:00; смена 5/2 — 09:00–18:00. Генератор бригад раздаёт основные 2/2 и 5/2, а
# 09:30–23:00 — только выбор в форме бригады. Фронт держит копию (`SHIFT_PRESETS` в `useApi.ts`).
SHIFT_PRESETS = {
    "2-2": {"name": "2/2 (10:00 – 22:00)", "start": "10:00", "end": "22:00"},
    "2-2-long": {"name": "2/2 (09:30 – 23:00)", "start": "09:30", "end": "23:00"},
    "5-2": {"name": "5/2 (09:00 – 18:00)", "start": "09:00", "end": "18:00"},
}
# Обед (блок 79, решение заказчика 18.10.2026): час, обязателен; окно по умолчанию одно у всех,
# у бригады правится. Работа на обед не прерывается: он — между работами, в точке, где бригада
# оказалась. Фронт держит копию окна (`LUNCH_WINDOW` в `useApi.ts`).
LUNCH_MIN = 60
LUNCH_WINDOW = ("13:00", "16:00")
# Что раздаёт генератор бригад: три записи, две из них 2/2 (`scripts/prepare_data.py`).
GENERATED_SHIFTS = ("5-2", "2-2", "2-2")
# Варианты сравнения (PLAN 6.14): ключ — `models.Variant`. Те же названия, что на фронте
# (`VARIANT_NAMES` в `useApi.ts`); здесь их печатает скрипт замеров README (блок 46).
VARIANT_NAMES = {
    "baseline": "Базовый (по ТЗ)",
    "emergency_first": "Аварии в приоритете",
    "emergency_asap": "Аварии сразу",
    "max_requests": "Максимум заявок",
    "urgent_first": "Срочные в приоритете",
    "priority_first": "Тип работ важнее количества",
    "rating_first": "Рейтинг в приоритете",
    "fast": "Быстрый расчёт",
}
# Чем посчитан план: ключ — `models.Algorithm`. Те же названия, что на фронте
# (`ALGORITHM_NAMES` в `useApi.ts`); здесь их печатает выгрузка плана (блок 56).
ALGORITHM_NAMES = {
    "baseline": "Базовый",
    "optimized": "Оптимизированный",
    "manual": "Ручная правка",
}

# Границы вырезки OSM для Valhalla: точка за ними недостижима, адрес считаем ненайденным (PLAN 6.1).
LAT_RANGE = (54.2, 57.0)
LON_RANGE = (35.1, 40.3)
