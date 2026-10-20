"""Готовит наборы данных: CSV кейса -> data/seed/dataset.json и data/examples/*.json (PLAN 3.1-3.9).

Запуск из backend/: python scripts/prepare_data.py --seed 1
Матрицы переездов встроенного набора и примеров (PLAN 5.7, нужна поднятая Valhalla):
    python scripts/prepare_data.py --matrices
Сводки контрольной выборки и день людей по участкам -> data/seed/control.json (PLAN 2.1):
    python scripts/prepare_data.py --control
Инженеры для составов демо-наборов -> data/seed/crews.json (PLAN 6.20):
    python scripts/prepare_data.py --crews
История отзывов демо-людей -> data/seed/reviews.json (PLAN 9.1, блок 70):
    python scripts/prepare_data.py --reviews
"""

import argparse
import csv
import io
import json
import random
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import ValidationError

from app.config import settings
from app.data import geocoder, importer
from app.data.geocoder import BUILDING_PART, normalize_address
from app.data.seed import (
    BUILTIN_ID,
    CREW_POOL,
    DEMO_CREWS,
    DEMO_REQUESTS,
    DEMO_TRAINING,
    demo_raw,
    load_example_matrices,
    load_matrices,
    remote_clusters,
    settle_homes,
)
from app.dictionaries import (
    GENERATED_SHIFTS,
    SHIFT_PRESETS,
    Access,
    BuildingType,
    Equipment,
    Skill,
    Tool,
    Transport,
    WorkPriority,
)
from app.models import Dataset, Engineer, Request
from app.planning import reviews, schedule
from app.planning.control import people_day
from app.routing import matrices
from app.routing.valhalla import PROFILE_BY_TRANSPORT, TRANSIT

# Каталог данных - тот же, из которого набор читает приложение (settings.data_dir).
DATA = settings.data_dir
RAW = DATA / "raw"
CACHE_PATH = RAW / "geocode-cache.json"

# Встроенный набор один - «Восток» (PLAN 2.1); остальные два лежат примерами для импорта.
CSV_NAME = "{} Синтетические данные.csv"
# Контрольная выборка - фактический день того же участка. Входом алгоритма она не является
# (PLAN 2.1): отсюда берётся число бригад как ориентир (ответ 12), справочная сводка
# «факт того же дня» (ответ 11) и день людей (блок 51) — для строки сравнения в метриках.
CONTROL_NAME = "{} Контрольное распределение.csv"
CONTROL_PATH = DATA / "seed" / "control.json"
# Статусы фактического дня, которые попадают в сводку (колонка «Статус BK»).
CONTROL_STATUSES = {
    "done": "Выполнена",
    "cancelled": "Отменена",
    "overdue": "Просрочена",
}
# Колонки, по которым строка контрольной выборки совпадает с синтетикой (кроме адреса).
CONTROL_KEY = ("Тип заявки BK", "Начало", "Окончание", "Район")

# Составы демо-наборов (PLAN 6.20) - первые N инженеров одного списка. Он генерируется тем
# же потоком, что бригады «Востока», поэтому его начало и есть бригады встроенного набора.
CREWS_PATH = DATA / "seed" / "crews.json"
REVIEWS_PATH = DATA / "seed" / "reviews.json"
# Третий элемент - основа имени для примера в формате загрузки (PLAN 5.4, блок 12.3).
SOURCES = [
    ("Восток", DATA / "seed" / "dataset.json", None),
    ("Юго-восток", DATA / "examples" / "yugo-vostok.json", None),
    (
        "Югоцентр",
        DATA / "examples" / "yugotsentr.json",
        DATA / "examples" / "yugotsentr",
    ),
]

# Наборы, для которых считаются матрицы (PLAN 5.7): встроенный и оба примера для импорта.
# Примеры — по той же причине, что встроенный: расчёт на сотню точек идёт минуты, а на
# наборе с дальними адресами способен положить Valhalla по памяти. На защите такого быть
# не должно, поэтому таблицы считаются заранее и лежат рядом с наборами.
MATRIX_TARGETS = [
    (BUILTIN_ID, DATA / "seed" / "dataset.json", DATA / "seed" / "matrices.json"),
    (
        "example:yugo-vostok",
        DATA / "examples" / "yugo-vostok.json",
        DATA / "examples" / "yugo-vostok-matrices.json",
    ),
    (
        "example:yugotsentr",
        DATA / "examples" / "yugotsentr.json",
        DATA / "examples" / "yugotsentr-matrices.json",
    ),
]

# PLAN 2.2: длительность работы = базовый норматив минус 20 минут заложенной в него дороги.
NORMS = {
    "Подключение": (70, 90),
    "Глобальная проблема": (80, 100),
    "Дозаказ": (20, 40),
    "Локальная заявка": (30, 50),
}
# Документы из того же норматива (PLAN 9.1): на сложность объекта они не умножаются.
DOCUMENTS_BY_TYPE = {
    "Подключение": 10,
    "Глобальная проблема": 0,
    "Дозаказ": 10,
    "Локальная заявка": 0,
}
SKILL_BY_TYPE = {
    "Подключение": Skill.CONNECTION,
    "Дозаказ": Skill.CONNECTION,
    "Локальная заявка": Skill.LOCAL,
    "Глобальная проблема": Skill.EMERGENCY,
}
WORK_PRIORITY_BY_TYPE = {
    "Подключение": WorkPriority.NEW_CONNECTION,
    "Дозаказ": WorkPriority.REGULAR,
    "Локальная заявка": WorkPriority.REGULAR,
    "Глобальная проблема": WorkPriority.EMERGENCY,
}

# Порядок справочников менять нельзя: от него зависят rnd.sample и rnd.choice,
# а значит и воспроизводимость набора на том же seed.
SKILLS = list(Skill)
TRANSPORTS = list(Transport)
EQUIPMENT = list(Equipment)
EQUIPMENT_CHANCE = [
    (Equipment.ROUTER, 0.8),
    (Equipment.SET_TOP_BOX, 0.6),
    (Equipment.ALICE, 0.4),
]
# Смены по ответу организаторов 29.09 (PLAN 2.4): 5/2 — 09:00–18:00, 2/2 — 10:00–22:00.
# Три записи, а не две: число розыгрышей `rnd.choice` то же, что у прежних трёх смен, поэтому
# навыки, транспорт и оборудование бригад не сдвинулись, а 2/2 достаётся около двух третей.
SHIFTS = [
    (SHIFT_PRESETS[preset]["start"], SHIFT_PRESETS[preset]["end"])
    for preset in GENERATED_SHIFTS
]
ALICE_SHARE = 0.15
# Начальная скорость инженера (PLAN 9.1, блок 68) - допущение команды: ±15 % от нормы.
SPEED_SPREAD = 0.15

# Инструменты по типу работ - допущение команды (PLAN 2.7): линию проверяют тестером,
# новое подключение ещё и обжимают, аварию на узле диагностируют с ноутбука, а дозаказ -
# это привезти и отдать оборудование. Правило без случайности: поток заявок не сдвигается.
TOOLS_BY_TYPE = {
    "Подключение": [Tool.CABLE_TESTER, Tool.CRIMPING_TOOL],
    "Глобальная проблема": [Tool.CABLE_TESTER, Tool.LAPTOP],
    "Дозаказ": [],
    "Локальная заявка": [Tool.CABLE_TESTER],
}
TOOL_CHANCE = [
    (Tool.CABLE_TESTER, 0.95),
    (Tool.CRIMPING_TOOL, 0.9),
    (Tool.LAPTOP, 0.7),
]

# Объект заявки (PLAN 9.1) - допущение команды: в выгрузке его нет. Доли типов здания и
# подъезда к дому, этажность по типу здания; лифт в доме до пяти этажей не ставят, в высоком
# доме он иногда не работает - тогда считаем «без лифта».
BUILDINGS = [
    (BuildingType.APARTMENT, 0.8),
    (BuildingType.OFFICE, 0.1),
    (BuildingType.PRIVATE, 0.1),
]
FLOORS_BY_BUILDING = {
    BuildingType.APARTMENT: [5, 5, 9, 9, 12, 14, 17, 22, 25],
    BuildingType.OFFICE: [3, 5, 8, 12, 20],
}
ELEVATOR_BROKEN = 0.1
FLATS_UNKNOWN = 0.15  # «?» в форме: сколько квартир, не знаем
ACCESS_CHANCE = [
    (Access.NORMAL, 0.7),
    (Access.FAR_PARKING, 0.15),
    (Access.CLOSED, 0.15),
]
REPEAT_VISIT_SHARE = 0.1
# Условия работы подключения (блок 78): прокладка кабеля и сверление — примерно у каждой десятой.
CABLE_SHARE = 0.1
DRILLING_SHARE = 0.1
FLATS_PER_FLOOR = 4  # квартир на этаже подъезда

# Бригада - один человек (Дополнения, п. 1), поэтому имя - фамилия. Список длиннее любого
# набора: фамилии в наборе не повторяются.
# fmt: off
SURNAMES = [
    "Соколов", "Мельников", "Попов", "Иванов", "Смирнов", "Кузнецов", "Васильев",
    "Петров", "Новиков", "Фёдоров", "Морозов", "Волков", "Алексеев", "Лебедев",
    "Семёнов", "Егоров", "Павлов", "Козлов", "Степанов", "Николаев", "Орлов", "Андреев",
    "Макаров", "Никитин", "Захаров", "Зайцев", "Соловьёв", "Борисов", "Яковлев",
    "Григорьев", "Романов", "Воробьёв", "Сергеев", "Кузьмин", "Фролов", "Александров",
]
# Фамилии сверх 36 — для резерва `crews.json` (блок 71). Отдельным списком и своим потоком:
# удлинённый SURNAMES сдвинул бы выборку, и первые люди списка получили бы другие фамилии.
EXTRA_SURNAMES = ["Дмитриев", "Королёв", "Гусев", "Киселёв", "Ильин", "Максимов"]
# fmt: on


# --- чтение CSV (PLAN 3.2) ---------------------------------------------------


def read_csv(path):
    """Строки заявок и адрес офиса из последней строки файла."""
    rows, office = [], None
    text = path.read_bytes().decode("cp1251")
    for row in csv.DictReader(io.StringIO(text), delimiter=";"):
        first = (row["Заявка"] or "").strip()
        if not first:
            continue
        if first.lower().startswith("адрес офиса"):
            office = (row["Тип заявки BK"] or "").strip()
            continue
        rows.append(row)
    if office is None:
        raise ValueError(f"{path.name}: нет строки «Адрес офиса»")
    return rows, office


# --- заявка (PLAN 2.3, 3.3) --------------------------------------------------


def equipment_of(row, kind, rnd):
    hd = (row["Тип заявки HD"] or "").strip()
    needed = set()
    if hd == "Роутер. Замена техническим специалистом":
        needed.add(Equipment.ROUTER)
    if "Замена приставки техником" in hd:
        needed.add(Equipment.SET_TOP_BOX)
    if (row.get("Подключение") or "").strip() in ("FMC", "FTTB"):
        needed.add(Equipment.ROUTER)
    if (row.get("Гигабитное подключение") or "").strip() == "Да":
        needed.add(Equipment.ROUTER)
    if kind == "Дозаказ":
        needed.add(rnd.choice(EQUIPMENT))
    if rnd.random() < ALICE_SHARE:  # клиент попросил при оформлении (Дополнения, п. 13)
        needed.add(Equipment.ALICE)
    return [e for e in EQUIPMENT if e in needed]


def map_request(row, rnd, emergency_by="bk"):
    kind = (row["Тип заявки BK"] or "").strip()
    # Ответ 29.09: аварию задаёт поле HD, но решение по BK переделывать не требуется. Мы
    # остаёмся на BK; `hd` — для замера: «Глобальная проблема» без HD «Авария» — локальная.
    hd = (row["Тип заявки HD"] or "").strip()
    if emergency_by == "hd" and kind == "Глобальная проблема" and hd != "Авария":
        kind = "Локальная заявка"
    if kind not in NORMS:
        raise ValueError(f"заявка {row['Заявка']}: неизвестный тип «{kind}»")
    service_min, base_min = NORMS[kind]
    skill = SKILL_BY_TYPE[kind]
    return {
        "id": (row["Заявка"] or "").strip(),
        "address": normalize_address(row["Адрес"]),
        "lat": None,
        "lon": None,
        "serviceDurationMin": service_min,
        "baseNormMin": base_min,
        "windowStart": clock(row["Начало"]),
        "windowEnd": clock(row["Окончание"]),
        "workPriority": WORK_PRIORITY_BY_TYPE[kind],
        "skill": skill,
        # Аварийные работы требуют автомобиль - допущение команды (PLAN 2.7).
        "requiredTransport": Transport.CAR if skill == Skill.EMERGENCY else None,
        "requiredEquipment": equipment_of(row, kind, rnd),
        "requiredTools": list(
            TOOLS_BY_TYPE[kind]
        ),  # своя копия, а не общий список правила
        "urgent": False,
        "hdType": hd or None,
        "gigabit": (row.get("Гигабитное подключение") or "").strip() == "Да",
        "documentsMin": DOCUMENTS_BY_TYPE[kind],
    }


def pick(rnd, chances):
    """Значение по долям `[(значение, доля), ...]`: один розыгрыш на выбор."""
    roll, total = rnd.random(), 0.0
    for value, share in chances:
        total += share
        if roll < total:
            return value
    return chances[-1][0]


def object_of(rnd, connection):
    """Объект, повторный выезд и условия работы заявки (PLAN 9.1, блок 78) своим потоком:
    остальные поля не сдвигаются. Прокладка кабеля и сверление — только у подключения."""
    building = pick(rnd, BUILDINGS)
    item = {"buildingType": building.value, "access": pick(rnd, ACCESS_CHANCE).value}
    if building is not BuildingType.PRIVATE:
        floors = rnd.choice(FLOORS_BY_BUILDING[building])
        entrances = 1 if building is BuildingType.OFFICE else rnd.randint(1, 8)
        lift = floors > 5 and rnd.random() >= ELEVATOR_BROKEN
        item |= {
            "floor": rnd.randint(1, floors),
            "floors": floors,
            "entrance": rnd.randint(1, entrances),
            "elevator": lift,
        }
        if building is not BuildingType.OFFICE:
            item["flatNumber"] = rnd.randint(1, floors * entrances * FLATS_PER_FLOOR)
            if rnd.random() >= FLATS_UNKNOWN:
                item["flats"] = floors * FLATS_PER_FLOOR
    cable, drilling = rnd.random() < CABLE_SHARE, rnd.random() < DRILLING_SHARE
    return {
        "object": item,
        "repeatVisit": rnd.random() < REPEAT_VISIT_SHARE,
        "cable": connection and cable,
        "drilling": connection and drilling,
    }


def clock(value):
    """«17.08.2026 0:01» -> «00:01»: дата одна на весь набор, нужно только время."""
    hours, minutes = value.split()[-1].split(":")
    return f"{int(hours):02d}:{int(minutes):02d}"


# Нормализация адреса и запрос к 2ГИС живут в app/data/geocoder.py: их же зовёт
# импорт файла (блок 12). Здесь остаётся только файловый кэш и пауза между запросами.


# --- геокодирование (PLAN 3.5) -----------------------------------------------


class Geocoder:
    """2ГИС Geocoder с кэшем на диске: демо-ключ даёт 1 000 запросов, лишних не делаем."""

    def __init__(self):
        self.cache = {}
        if CACHE_PATH.exists():
            self.cache = json.loads(CACHE_PATH.read_text("utf-8"))
        self.misses = []
        self.calls = 0

    def __call__(self, address):
        if address in self.cache:
            return tuple(self.cache[address])
        if address in self.misses:  # один и тот же адрес встречается в наборах не раз
            return None
        point = self.ask(address)
        if point is None:
            without_building = BUILDING_PART.sub("", address)
            if without_building != address:
                point = self.ask(without_building)
        if point is None:
            self.misses.append(address)
            return None
        # Пишем после каждого найденного адреса: прерванный запуск не тратит лимит заново.
        self.cache[address] = list(point)
        dump = json.dumps(self.cache, ensure_ascii=False, indent=2, sort_keys=True)
        CACHE_PATH.write_text(dump, "utf-8")
        return point

    def ask(self, query):
        time.sleep(0.15)
        self.calls += 1
        try:
            return geocoder.lookup(query)
        except geocoder.GeocodeError as error:
            # Просроченный ключ и исчерпанный лимит отвечают так на каждый адрес.
            # Молча записать в «не найдены» весь набор - отправить искать ошибку не там.
            sys.exit(str(error))


# --- бригады (PLAN 2.4, 3.6) -------------------------------------------------


def read_control(name):
    """Строки контрольной выборки участка; `None`, если файла нет."""
    path = RAW / CONTROL_NAME.format(name)
    if not path.exists():
        return None
    text = path.read_bytes().decode("cp1251")
    return [
        row
        for row in csv.DictReader(io.StringIO(text), delimiter=";")
        if (row["Заявка"] or "").strip()
    ]


def control_brigades(name):
    """Сколько бригад работало в фактическом дне того же участка (ответ 12).

    Контрольный файл в расчёт не идёт — число берётся из файла организаторов, а не
    назначается нами.
    """
    rows = read_control(name)
    return None if rows is None else brigade_count(rows)


def brigade_count(rows):
    """Число разных бригад в строках контрольной выборки; ни одной — `None`."""
    names = {(row.get("Бригада") or "").strip() for row in rows}
    return len(names - {""}) or None


def control_assignment(name, control):
    """Заявка синтетического набора -> бригада контрольного дня (блок 51).

    `control` — уже прочитанные строки контрольной выборки (`read_control`): файл читается
    один раз на сводку.

    Номера заявок в контрольной выборке другие, но строки идут в том же порядке, что в
    синтетике: тип, окно, район и адрес совпадают построчно. Сопоставляем по порядку и
    сверяем ключ — поиск по ключу запутался бы на полных дублях «Юго-востока». Ключ не
    совпал — ошибка, а не догадка. Отменённые и строки без бригады в день людей не входят:
    отменённые никто не выполнял, а когда их отменили, выгрузка не говорит.
    """
    rows, _ = read_csv(RAW / CSV_NAME.format(name))
    if len(rows) != len(control):
        raise ValueError(
            f"{name}: в контрольной выборке {len(control)} заявок, в синтетике {len(rows)}"
        )
    assignment = {}
    for number, (row, fact) in enumerate(zip(rows, control, strict=True), start=1):
        if control_key(row) != control_key(fact):
            raise ValueError(
                f"{name}, строка {number}: контрольная выборка {control_key(fact)} "
                f"не совпала с синтетикой {control_key(row)}"
            )
        brigade = (fact.get("Бригада") or "").strip()
        if brigade and (fact.get("Статус BK") or "").strip() != "Отменена":
            assignment[row["Заявка"].strip()] = brigade
    return assignment


def control_key(row):
    """Чем строка контрольной выборки совпадает с синтетикой: в контроле адрес с квартирой,
    нормализация её отрезает."""
    return (
        *((row[column] or "").strip() for column in CONTROL_KEY),
        normalize_address(row["Адрес"]),
    )


def control_summary(name, source):
    """Факт того же дня и день людей (ответ 11, PLAN 5.3, блок 51).

    Справка для сравнения на защите, а не вход алгоритма: в расчёт она не попадает.
    `total` — все заявки дня: выгрузка — снимок, и часть их ещё не закрыта. `perEngineer` —
    выполнено на одну бригаду фактического дня. `run` — день людей нашим расписанием
    (`planning/control.py`) по готовым таблицам набора `source`: ни Valhalla, ни ключ не нужны.
    """
    rows = read_control(name)
    engineers = None if rows is None else brigade_count(rows)
    if engineers is None:
        return None
    statuses = [(row.get("Статус BK") or "").strip() for row in rows]
    summary = {key: statuses.count(status) for key, status in CONTROL_STATUSES.items()}
    dataset = Dataset(
        id=f"control:{name}",
        created_at=datetime.now(UTC),
        **json.loads(source.read_text("utf-8")),
    )
    run = people_day(dataset, control_assignment(name, rows), control_travel(dataset))
    return {
        "engineers": engineers,
        "total": len(rows),
        **summary,
        "perEngineer": round(summary["done"] / engineers, 1),
        "run": run.model_dump(by_alias=True),
    }


def control_travel(dataset):
    """Готовые автомобильные таблицы набора — по отпечатку его точек, как их находит кэш
    (PLAN 5.4). Нет их — понятный отказ, а не `KeyError` в глубине расписания."""
    car = PROFILE_BY_TRANSPORT[Transport.CAR]
    table = matrices.points(dataset)
    digest = matrices.points_hash(table)
    document = next(
        (
            document
            for document in load_matrices() + load_example_matrices()
            if document["pointsHash"] == digest and document["profile"] == car
        ),
        None,
    )
    if document is None:
        sys.exit(
            f"{dataset.name}: нет готовой таблицы «{car}» под текущие точки набора — "
            "сначала prepare_data.py --matrices"
        )
    return matrices.Travel(dataset.id, table, {car: matrices.from_document(document)})


def write_control():
    """Сводки контрольной выборки участков демо-наборов -> data/seed/control.json (PLAN 2.1).

    Словарь по участкам (`DEMO_REQUESTS`): сводку показывает любая пара с заявками участка.
    Нужны контрольные CSV и готовые таблицы переездов; ни ключ 2ГИС, ни Valhalla.
    """
    summaries = {}
    for requests_id, relative in DEMO_REQUESTS.items():
        source = DATA / relative
        name = json.loads(source.read_text("utf-8"))["name"]
        summary = control_summary(name, source)
        if summary is None:
            sys.exit(f"{name}: нет контрольной выборки в data/raw")
        summaries[requests_id] = summary
        print(f"{name}, факт того же дня: {summary}")
    CONTROL_PATH.write_text(
        json.dumps(summaries, ensure_ascii=False, indent=2), "utf-8"
    )
    print(f"    записан {CONTROL_PATH.relative_to(DATA.parent)}")


# Дома бригад раздаёт app/data/seed.py (`settle_homes`): тем же кодом сервер собирает
# демо-пару «заявки + инженеры» (PLAN 6.20).


def make_engineers(count, office, rnd, tool_rnd, names, speed_rnd, home_rnd):
    """`tool_rnd` - свой поток для инструментов: они появились позже остальных полей, и в
    общем потоке сдвинули бы смены, навыки и транспорт всех бригад после первой. Фамилии
    (`names`), скорость (`speed_rnd`, блок 68) и домашнее оборудование (`home_rnd`, блок 73)
    - по той же причине отдельно. Адрес дома зависит от участка, его даёт `give_homes`."""
    engineers = []
    for number, surname in zip(range(1, count + 1), names, strict=True):
        shift_start, shift_end = rnd.choice(SHIFTS)
        skills = rnd.sample(SKILLS, rnd.randint(1, 3))
        transport = rnd.choice(TRANSPORTS)
        equipment = [e for e, chance in EQUIPMENT_CHANCE if rnd.random() < chance]
        tools = [t for t, chance in TOOL_CHANCE if tool_rnd.random() < chance]
        # Скорость на начало дня (PLAN 9.1): ±15 % от нормы, тысячные.
        speed = round(1000 * (1 + speed_rnd.uniform(-SPEED_SPREAD, SPEED_SPREAD)))
        home = [e for e, chance in EQUIPMENT_CHANCE if home_rnd.random() < chance]
        engineers.append(
            {
                "id": f"brigade-{number}",
                "name": surname,
                "startLat": office["lat"],
                "startLon": office["lon"],
                "shiftStart": shift_start,
                "shiftEnd": shift_end,
                "skills": [s for s in SKILLS if s in skills],
                "transport": transport,
                "equipment": equipment,
                "tools": tools,
                "speed": speed,
                "homeEquipment": home,
            }
        )
    return engineers


# --- чек-лист набора (PLAN 2.5, 3.8) -----------------------------------------


def to_model(kind, row, invalid):
    """Строка -> модель; непринятую записываем в список и продолжаем чек-лист."""
    try:
        return kind(**row)
    except ValidationError as error:
        problems = "; ".join(importer.where(e) for e in error.errors())
        invalid.append(f"{row.get('id')}: {problems}")
        return None


def checklist(dataset):
    requests, engineers = dataset["requests"], dataset["engineers"]
    # Заявки борются за одно время: сколько окон открыто разом в худшей точке дня.
    peak = max(
        (
            sum(1 for r in requests if r["windowStart"] <= moment < r["windowEnd"])
            for moment in {r["windowStart"] for r in requests}
        ),
        default=0,
    )
    ids = [r["id"] for r in requests]
    skills = {s for e in engineers for s in e["skills"]}
    transports = {e["transport"] for e in engineers}
    combinations = {tuple(e["skills"]) for e in engineers}
    no_point = [r["id"] for r in requests if r["lat"] is None]
    homes = {
        (e["startLat"], e["startLon"])
        for e in engineers
        if (e["startLat"], e["startLon"])
        != (dataset["office"]["lat"], dataset["office"]["lon"])
    }

    # Проверка ограничений живёт в одном месте - app/planning/schedule.py (PLAN 6.2),
    # а она работает с моделями. Строку, которую модель не приняла, показываем пунктом
    # чек-листа: с таким набором всё равно не поднимется приложение (PLAN 6.1).
    # Заявку без координат моделью не разобрать - её ловит соседний пункт про координаты.
    invalid = []
    crew = [m for e in engineers if (m := to_model(Engineer, e, invalid))]
    tasks = [
        m
        for r in requests
        if r["lat"] is not None and (m := to_model(Request, r, invalid))
    ]

    # Подходящая — ещё и та, что унесёт оборудование заявки за раз (PLAN 6.2): пешему
    # с вместимостью 2 заявка на три вида не достанется никогда.
    def fits(task, e):
        return schedule.resource_violation(
            task, e
        ) is None and not schedule.over_capacity(e, schedule.units([task]))

    no_brigade = [task.id for task in tasks if not any(fits(task, e) for e in crew)]

    items = [
        (f"идентификаторы заявок уникальны ({len(ids)})", len(set(ids)) == len(ids)),
        ("встречаются все три навыка", skills == set(SKILLS)),
        ("встречаются все три транспорта", transports == set(TRANSPORTS)),
        ("комбинации навыков разные", len(combinations) > 1),
        (f"окна заявок пересекаются (открыто разом до {peak})", peak > 1),
        (f"у каждой заявки есть бригада (нет у {len(no_brigade)})", not no_brigade),
        (f"у всех заявок есть координаты (без них: {len(no_point)})", not no_point),
        (
            f"удалённые адреса прикрыты домами бригад (кластеров {len(homes)})",
            len(remote_clusters(requests, dataset["office"])) == len(homes),
        ),
        (f"заявки и бригады проходят модели (не прошли: {len(invalid)})", not invalid),
    ]
    print("  Чек-лист набора:")
    for text, ok in items:
        print(f"    [{'ок ' if ok else 'нет'}] {text}")
    if no_brigade:
        print(f"    заявки без подходящей бригады: {', '.join(no_brigade)}")
    if no_point:
        print(f"    заявки без координат: {', '.join(no_point)}")
    for line in invalid:
        print(f"    {line}")
    print("    [ждёт] «базовый хуже оптимизированного» - проверяется в блоке 8")
    return all(ok for _, ok in items)


# --- сборка ------------------------------------------------------------------


def build(name, geocode, brigades, seed, emergency_by="bk"):
    """Набор из CSV участка. `brigades` — `None` означает «столько же, сколько в факте»."""
    rows, office_address = read_csv(RAW / CSV_NAME.format(name))
    rnd = random.Random(f"{seed}:requests:{name}")
    requests = [map_request(row, rnd, emergency_by) for row in rows]
    objects = random.Random(f"{seed}:objects:{name}")
    for request in requests:
        request |= object_of(
            objects, request["workPriority"] == WorkPriority.NEW_CONNECTION
        )

    office_address = normalize_address(office_address)
    point = geocode(office_address)
    if point is None:
        raise ValueError(f"{name}: не найден адрес офиса «{office_address}»")
    office = {"address": office_address, "lat": point[0], "lon": point[1]}

    for request in requests:
        point = geocode(request["address"])
        if point:
            request["lat"], request["lon"] = point

    brigade_rnd = random.Random(f"{seed}:brigades:{name}")
    tool_rnd = random.Random(f"{seed}:tools:{name}")
    names = random.Random(f"{seed}:names:{name}").sample(SURNAMES, brigades)
    speed_rnd = random.Random(f"{seed}:speeds:{name}")
    home_rnd = random.Random(f"{seed}:home-equipment:{name}")
    engineers = make_engineers(
        brigades, office, brigade_rnd, tool_rnd, names, speed_rnd, home_rnd
    )
    settle_homes(engineers, requests, office)
    return {
        "name": name,
        "office": office,
        "requests": requests,
        "engineers": engineers,
    }


def write_csv_example(dataset, stem):
    """Тот же набор в формате загрузки: пример для POST /api/dataset/import (PLAN 5.4).

    Выгружает `importer.to_csv` - тот же модуль, что файл потом и разбирает, поэтому
    пример не может разойтись с форматом. Офис - последней строкой CSV заявок.
    """
    office = {"requests": dataset["office"]["address"], "engineers": None}
    # Объект заявки в CSV - плоскими колонками (`importer.OBJECT_COLUMNS`).
    # Стартовый рейтинг в CSV — двумя колонками (`importer._rated`).
    rows = {
        "requests": [
            request | (request.get("object") or {}) for request in dataset["requests"]
        ],
        "engineers": [
            engineer
            | (
                {"rating": rating["stars"], "reviewsCount": rating["count"]}
                if (rating := engineer.get("rating"))
                else {}
            )
            for engineer in dataset["engineers"]
        ],
    }
    for part, columns in (
        ("requests", importer.REQUEST_COLUMNS),
        ("engineers", importer.ENGINEER_COLUMNS),
    ):
        path = stem.with_name(f"{stem.name}-{part}.csv")
        path.write_text(importer.to_csv(rows[part], columns, office[part]), "utf-8")
        print(f"    записан {path.relative_to(DATA.parent)}")


def ready(out_path, dataset, profiles):
    """Что уже посчитано: `{профиль: Matrix}` для текущих точек набора.

    Расчёт идёт минутами и на слабой машине способен уронить Valhalla по памяти, поэтому
    команда возобновляемая: посчитанное пропускается, и после перезапуска Valhalla можно
    просто повторить запуск. Точки или состав бригад изменились — отпечаток не совпадёт,
    и набор посчитается заново.
    """
    if not out_path.exists():
        return {}
    digest = matrices.points_hash(matrices.points(dataset))
    documents = json.loads(out_path.read_text("utf-8"))
    return {
        document["profile"]: matrices.from_document(document)
        for document in documents
        if document["pointsHash"] == digest and document["profile"] in profiles
    }


def derive_transit(dataset, done):
    """Общественный транспорт из готовых пешеходных и автомобильных таблиц (PLAN 3.3).

    Профиль производный, своего костинга у Valhalla для него нет, поэтому поднимать её
    ради него не нужно: файл, посчитанный до ответа 14, дополняется на месте.
    """
    if TRANSIT in done or TRANSIT not in matrices.profiles(dataset):
        return False
    if not set(matrices.TRANSIT_BASE) <= set(done):
        return False
    walk, car = (done[base] for base in matrices.TRANSIT_BASE)
    done[TRANSIT] = matrices.transit(walk, car, walk.point_ids)
    return True


def write_crews(seed):
    """Инженеры для составов демо-наборов -> data/seed/crews.json (PLAN 6.20).

    Список генерируется тем же потоком, что бригады «Востока», поэтому его первые 12 обязаны
    совпасть с бригадами встроенного набора: иначе «штатный состав» с заявками «Востока» не
    был бы встроенным набором. Каждая пара «заявки + состав» проходит чек-лист (2.5); не
    прошла — файл не пишется. Ни ключ 2ГИС, ни Valhalla не нужны.
    """
    name, source, _ = SOURCES[0]
    builtin = json.loads(source.read_text("utf-8"))
    # Составы — начало списка, остальные — резерв «Рейтинга в приоритете» (блок 71).
    # Генерация последовательная: удлинение списка первых людей не меняет.
    count = CREW_POOL
    names = random.Random(f"{seed}:names:{name}").sample(SURNAMES, len(SURNAMES))
    names += random.Random(f"{seed}:names-extra:{name}").sample(
        EXTRA_SURNAMES, count - len(names)
    )
    pool = make_engineers(
        count,
        builtin["office"],
        random.Random(f"{seed}:brigades:{name}"),
        random.Random(f"{seed}:tools:{name}"),
        names,
        random.Random(f"{seed}:speeds:{name}"),
        random.Random(f"{seed}:home-equipment:{name}"),
    )
    staff = len(builtin["engineers"])
    # Сверка — по паре «Восток × штатный»: в файле набора дома уже розданы (`give_homes`).
    if demo_raw("vostok", "staff", pool)["engineers"] != builtin["engineers"]:
        sys.exit(
            f"первые {staff} инженеров не совпали с бригадами «{name}»: "
            "наборы собраны с другим --seed"
        )
    for engineer in pool:  # старт даёт офис выбранных заявок
        del engineer["startLat"], engineer["startLon"]

    ok = True
    for requests_id in DEMO_REQUESTS:
        for crew_id, size in DEMO_CREWS.items():
            if crew_id in DEMO_TRAINING:
                continue  # учебный состав нарочно не проходит чек-лист (блок 55)
            dataset = demo_raw(requests_id, crew_id, pool)
            print(f"\n{dataset['name']}: инженеров {size}")
            ok = checklist(dataset) and ok
    if not ok:
        sys.exit("Чек-лист не выполнен: состав не записан")
    CREWS_PATH.write_text(json.dumps(pool, ensure_ascii=False, indent=2), "utf-8")
    print(f"\n    записан {CREWS_PATH.relative_to(DATA.parent)}")


# История отзывов (блок 70): у каждого человека своё «качество» — средняя, вокруг которой
# ложатся оценки. Отзывы есть у всех (блок 71, решение заказчика).
QUALITY = (3.8, 4.9)
REVIEWS_PER_PERSON = (3, 40)
HISTORY_DAYS = 120


def write_reviews(seed):
    """История отзывов демо-людей -> data/seed/reviews.json (PLAN 9.1, блок 70).

    Своим потоком на человека: появится новый человек в `crews.json` — у прежних история не
    сдвинется. Номера заявок истории шестизначные, с пятизначными заявками наборов не
    пересекаются. Дата — сколько дней назад: её превращает в дату загрузка при старте.
    """
    pool = json.loads(CREWS_PATH.read_text("utf-8"))
    history = []
    for index, engineer in enumerate(pool):
        rnd = random.Random(f"{seed}:reviews:{engineer['id']}")
        # Первое число потока до блока 71 решало, новичок ли человек. Оно по-прежнему
        # вынимается: иначе сдвинулась бы история людей, уже лежащая в базах (загрузка
        # дописывает отзывы по номеру и лишние не убирает).
        rnd.random()
        quality = rnd.uniform(*QUALITY)
        for number in range(rnd.randint(*REVIEWS_PER_PERSON)):
            stars = min(5, max(1, round(rnd.gauss(quality, 0.8))))
            text = reviews.written(rnd, stars)
            history.append(
                {
                    "requestId": str(600000 + index * 1000 + number),
                    "engineerId": engineer["id"],
                    "stars": stars,
                    "tags": [str(tag) for tag in text.tags],
                    "comment": text.comment,
                    "clientName": text.client_name,
                    "daysAgo": rnd.randint(1, HISTORY_DAYS),
                }
            )
    REVIEWS_PATH.write_text(json.dumps(history, ensure_ascii=False, indent=2), "utf-8")
    people = len({item["engineerId"] for item in history})
    print(f"отзывов {len(history)} у {people} из {len(pool)} человек")
    print(f"    записан {REVIEWS_PATH.relative_to(DATA.parent)}")


def rate_examples(dataset, seed):
    """Стартовый рейтинг бригад примера загрузки (блок 76): средняя и число оценок до дня.

    Модель оценок — как у истории демо-людей (`write_reviews`), поток на участок и бригаду:
    остальные поля набора не сдвигаются. Встроенному набору не ставится: его люди — демо-люди
    с историей в `reviews.json`, и стартовый рейтинг посчитал бы их отзывы дважды.
    """
    for engineer in dataset["engineers"]:
        rnd = random.Random(f"{seed}:rating:{dataset['name']}:{engineer['id']}")
        quality = rnd.uniform(*QUALITY)
        stars = [
            min(5, max(1, round(rnd.gauss(quality, 0.8))))
            for _ in range(rnd.randint(*REVIEWS_PER_PERSON))
        ]
        engineer["rating"] = {
            "stars": round(sum(stars) / len(stars), 2),
            "count": len(stars),
        }


def write_matrices():
    """Таблицы переездов встроенного набора и примеров -> рядом с наборами (PLAN 5.7).

    Приложение кладёт их в кэш при старте (`seed.seed_matrices`), а ключом служит отпечаток
    точек, поэтому импортированному примеру они подходят с любым новым идентификатором.
    """
    for dataset_id, source, out_path in MATRIX_TARGETS:
        if not source.exists():
            sys.exit(
                f"нет {source.relative_to(DATA.parent)}: "
                "сначала соберите наборы запуском без --matrices"
            )
        dataset = Dataset(
            id=dataset_id,
            created_at=datetime.now(UTC),
            **json.loads(source.read_text("utf-8")),
        )
        profiles = matrices.profiles(dataset)
        table = matrices.points(dataset)
        print()
        done = ready(out_path, dataset, profiles)
        if set(done) == set(profiles):
            print(f"{dataset.name}: уже посчитан, пропускаем")
            continue

        started = time.monotonic()
        if derive_transit(dataset, done) and set(done) == set(profiles):
            # Valhalla не нужна: общественный транспорт выводится из готовых таблиц.
            print(f"{dataset.name}: добавлен профиль {TRANSIT} из готовых таблиц")
            travel = matrices.Travel(dataset_id, table, done)
        else:
            print(
                f"{dataset.name}: точек {len(table)}, пар {len(table) ** 2} "
                f"на каждый из профилей {', '.join(matrices.base_profiles(profiles))}"
            )
            travel = matrices.build(dataset)
            if travel.approximate:
                sys.exit(
                    "Valhalla не ответила по профилям "
                    f"{', '.join(sorted(travel.approximate_profiles))}. Поднимите её "
                    "(docker compose --profile valhalla up -d valhalla); если она падает "
                    "по памяти, уменьшите VALHALLA_MAX_MATRIX_PAIRS и повторите."
                )

        digest = matrices.points_hash(travel.points)
        documents = [
            matrices.to_document(dataset_id, digest, travel.matrices[profile])
            for profile in profiles
        ]
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(documents, ensure_ascii=False), "utf-8")
        print(f"    посчитано за {time.monotonic() - started:.0f} с")
        print(f"    записан {out_path.relative_to(DATA.parent)}")


def main():
    parser = argparse.ArgumentParser(description="CSV кейса -> набор данных")
    parser.add_argument(
        "--brigades",
        type=int,
        help="число бригад; по умолчанию — как в контрольной выборке участка (ответ 12)",
    )
    # 1 - первый seed, на котором чек-лист (2.5) проходит на всех трёх наборах (блок 28).
    parser.add_argument("--seed", type=int, default=1, help="seed генератора бригад")
    parser.add_argument(
        "--emergency-by",
        choices=["bk", "hd"],
        default="bk",
        help="чем задана авария: тип BK «Глобальная проблема» или HD «Авария» (ответ 29.09)",
    )
    parser.add_argument(
        "--matrices",
        action="store_true",
        help="только матрицы переездов по уже записанному data/seed/dataset.json",
    )
    parser.add_argument(
        "--control",
        action="store_true",
        help="только сводки контрольной выборки и день людей -> data/seed/control.json",
    )
    parser.add_argument(
        "--crews",
        action="store_true",
        help="только инженеры для составов демо-наборов -> data/seed/crews.json",
    )
    parser.add_argument(
        "--reviews",
        action="store_true",
        help="только история отзывов демо-людей -> data/seed/reviews.json",
    )
    args = parser.parse_args()
    if args.emergency_by == "hd" and (
        args.matrices or args.control or args.crews or args.reviews
    ):
        parser.error("--emergency-by действует только на сборку наборов из CSV")

    # Отдельные режимы: синтетические CSV и геокодер не нужны, ключ 2ГИС тоже.
    if args.matrices:
        return write_matrices()
    if args.control:
        return write_control()
    if args.crews:
        return write_crews(args.seed)
    if args.reviews:
        return write_reviews(args.seed)

    # Ключ 2ГИС нужен, только если адреса нет в кэше: `geocoder.lookup` без ключа откажет,
    # а пересборка по готовому кэшу (например, `--emergency-by hd`) идёт без него.
    geocode = Geocoder()
    ok = True
    for name, out_path, csv_stem in SOURCES:
        brigades = args.brigades or control_brigades(name)
        if brigades is None:
            sys.exit(f"{name}: нет контрольной выборки в data/raw, задайте --brigades")
        dataset = build(name, geocode, brigades, args.seed, args.emergency_by)
        if out_path != SOURCES[0][1]:
            rate_examples(dataset, args.seed)
        homes = len(remote_clusters(dataset["requests"], dataset["office"]))
        print(
            f"\n{name}: заявок {len(dataset['requests'])}, бригад {brigades}"
            + (f", удалённых кластеров {homes}" if homes else "")
        )
        # Набор, не прошедший чек-лист, не записываем: иначе он затрёт рабочий встроенный.
        if not checklist(dataset):
            ok = False
            continue
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(dataset, ensure_ascii=False, indent=2), "utf-8")
        print(f"    записан {out_path.relative_to(DATA.parent)}")
        if csv_stem:
            write_csv_example(dataset, csv_stem)

    print(
        f"\nЗапросов к геокодеру: {geocode.calls}, адресов в кэше: {len(geocode.cache)}"
    )
    if geocode.misses:
        print("Не найдены (впишите координаты руками в data/raw/geocode-cache.json):")
        for address in geocode.misses:
            print(f"  {address}")
    if not ok:
        sys.exit("Чек-лист не выполнен: смените --seed или --brigades")


if __name__ == "__main__":
    main()
