"""Вложения для ассистента (блок 60): файл диспетчера → текст для модели и сводка для чата.

Ассистент читает CSV, JSON и TXT — без новых зависимостей (решение заказчика, развилка № 4
плана реализации). Файл формата загрузки (PLAN 5.4) узнаётся тем же `importer`, что разбирает
импорт: сводка тогда говорит «12 заявок формата загрузки», а не «CSV, 13 строк». Модели текст
уходит как данные, а не инструкции (`assistant._files`).
"""

from pathlib import PurePath

from .config import settings
from .data import importer
from .models import NewEngineer, NewRequest
from .planning import explain

# Колонки, без которых CSV — не формат загрузки: обязательные поля моделей набора (координаты
# заявки и старт бригады в импорте необязательны — их находит геокодер или берётся офис).
REQUIRED = {
    model: {field.alias for field in model.model_fields.values() if field.is_required()}
    for model in (NewRequest, NewEngineer)
}
# Сколько байт принимает одно вложение: больше — уже не «файл к сообщению», а набор данных,
# и для него есть импорт.
MAX_BYTES = 1024 * 1024
KINDS = {".csv": "csv", ".json": "json", ".txt": "txt"}


class Refused(Exception):
    """Файл не принят: `status` — код ответа (413 — велик, 415 — не тот тип), текст — для чата."""

    def __init__(self, status: int, text: str):
        super().__init__(text)
        self.status = status


def read(name: str, data: bytes) -> dict:
    """`{kind, text, summary}` по имени и содержимому файла; не тот файл — `Refused`."""
    kind = KINDS.get(PurePath(name).suffix.lower())
    if kind is None:
        raise Refused(415, "Ассистент читает только файлы CSV, JSON и TXT")
    if len(data) > MAX_BYTES:
        raise Refused(
            413, "Файл больше 1 МБ — большой набор загружают в «Новый датасет»"
        )
    try:
        text = importer.decode(data)
    except importer.ImportProblem as error:
        raise Refused(
            415, "Файл не читается как текст в UTF-8 или Windows-1251"
        ) from error
    limit = settings.assistant_attachment_chars
    cut = limit if len(text) > limit else None
    summary = explain.attachment_summary(kind, cut=cut, **_counts(kind, text, data))
    if cut:
        text = text[:limit] + "\n…обрезано"
    return {"kind": kind, "text": text, "summary": summary}


def _counts(kind: str, text: str, data: bytes) -> dict:
    """Что сказать о файле: строки, а у формата загрузки — сколько в нём заявок и инженеров."""
    lines = sum(1 for line in text.splitlines() if line.strip())
    if kind == "csv":
        first = text.splitlines()[0] if text.strip() else ""
        # Excel дописывает к строке пустую ячейку («…;hdType;») — она не колонка.
        header = {cell.strip() for cell in first.split(importer.DELIMITER)} - {""}
        for columns, model, key, title in (
            (importer.REQUEST_COLUMNS, NewRequest, "requests", "заявки"),
            (importer.ENGINEER_COLUMNS, NewEngineer, "engineers", "инженеры"),
        ):
            # Все обязательные колонки есть, чужих нет; необязательные можно опустить.
            if REQUIRED[model] <= header <= set(columns):
                body, _office = importer._split_office(text)
                items, _problems = importer._rows(body, title)
                return {"lines": lines, key: len(items)}
    if kind == "json":
        try:
            raw = importer.from_json(data)
        except importer.ImportProblem:
            return {"lines": lines}
        return {
            "lines": lines,
            "requests": len(raw["requests"]),
            "engineers": len(raw["engineers"]),
        }
    return {"lines": lines}
