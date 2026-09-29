"""Общие фикстуры планирования (блок 6).

Таблицы переездов собираются руками из заданных плеч: ни Valhalla, ни Mongo тестам блока 6
не нужны. Координаты точек не участвуют в расчёте — важен только порядок, он же индекс матрицы.
Тестам, которым база всё-таки нужна, здесь же лежит фикстура `mongo`.
"""

from datetime import UTC, datetime

import pytest
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from app import db
from app.config import settings
from app.models import Dataset, Engineer, Office, Request
from app.routing import matrices as matrices_module
from app.routing.matrices import OFFICE, Matrix, Travel, point_id, start_id
from app.routing.valhalla import PROFILE_BY_TRANSPORT


@pytest.fixture(autouse=True)
def _no_retry_pause(monkeypatch):
    """Повтор запроса к Valhalla ждёт две секунды — в тестах это только замедление.

    Сам повтор остаётся: его поведение и проверяется, а пауза к делу не относится.
    """
    monkeypatch.setattr(matrices_module, "RETRY_PAUSE_SEC", 0)


# Заявка и бригада по умолчанию: смена 09:00–18:00, переезд 10 минут и 5 км.
REQUEST = {
    "id": "1",
    "address": "адрес",
    "lat": 55.71,
    "lon": 37.78,
    "serviceDurationMin": 60,
    "baseNormMin": 80,
    "windowStart": "09:00",
    "windowEnd": "12:00",
    "workPriority": "regular",
    "skill": "local",
    "requiredTransport": None,
    "requiredEquipment": [],
}
ENGINEER = {
    "id": "brigade-1",
    "name": "Бригада 1",
    "startLat": 55.70,
    "startLon": 37.77,
    "shiftStart": "09:00",
    "shiftEnd": "18:00",
    "skills": ["local"],
    "transport": "car",
    "equipment": [],
}

LEG = (600, 5000)  # 10 минут, 5 км
OFFICE_POINT = {"address": "офис", "lat": 55.70, "lon": 37.77}


@pytest.fixture
def make_request():
    def make(request_id: str = "1", **changes) -> Request:
        return Request(**(REQUEST | {"id": request_id} | changes))

    return make


@pytest.fixture
def make_engineer():
    def make(engineer_id: str = "brigade-1", **changes) -> Engineer:
        # Имя идёт за идентификатором: объяснения блока 9 печатают именно его, и две
        # бригады с одинаковым именем сделали бы их тексты нечитаемыми.
        name = f"Бригада {engineer_id.rsplit('-', 1)[-1]}"
        return Engineer(**(ENGINEER | {"id": engineer_id, "name": name} | changes))

    return make


@pytest.fixture
def make_dataset():
    """Набор данных из готовых заявок и бригад. `Dataset` наследует `Input`, поэтому его
    же принимают расчёт и отбор кандидатов."""

    def make(
        requests: list[Request], engineers: list[Engineer], dataset_id: str = "test"
    ) -> Dataset:
        return Dataset(
            id=dataset_id,
            name="набор",
            created_at=datetime.now(UTC),
            office=OFFICE_POINT,
            requests=requests,
            engineers=engineers,
        )

    return make


@pytest.fixture
def make_travel():
    """Таблицы для перечисленных заявок.

    `legs` задаёт отдельные плечи: ключ — пара идентификаторов («office» и id заявки),
    значение — `(секунды, метры)` или `None`, если пути нет.
    """

    def make(
        requests: list[Request],
        leg: tuple[int, int] = LEG,
        legs: dict[tuple[str, str], tuple[int, int] | None] | None = None,
        engineers: list[Engineer] | None = None,
    ) -> Travel:
        legs = {
            (_pid(source), _pid(target)): value
            for (source, target), value in (legs or {}).items()
        }
        # Дома бригад (PLAN 2.4) — такие же точки таблицы, как офис и заявки.
        homes = [
            start_id(Office(**OFFICE_POINT), engineer) for engineer in engineers or []
        ]
        ids = (
            [OFFICE]
            + list(dict.fromkeys(home for home in homes if home != OFFICE))
            + [point_id(request.id) for request in requests]
        )
        # Координаты точек таблицы — те же, что у самих заявок и бригад: расчёт по ним не
        # идёт (плечи задаются явно), но правка адреса сверяет их с набором, и «все точки
        # в одном месте» выглядело бы как переезд каждой из них (PLAN 6.19).
        coordinates = {OFFICE: (OFFICE_POINT["lat"], OFFICE_POINT["lon"])}
        for engineer in engineers or []:
            coordinates[start_id(Office(**OFFICE_POINT), engineer)] = (
                engineer.start_lat,
                engineer.start_lon,
            )
        for request in requests:
            coordinates[point_id(request.id)] = (request.lat, request.lon)

        matrices = {}
        for profile in set(PROFILE_BY_TRANSPORT.values()):
            values = [[legs.get((a, b), leg) for b in ids] for a in ids]
            matrices[profile] = Matrix(
                profile,
                list(ids),
                [[None if v is None else v[0] for v in row] for row in values],
                [[None if v is None else v[1] for v in row] for row in values],
            )
        return Travel("test", [(pid, *coordinates[pid]) for pid in ids], matrices)

    return make


def _pid(value: str) -> str:
    """Идентификатор точки: офис и дом бригады пишутся как есть, остальное — заявка."""
    return value if value == OFFICE or value.startswith("start:") else point_id(value)


@pytest.fixture
def mongo(monkeypatch):
    """Живая Mongo в отдельной базе: общий `planner` не должен нести состояние тестов.

    Подменяются все коллекции сразу — тесту достаточно попросить фикстуру, а какую из них
    тронет проверяемый код, знать не обязательно.
    """
    client = MongoClient(settings.mongo_url, serverSelectionTimeoutMS=2000)
    try:
        client.admin.command("ping")
    except PyMongoError:
        pytest.skip("Mongo не поднята: docker compose up -d mongo")
    collections = client["planner-test"]
    for name in ("datasets", "matrices", "plans", "geocodes", "scenarios"):
        monkeypatch.setattr(db, name, collections[name])
    yield collections
    client.drop_database("planner-test")
    client.close()
