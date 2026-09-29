"""Набор данных: текущий, загрузка файла и возврат к встроенному (PLAN 5.4)."""

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from .. import db
from ..data import geocoder, importer, seed
from ..models import (
    Dataset,
    DemoCatalog,
    DemoChoice,
    Engineer,
    NewEngineer,
    NewRequest,
    Request,
)
from ..routing import matrices

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/dataset", tags=["dataset"])


@router.get("", response_model=Dataset)
def get_dataset() -> Dataset:
    return _current()


@router.post("/import", response_model=Dataset)
def import_dataset(
    file: Annotated[UploadFile | None, File(description="JSON набора целиком")] = None,
    requests: Annotated[UploadFile | None, File(description="CSV заявок")] = None,
    engineers: Annotated[UploadFile | None, File(description="CSV бригад")] = None,
    name: Annotated[str | None, Form(description="имя набора")] = None,
) -> Dataset:
    """Загруженный набор становится текущим; встроенный остаётся в базе (PLAN 5.4).

    Обработчик намеренно синхронный, как и все остальные: геокодер, Valhalla и Mongo
    ходят блокирующими вызовами, и в `async def` они встали бы на цикл событий — загрузка
    файла без координат держала бы весь сервис минуту. Синхронный FastAPI уводит в
    пул потоков, и `/api/health` с расчётом плана продолжают отвечать.
    """
    raw = _parse(file, requests, engineers)

    geocoded = 0

    def geocode(address: str):
        nonlocal geocoded
        geocoded += 1
        return geocoder.geocode(address)

    try:
        dataset = importer.build(raw, geocode, name, _stem(file or requests))
    except importer.ImportProblem as error:
        raise HTTPException(400, str(error)) from error
    except geocoder.GeocodeError as error:
        raise HTTPException(503, str(error)) from error

    # Таблицы переездов считаются до записи набора: текущим он становится, только когда
    # по нему можно считать план. Без Valhalla они выйдут по прямой и не закэшируются —
    # план потом честно пометится approximate (PLAN 5.6).
    approximate = matrices.get(dataset).approximate_profiles
    if approximate:
        log.warning(
            "Valhalla недоступна: в наборе «%s» по прямой посчитаны профили %s",
            dataset.name,
            ", ".join(sorted(approximate)),
        )
    db.datasets.insert_one(seed.to_document(dataset))
    log.info(
        "Загружен набор «%s»: заявок %d, бригад %d, адресов через геокодер %d",
        dataset.name,
        len(dataset.requests),
        len(dataset.engineers),
        geocoded,
    )
    return dataset


@router.post("/reset", response_model=Dataset)
def reset_dataset() -> Dataset:
    return seed.reset_builtin()


@router.get("/demos", response_model=DemoCatalog)
def demo_catalog() -> DemoCatalog:
    """Демо-наборы (PLAN 6.20): заявки участков и составы инженеров выбираются по отдельности."""
    return seed.demo_catalog()


@router.post("/demo", response_model=Dataset)
def load_demo(choice: DemoChoice) -> Dataset:
    """Пара «заявки + состав» становится текущим набором; «Восток» со штатным — встроенный.

    Пара сверяется с каталогом до сборки, а не ловлей `KeyError`: иначе испорченный файл
    набора тоже отвечал бы «нет такого демо-набора» и прятал настоящую причину.
    """
    if choice.requests not in seed.DEMO_REQUESTS or choice.crew not in seed.DEMO_CREWS:
        raise HTTPException(
            404, f"Нет демо-набора: заявки «{choice.requests}», состав «{choice.crew}»"
        )
    return seed.load_demo(choice.requests, choice.crew)


# --- правка набора диспетчером (PLAN 6.19) -----------------------------------
#
# Второй путь, отличный от события: событие меняет день (что-то уже началось и остаётся
# закреплённым), а правка набора меняет исходные данные — план после неё считается с нуля.
# Прежние планы не трогаются: они ссылаются на свой вход. `POST /api/dataset/reset`
# возвращает встроенный набор — это и есть «отменить мои правки».


@router.post("/requests", response_model=Dataset)
def add_request(new: NewRequest) -> Dataset:
    """Заявка в текущий набор. Без координат — ищем адрес геокодером."""
    dataset = _current()
    if any(request.id == new.id for request in dataset.requests):
        raise HTTPException(409, f"Заявка №{new.id} уже есть в наборе")
    point = _point(new.lat, new.lon, new.address)
    request = Request.model_validate(
        new.model_dump() | {"lat": point[0], "lon": point[1]}
    )
    return _save(dataset, requests=[*dataset.requests, request])


@router.put("/requests/{request_id}", response_model=Dataset)
def update_request(request_id: str, new: NewRequest) -> Dataset:
    """Правка заявки до начала дня (PLAN 6.19): меняются исходные данные, план считается с нуля.

    Времён стопов здесь нет — они вычисляются расписанием. Новый адрес геокодируется, а его
    точка досчитывается в таблицах тем же `extend`, что и у добавленной заявки.
    """
    dataset = _current()
    if not any(request.id == request_id for request in dataset.requests):
        raise HTTPException(404, f"Заявки №{request_id} нет в наборе")
    if new.id != request_id and any(item.id == new.id for item in dataset.requests):
        raise HTTPException(409, f"Заявка №{new.id} уже есть в наборе")
    point = _point(new.lat, new.lon, new.address)
    updated = Request.model_validate(
        new.model_dump() | {"lat": point[0], "lon": point[1]}
    )
    return _save(
        dataset,
        requests=[
            updated if item.id == request_id else item for item in dataset.requests
        ],
    )


@router.delete("/requests/{request_id}", response_model=Dataset)
def remove_request(request_id: str) -> Dataset:
    dataset = _current()
    kept = [request for request in dataset.requests if request.id != request_id]
    if len(kept) == len(dataset.requests):
        raise HTTPException(404, f"Заявки №{request_id} нет в наборе")
    if not kept:
        raise HTTPException(400, "Это последняя заявка: набор без заявок не считается")
    return _save(dataset, requests=kept)


@router.post("/engineers", response_model=Dataset)
def add_engineer(new: NewEngineer) -> Dataset:
    """Бригада в текущий набор. Пустой старт означает офис (Дополнения, п. 4)."""
    dataset = _current()
    if any(engineer.id == new.id for engineer in dataset.engineers):
        raise HTTPException(409, f"Инженер {new.id} уже есть в наборе")
    start = (
        (new.start_lat, new.start_lon)
        if new.start_lat is not None and new.start_lon is not None
        else (dataset.office.lat, dataset.office.lon)
    )
    engineer = Engineer.model_validate(
        new.model_dump() | {"start_lat": start[0], "start_lon": start[1]}
    )
    return _save(dataset, engineers=[*dataset.engineers, engineer])


@router.put("/engineers/{engineer_id}", response_model=Dataset)
def update_engineer(engineer_id: str, new: NewEngineer) -> Dataset:
    """Правка бригады до начала дня (PLAN 6.19). Пустой старт означает офис."""
    dataset = _current()
    if not any(item.id == engineer_id for item in dataset.engineers):
        raise HTTPException(404, f"Инженера {engineer_id} нет в наборе")
    if new.id != engineer_id and any(item.id == new.id for item in dataset.engineers):
        raise HTTPException(409, f"Инженер {new.id} уже есть в наборе")
    start = (
        (new.start_lat, new.start_lon)
        if new.start_lat is not None and new.start_lon is not None
        else (dataset.office.lat, dataset.office.lon)
    )
    updated = Engineer.model_validate(
        new.model_dump() | {"start_lat": start[0], "start_lon": start[1]}
    )
    return _save(
        dataset,
        engineers=[
            updated if item.id == engineer_id else item for item in dataset.engineers
        ],
    )


@router.delete("/engineers/{engineer_id}", response_model=Dataset)
def remove_engineer(engineer_id: str) -> Dataset:
    dataset = _current()
    kept = [item for item in dataset.engineers if item.id != engineer_id]
    if len(kept) == len(dataset.engineers):
        raise HTTPException(404, f"Инженера {engineer_id} нет в наборе")
    if not kept:
        raise HTTPException(
            400, "Это последний инженер: набор без инженеров не считается"
        )
    return _save(dataset, engineers=kept)


def _current() -> Dataset:
    dataset = seed.current()
    if dataset is None:
        raise HTTPException(404, "Набор данных не загружен")
    return dataset


def _point(lat: float | None, lon: float | None, address: str) -> tuple[float, float]:
    """Координаты из тела запроса, иначе из геокодера — как при загрузке файла (PLAN 5.4)."""
    if lat is not None and lon is not None:
        return lat, lon
    try:
        point = geocoder.geocode(address)
    except geocoder.GeocodeError as error:
        raise HTTPException(503, str(error)) from error
    if point is None:
        raise HTTPException(404, f"Адрес «{address}» не найден в 2ГИС")
    return point


def _save(
    dataset: Dataset,
    requests: list[Request] | None = None,
    engineers: list[Engineer] | None = None,
) -> Dataset:
    """Записывает правленый набор и достраивает его таблицы переездов.

    Таблицы досчитываются `extend`: новые точки стоят два запроса на профиль, остальное
    берётся из прежней таблицы. `createdAt` не трогаем — набор и так текущий, а поднимать
    его над загруженным позже правка не должна.
    """
    travel = matrices.get(dataset)
    updated = dataset.model_copy(
        update={
            "requests": requests if requests is not None else dataset.requests,
            "engineers": engineers if engineers is not None else dataset.engineers,
            "updated_at": datetime.now(UTC),
        }
    )
    matrices.extend(travel, updated)
    db.datasets.replace_one({"_id": updated.id}, seed.to_document(updated))
    log.info(
        "Набор «%s» изменён: заявок %d, бригад %d",
        updated.name,
        len(updated.requests),
        len(updated.engineers),
    )
    return updated


def _parse(
    file: UploadFile | None,
    requests: UploadFile | None,
    engineers: UploadFile | None,
) -> dict:
    """Один JSON или два CSV (офис — строкой в CSV заявок) — смесь не принимаем."""
    if file is not None and (requests is not None or engineers is not None):
        raise HTTPException(400, "Загрузите либо JSON («file»), либо два CSV")
    try:
        if file is not None:
            return importer.from_json(file.file.read())
        if requests is not None and engineers is not None:
            return importer.from_csv(requests.file.read(), engineers.file.read())
    except importer.ImportProblem as error:
        raise HTTPException(400, str(error)) from error
    raise HTTPException(
        400, "Нужен JSON набора («file») или два CSV («requests» и «engineers»)"
    )


def _stem(upload: UploadFile | None) -> str | None:
    """Имя набора из имени файла — последняя попытка, когда своего имени нет ни у чего."""
    return Path(upload.filename).stem if upload and upload.filename else None
