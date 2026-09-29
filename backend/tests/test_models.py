import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.config import Settings, settings
from app.models import Dataset, Engineer, Request

REQUEST = {
    "id": "74198",
    "address": "Москва, Волгоградский проспект, дом 128 корпус 5",
    "lat": 55.700915,
    "lon": 37.782227,
    "serviceDurationMin": 70,
    "baseNormMin": 90,
    "windowStart": "20:00",
    "windowEnd": "22:00",
    "workPriority": "new_connection",
    "skill": "connection",
    "requiredTransport": None,
    "requiredEquipment": ["router"],
    "urgent": False,
}
ENGINEER = {
    "id": "brigade-1",
    "name": "Бригада 1",
    "startLat": 55.702293,
    "startLon": 37.77392,
    "shiftStart": "09:00",
    "shiftEnd": "18:00",
    "skills": ["local", "emergency"],
    "transport": "car",
    "equipment": [],
}


def request(**changes):
    return REQUEST | changes


def engineer(**changes):
    return ENGINEER | changes


def test_request_and_engineer_are_read():
    assert Request(**REQUEST).skill == "connection"
    assert Engineer(**ENGINEER).transport == "car"


@pytest.mark.parametrize(
    "changes",
    [
        {"windowStart": "9:00"},  # формат не HH:MM
        {"windowEnd": "25:00"},
        {"windowStart": "22:00", "windowEnd": "20:00"},  # конец раньше начала
        {"windowStart": "20:00", "windowEnd": "20:00"},  # окно нулевой длины
        {"skill": "networking"},  # нет в справочнике
        {"workPriority": "urgent"},  # срочность - это не тип работы
        {"requiredTransport": "helicopter"},
        {"requiredEquipment": ["modem"]},  # неизвестное оборудование
        {"requiredEquipment": ["router", "router"]},
        {"requiredTools": ["hammer"]},  # неизвестный инструмент
        {"requiredTools": ["laptop", "laptop"]},
        {"serviceDurationMin": 0},
        {"baseNormMin": 0},
        {"lat": 60.0},  # вне вырезки OSM
        {"lon": 12.0},
        {"id": ""},
        {"comment": "лишнее поле"},  # extra="forbid"
    ],
)
def test_bad_request(changes):
    with pytest.raises(ValidationError):
        Request(**request(**changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"skills": []},  # навыков 1-3
        {"skills": ["local", "connection", "emergency", "local"]},
        {"skills": ["local", "local"]},
        {"skills": ["driving"]},
        {"transport": "scooter"},
        {"equipment": ["router", "router"]},
        {"tools": ["hammer"]},
        {"tools": ["laptop", "laptop"]},
        {"shiftStart": "18:00", "shiftEnd": "09:00"},
        {"shiftEnd": "24:00"},
        {"startLat": 60.0},
        {"name": ""},
    ],
)
def test_bad_engineer(changes):
    with pytest.raises(ValidationError):
        Engineer(**engineer(**changes))


def dataset(**changes):
    base = {
        "id": "test",
        "name": "Тест",
        "office": {"address": "Москва, офис", "lat": 55.702293, "lon": 37.77392},
        "requests": [REQUEST],
        "engineers": [ENGINEER],
        "builtIn": False,
        "createdAt": datetime.now(UTC),
    }
    return Dataset(**(base | changes))


def test_duplicate_ids_are_rejected():
    with pytest.raises(ValidationError):
        dataset(requests=[REQUEST, request()])
    with pytest.raises(ValidationError):
        dataset(engineers=[ENGINEER, engineer()])


def test_order_is_kept():
    """Базовый вариант (PLAN 6.4) идёт по исходному порядку - модель его не меняет."""
    ids = [request(id="2"), request(id="1"), request(id="3")]
    assert [r.id for r in dataset(requests=ids).requests] == ["2", "1", "3"]


def test_builtin_dataset_is_valid():
    """Встроенный набор целиком проходит валидацию и сериализуется обратно в camelCase."""
    raw = json.loads((settings.data_dir / "seed" / "dataset.json").read_text("utf-8"))
    loaded = Dataset(id="builtin", built_in=True, created_at=datetime.now(UTC), **raw)

    assert len(loaded.requests) == 66
    assert len(loaded.engineers) == 12  # как в контрольной выборке участка (ответ 12)
    dumped = loaded.model_dump(by_alias=True, mode="json")
    assert (
        dumped["requests"][0]["serviceDurationMin"]
        == raw["requests"][0]["serviceDurationMin"]
    )
    assert dumped["engineers"][0]["shiftStart"] == raw["engineers"][0]["shiftStart"]
    assert dumped["builtIn"] is True


def test_equipment_capacity_rejects_an_unknown_transport():
    """Опечатка в ключе вместимости падает при старте, а не значит «без ограничения»."""
    with pytest.raises(ValidationError):
        Settings(equipment_capacity={"pedestrian": 2})
