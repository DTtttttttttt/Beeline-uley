"""Загрузка набора данных из файла: JSON или два CSV (PLAN 5.4, блок 12.2).

Разбор нарочно тонкий: значения передаются моделям строками, приведение и все проверки
входа делает pydantic (PLAN 6.1) — второй копии правил в импорте нет. Ошибки собираются
все сразу и называют место: «заявки, строка 7: windowEnd: ...».
"""

import csv
import io
import json
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import ValidationError

from ..models import Dataset, Engineer, Office, Request

DELIMITER = ";"
DEFAULT_NAME = "Загруженный набор"
# Больше двух десятков строк ошибок не читают, а полный список раздувает ответ.
MAX_PROBLEMS = 20

# Колонки CSV — поля 5.1 в том же написании, что и в JSON (PLAN 5.4).
REQUEST_COLUMNS = [
    "id",
    "address",
    "lat",
    "lon",
    "serviceDurationMin",
    "baseNormMin",
    "windowStart",
    "windowEnd",
    "workPriority",
    "skill",
    "requiredTransport",
    "requiredEquipment",
    "requiredTools",
    "urgent",
    "hdType",
]
ENGINEER_COLUMNS = [
    "id",
    "name",
    "startLat",
    "startLon",
    "shiftStart",
    "shiftEnd",
    "skills",
    "transport",
    "equipment",
    "tools",
]
# Последняя строка CSV заявок — «Адрес офиса;<адрес>», как в исходных файлах кейса
# (Дополнения, п. 4): офис едет в самом файле, а не вводится при загрузке (PLAN 5.4).
OFFICE_LABEL = "Адрес офиса"
# Списки в CSV — через запятую внутри одной ячейки (PLAN 5.4).
LIST_COLUMNS = {"requiredEquipment", "requiredTools", "skills", "equipment", "tools"}

# Подпись места («заявки, строка 7», «заявка 3») и поля этого места.
Item = tuple[str, dict]
Geocode = Callable[[str], tuple[float, float] | None]


class ImportProblem(Exception):
    """Файл не принят. `problems` — по сообщению на каждое найденное место."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        text = "; ".join(problems[:MAX_PROBLEMS])
        if len(problems) > MAX_PROBLEMS:
            text += f"; и ещё {len(problems) - MAX_PROBLEMS}"
        super().__init__(text)


def where(error: dict) -> str:
    """«windowEnd: ...» для поля и просто текст для проверки всей модели."""
    place = ".".join(str(part) for part in error["loc"])
    return f"{place}: {error['msg']}" if place else error["msg"]


# --- разбор файлов -----------------------------------------------------------


def decode(data: bytes) -> str:
    """utf-8 (в том числе с BOM), иначе cp1251 — так сохраняет Excel на русской Windows.

    Ни та, ни другая кодировка не подошла — это чужой файл (xlsx вместо csv, UTF-16),
    и он обязан отвечать ошибкой загрузки, а не 500.
    """
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ImportProblem(["файл не читается ни как UTF-8, ни как Windows-1251"])


def from_csv(requests: bytes, engineers: bytes) -> dict:
    """Два CSV; офис — последняя непустая строка файла заявок, бригадам он нужен как старт."""
    request_text, office = _split_office(decode(requests))
    request_items, problems = _rows(request_text, "заявки")
    engineer_items, engineer_problems = _rows(decode(engineers), "инженеры")
    problems += engineer_problems
    if office is None:
        problems.insert(
            0, f"заявки: последней строкой должен идти «{OFFICE_LABEL};<адрес>»"
        )
    if problems:
        raise ImportProblem(problems)
    return {
        "name": None,
        "office": {"address": office},
        "requests": request_items,
        "engineers": engineer_items,
    }


def _split_office(text: str) -> tuple[str, str | None]:
    """Текст заявок без строки офиса и сам адрес; `None` — строки нет.

    Строка берётся только последняя: номера строк в ошибках остаются номерами файла,
    а «Адрес офиса» посреди заявок разбирался бы как заявка и падал на ней с понятной
    ошибкой. Регистр не важен — в исходных файлах встречаются оба написания.
    """
    lines = text.splitlines()
    while lines and not lines[-1].strip(";, \t"):
        lines.pop()
    if not lines:
        return text, None
    label, _, address = lines[-1].partition(DELIMITER)
    address = address.strip().strip(DELIMITER).strip()
    if label.strip().casefold() != OFFICE_LABEL.casefold() or not address:
        return text, None
    return "\n".join(lines[:-1]), address


def _rows(text: str, title: str) -> tuple[list[Item], list[str]]:
    reader = csv.DictReader(io.StringIO(text), delimiter=DELIMITER)
    if not reader.fieldnames:
        return [], [f"{title}: файл пуст"]

    items, problems = [], []
    for row in reader:
        label = f"{title}, строка {reader.line_num}"
        if None in row:  # значений в строке больше, чем колонок в заголовке
            problems.append(f"{label}: значений больше, чем колонок в заголовке")
            continue
        # Пустая ячейка выбрасывается: дальше работает умолчание модели
        # (requiredTransport: null, requiredEquipment: [], urgent: false).
        fields = {
            column.strip(): value.strip()
            for column, value in row.items()
            if column and (value or "").strip()
        }
        if not fields:  # пустая строка в конце файла
            continue
        for column in LIST_COLUMNS & fields.keys():
            fields[column] = [
                part.strip() for part in fields[column].split(",") if part.strip()
            ]
        items.append((label, fields))
    return items, problems


def from_json(data: bytes) -> dict:
    """`{name?, office, requests, engineers}` — то же, что пишет scripts/prepare_data.py."""
    try:
        raw = json.loads(decode(data))
    except json.JSONDecodeError as error:
        raise ImportProblem([f"файл не разбирается как JSON: {error}"]) from error
    if not isinstance(raw, dict):
        raise ImportProblem(["ожидается объект {office, requests, engineers}"])

    problems, parts = [], {}
    for key, title in (("requests", "заявка"), ("engineers", "инженер")):
        value = raw.get(key)
        if not isinstance(value, list):
            problems.append(f"«{key}»: ожидается список")
            continue
        parts[key] = []
        for number, item in enumerate(value, start=1):
            if isinstance(item, dict):
                parts[key].append((f"{title} {number}", dict(item)))
            else:
                problems.append(f"{title} {number}: ожидается объект")
    if not isinstance(raw.get("office"), dict):
        problems.append("«office»: ожидается объект с адресом")
    if problems:
        raise ImportProblem(problems)
    return {"name": raw.get("name"), "office": dict(raw["office"]), **parts}


# --- сборка набора -----------------------------------------------------------


def build(
    raw: dict,
    geocode: Geocode,
    name: str | None = None,
    fallback_name: str | None = None,
) -> Dataset:
    """Разобранный файл -> `Dataset`. Адреса без координат добираются геокодером.

    Имя набора берётся по убыванию: `name` — заданное при загрузке (иначе переименовать
    набор было бы нельзя и два импорта одного примера не отличить друг от друга), затем
    имя внутри файла, затем `fallback_name` — имя файла. Последнее стоит ниже своего
    имени набора: «Юго-восток» из файла понятнее, чем «yugo-vostok» из его названия.
    """
    problems: list[str] = []
    office = _office(raw["office"], geocode, problems)

    requests = []
    for label, fields in raw["requests"]:
        filled = _with_point(fields, "lat", "lon", label, geocode, problems)
        if filled and (model := _model(Request, filled, label, problems)):
            requests.append(model)

    engineers = []
    for label, fields in raw["engineers"]:
        # Все бригады стартуют из офиса (Дополнения, п. 4). Пустые координаты старта
        # берём у него: вручную их не заполнить — офис геокодируется уже на сервере.
        if office is not None and not fields.get("startLat"):
            fields = fields | {"startLat": office.lat, "startLon": office.lon}
        if model := _model(Engineer, fields, label, problems):
            engineers.append(model)

    # Файл из одних заголовков разбирается без ошибок, но набор из него пустой: молча
    # сделать такой текущим - оставить диспетчера с пустым экраном без единого сообщения.
    for items, title in ((requests, "заявок"), (engineers, "инженеров")):
        if not items:
            problems.append(f"в файле нет {title}")

    if problems:
        raise ImportProblem(problems)
    try:
        return Dataset(
            id=uuid4().hex,
            name=name or raw.get("name") or fallback_name or DEFAULT_NAME,
            created_at=datetime.now(UTC),
            office=office,
            requests=requests,
            engineers=engineers,
        )
    except ValidationError as error:
        # Сюда доходят проверки набора целиком: повторяющиеся идентификаторы (PLAN 6.1).
        raise ImportProblem([where(problem) for problem in error.errors()]) from error


def _office(fields: dict, geocode: Geocode, problems: list[str]) -> Office | None:
    filled = _with_point(dict(fields), "lat", "lon", "офис", geocode, problems)
    return _model(Office, filled, "офис", problems) if filled else None


def _with_point(
    fields: dict, lat: str, lon: str, label: str, geocode: Geocode, problems: list[str]
) -> dict | None:
    """Координаты из файла, иначе из геокодера. `None` — место уже попало в ошибки."""
    if fields.get(lat) not in (None, "") and fields.get(lon) not in (None, ""):
        return fields
    address = fields.get("address")
    if not address:
        problems.append(f"{label}: нет ни координат, ни адреса")
        return None
    point = geocode(address)
    if point is None:
        problems.append(f"{label}: адрес «{address}» не найден в 2ГИС")
        return None
    return fields | {lat: point[0], lon: point[1]}


def _model(kind, fields: dict, label: str, problems: list[str]):
    try:
        return kind(**fields)
    except ValidationError as error:
        problems.extend(f"{label}: {where(problem)}" for problem in error.errors())
        return None


# --- выгрузка: примеры формата загрузки (PLAN 12.3) --------------------------


def to_csv(rows: list[dict], columns: list[str], office: str | None = None) -> str:
    """Тот же формат, что читает `_rows`: пример не может разойтись с разбором.

    `office` — адрес офиса последней строкой: так пишется CSV заявок.
    """
    out = io.StringIO()
    writer = csv.DictWriter(
        out, fieldnames=columns, delimiter=DELIMITER, lineterminator="\n"
    )
    writer.writeheader()
    for row in rows:
        writer.writerow({column: _cell(row.get(column)) for column in columns})
    if office is not None:
        out.write(f"{OFFICE_LABEL}{DELIMITER}{office}\n")
    return out.getvalue()


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ",".join(str(part) for part in value)
    return str(value)
