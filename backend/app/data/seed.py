"""Встроенный набор данных: файл с диска -> коллекция `datasets` (PLAN 4.1 блок 4.4)."""

import json
from datetime import UTC, datetime

from pymongo import DESCENDING

from .. import db
from ..config import settings
from ..dictionaries import CREW_NAMES, Skill, Transport
from ..models import Control, Dataset, DemoCatalog, DemoCrew, DemoRequests, Scenario
from ..routing import matrices

# Встроенный набор лежит под постоянным идентификатором: матрицы (блок 5) кэшируются
# по datasetId, и после перезапуска или reset они должны остаться пригодными.
BUILTIN_ID = "builtin"

# «Дом» бригады в удалённом городе (ответ 13, PLAN 2.4). Кашира и Ступино лежат в сотне
# километров от офиса: из офиса до них не доехать в окно, а из своего города - можно.
REMOTE_M = 25_000  # дальше этого от офиса адрес считается удалённым
CLUSTER_M = 15_000  # ближе этого друг к другу удалённые адреса считаем одним городом
HOME_LOAD = 8  # сколько удалённых заявок берёт на себя одна домашняя бригада

# Демо-наборы (PLAN 6.20): заявки и инженеры выбираются по отдельности. Заявки — офис и
# заявки файлов кейса: встроенного и обоих примеров импорта; их таблицы переездов посчитаны
# заранее (PLAN 5.7), поэтому любая пара считается без Valhalla.
DEMO_REQUESTS = {
    "vostok": "seed/dataset.json",
    "yugo-vostok": "examples/yugo-vostok.json",
    "yugotsentr": "examples/yugotsentr.json",
}
# Составы — первые N инженеров одного списка (`data/seed/crews.json`): люди те же, меняется
# только их число, поэтому составы сравнимы между собой. Первые 12 — бригады встроенного
# набора; 9 — наименьший такой состав, в котором есть все три вида транспорта. Названия —
# `CREW_NAMES` в справочниках.
DEMO_CREWS = {"reduced": 9, "staff": 12, "extended": 16}
# Эта пара и есть встроенный набор: выбор её — «Вернуть демонстрационный набор».
DEMO_DEFAULT = ("vostok", "staff")


def load_builtin() -> Dataset:
    """Читает data/seed/dataset.json и проверяет его моделями."""
    path = settings.data_dir / "seed" / "dataset.json"
    raw = json.loads(path.read_text("utf-8"))
    return Dataset(
        id=BUILTIN_ID,
        built_in=True,
        created_at=datetime.now(UTC),
        control=load_control(),
        **raw,
    )


def load_control() -> Control | None:
    """Сводка контрольной выборки: data/seed/control.json (`prepare_data.py --control`).

    Справочная строка метрик, а не вход (PLAN 2.1): файла нет — строки просто нет.
    Проверяется моделью здесь, в одном месте: испорченный файл даёт `ValidationError`
    (её ловит старт сервиса), а не `TypeError` где-то при сборке набора.
    """
    path = settings.data_dir / "seed" / "control.json"
    if not path.exists():
        return None
    return Control.model_validate_json(path.read_text("utf-8"))


def load_matrices() -> list[dict]:
    """Готовые таблицы переездов встроенного набора: демо считает план и без Valhalla (PLAN 5.7).

    Файла может не быть — тогда матрицы посчитаются при первом плане (`matrices.get`).
    """
    path = settings.data_dir / "seed" / "matrices.json"
    return json.loads(path.read_text("utf-8")) if path.exists() else []


def load_example_matrices() -> list[dict]:
    """То же для наборов-примеров: `data/examples/*-matrices.json` (PLAN 5.7).

    Считаются заранее тем же `prepare_data.py --matrices`. Без них импорт примера идёт
    в Valhalla за тысячами пар — минуты ожидания на защите и риск уронить её по памяти.
    """
    examples = settings.data_dir / "examples"
    return [
        document
        for path in sorted(examples.glob("*-matrices.json"))
        for document in json.loads(path.read_text("utf-8"))
    ]


def seed_matrices() -> None:
    """Кладёт готовые таблицы в кэш по отпечатку точек (PLAN 5.4).

    Отпечаток и отсеивает устаревшее: таблица под чужие точки просто не будет найдена,
    сверять что-то здесь не нужно. Идентификатор набора в ключ не входит, поэтому таблицы
    примеров подходят импортированному набору, хотя id у него каждый раз новый.
    """
    for document in load_matrices() + load_example_matrices():
        db.matrices.replace_one(
            {"pointsHash": document["pointsHash"], "profile": document["profile"]},
            document,
            upsert=True,
        )


def load_scenarios() -> list[Scenario]:
    """Готовые цепочки событий: `data/scenarios/*.json` (PLAN 6.17).

    В файле только `name` и `events`; идентификатором служит имя файла, а набором —
    встроенный: сценарий перечисляет конкретные заявки и бригады и для чужого набора
    бессмыслен. Каталога может не быть — тогда сценариев просто нет.

    Свои `id` и `datasetId` файла перекрываются, а не спорят с нашими: сохранённый ответ
    `GET /api/scenarios` — файл ровно с этими полями, и `TypeError` из него прошёл бы мимо
    обработки в `main.lifespan` и не дал бы сервису подняться вовсе.
    """
    folder = settings.data_dir / "scenarios"
    return [
        Scenario(
            **(
                json.loads(path.read_text("utf-8"))
                | {"id": path.stem, "datasetId": BUILTIN_ID}
            )
        )
        for path in sorted(folder.glob("*.json"))
    ]


def seed_scenarios() -> None:
    """Кладёт сценарии из файлов в коллекцию `scenarios` (PLAN 5.3).

    Перезаписью по идентификатору: файл — источник истины, правка в нём должна доезжать
    до базы, а не ложиться рядом вторым документом.
    """
    for scenario in load_scenarios():
        db.scenarios.replace_one(
            {"_id": scenario.id}, scenario_to_document(scenario), upsert=True
        )


def ensure_seeded() -> None:
    """Кладёт встроенный набор при старте; лежащий в базе обновляет из файла, если его
    не правил диспетчер.

    Том Mongo переживает пересборку `data/seed/dataset.json` (новые поля, другой seed), и
    без обновления демо молча считало бы по прежнему набору до «Вернуть демонстрационный
    набор». Правленый набор (`updatedAt`) не трогаем: правки диспетчера важнее файла, а
    вернуть файл — это и есть reset. `createdAt` остаётся прежним: иначе встроенный набор
    на каждом старте становился бы текущим поверх загруженного позже (PLAN 5.4).
    """
    stored = db.datasets.find_one({"_id": BUILTIN_ID}, {"updatedAt": 1})
    if stored is None:
        db.datasets.insert_one(to_document(load_builtin()))
    else:
        document = to_document(load_builtin())
        # Сводку дописываем и правленому набору: это справка об участке, а не часть набора.
        # Файл убрали — убираем и строку, а не оставляем в базе устаревшую.
        fields = ["control"]
        if stored.get("updatedAt") is None:
            fields += ["name", "office", "requests", "engineers"]
        db.datasets.update_one(
            {"_id": BUILTIN_ID}, {"$set": {key: document[key] for key in fields}}
        )
    # Матрицы кладём всегда: том Mongo мог остаться с прошлых блоков, когда их ещё не было,
    # и тогда демо молча считало бы по прямой при живом data/seed/matrices.json.
    seed_matrices()
    # Сценарии — по той же причине и из того же каталога. В `reset_builtin` их нет:
    # от документа набора они не зависят, а файл перечитывается при каждом старте.
    seed_scenarios()


def reset_builtin() -> Dataset:
    """Перечитывает файл и делает встроенный набор текущим: у него самый свежий createdAt."""
    dataset = load_builtin()
    db.datasets.replace_one({"_id": BUILTIN_ID}, to_document(dataset), upsert=True)
    seed_matrices()
    return dataset


# --- дома бригад (ответ 13, PLAN 2.4) ------------------------------------------
#
# Здесь, а не в скрипте подготовки: демо-пару «заявки + инженеры» собирает сервер, и дома
# инженерам в удалённом городе раздаются тем же кодом, что и при подготовке наборов.


def remote_clusters(requests: list[dict], office: dict) -> list[tuple]:
    """Удалённые заявки, сгруппированные по городам: центр кластера и его размер.

    Кластеризация жадная и по расстоянию, а не по колонке «Район»: район в набор не
    попадает, а география от названий не зависит.
    """
    remote = [
        request
        for request in requests
        if request["lat"] is not None
        and matrices.haversine(
            (office["lat"], office["lon"]), (request["lat"], request["lon"])
        )
        > REMOTE_M
    ]
    clusters = []
    for request in sorted(remote, key=lambda item: (item["lat"], item["lon"])):
        point = (request["lat"], request["lon"])
        for cluster in clusters:
            if matrices.haversine(cluster[0], point) <= CLUSTER_M:
                cluster[1].append(point)
                break
        else:
            clusters.append((point, [point]))
    return sorted(clusters, key=lambda cluster: len(cluster[1]), reverse=True)


def settle_homes(engineers: list[dict], requests: list[dict], office: dict) -> int:
    """Часть бригад с автомобилем живёт в удалённом городе, а не в офисе (ответ 13).

    Пешеходу и велосипедисту дом в Кашире не поможет: туда всё равно нужен автомобиль,
    и заявки там - аварийные в том числе.
    """
    cars = iter([e for e in engineers if e["transport"] == Transport.CAR])
    settled = 0
    for center, points in remote_clusters(requests, office):
        for _ in range(max(1, round(len(points) / HOME_LOAD))):
            engineer = next(cars, None)
            if engineer is None:
                return settled
            engineer["startLat"], engineer["startLon"] = center
            settled += 1
    return settled


# --- демо-наборы (PLAN 6.20) -------------------------------------------------


def load_crew_pool() -> list[dict]:
    """Инженеры для составов, без точки старта: её даёт офис выбранных заявок
    (`prepare_data.py --crews`)."""
    path = settings.data_dir / "seed" / "crews.json"
    return json.loads(path.read_text("utf-8"))


def _demo_source(requests_id: str) -> dict:
    return json.loads(
        (settings.data_dir / DEMO_REQUESTS[requests_id]).read_text("utf-8")
    )


def demo_raw(requests_id: str, crew_id: str, pool: list[dict] | None = None) -> dict:
    """Пара «заявки + инженеры» полями набора. Неизвестный идентификатор — `KeyError`.

    Все инженеры стартуют из офиса этих заявок, а часть машин селится в удалённом городе —
    так же, как при подготовке набора. Центр кластера от состава не зависит, поэтому точки
    пары совпадают с точками файла заявок, и готовые таблицы переездов к ней подходят.
    `pool` — список ещё не записанного файла: его пары проверяет чек-лист подготовки.
    """
    source = _demo_source(requests_id)
    office = source["office"]
    engineers = [
        engineer | {"startLat": office["lat"], "startLon": office["lon"]}
        for engineer in (pool or load_crew_pool())[: DEMO_CREWS[crew_id]]
    ]
    settle_homes(engineers, source["requests"], office)
    return {
        "name": f"{source['name']} · {CREW_NAMES[crew_id].lower()}",
        "office": office,
        "requests": source["requests"],
        "engineers": engineers,
    }


def demo_catalog() -> DemoCatalog:
    """Что предложить в окне «Новый датасет»: заявки участков и составы инженеров."""
    pool = load_crew_pool()
    requests = []
    for key in DEMO_REQUESTS:
        source = _demo_source(key)
        requests.append(
            DemoRequests(id=key, name=source["name"], count=len(source["requests"]))
        )
    crews = []
    for key, count in DEMO_CREWS.items():
        # Число — по тем, кто в файле на деле: короткий файл не должен обещать лишних людей.
        people = pool[:count]
        crews.append(
            DemoCrew(
                id=key,
                name=CREW_NAMES[key],
                count=len(people),
                skills=[s for s in Skill if any(s in e["skills"] for e in people)],
                transports=[
                    t for t in Transport if any(t == e["transport"] for e in people)
                ],
            )
        )
    return DemoCatalog(requests=requests, crews=crews)


def load_demo(requests_id: str, crew_id: str) -> Dataset:
    """Делает демо-пару текущим набором. Неизвестный идентификатор — `KeyError`.

    Пара по умолчанию — встроенный набор: с ним остаются его сценарии и факт того же дня.
    Остальные лежат под постоянным идентификатором пары и перезаписываются, а не копятся.
    Готовые таблицы кладутся в кэш, как у `reset_builtin`: сервис мог подняться без Mongo, и
    тогда старт их не положил — пара ушла бы в Valhalla за тысячами пар.
    """
    if (requests_id, crew_id) == DEMO_DEFAULT:
        return reset_builtin()
    dataset = Dataset(
        id=f"demo:{requests_id}:{crew_id}",
        built_in=True,
        created_at=datetime.now(UTC),
        **demo_raw(requests_id, crew_id),
    )
    db.datasets.replace_one({"_id": dataset.id}, to_document(dataset), upsert=True)
    seed_matrices()
    return dataset


def current() -> Dataset | None:
    """Текущий набор — загруженный последним (PLAN 5.4: импорт и reset делают набор текущим)."""
    document = db.datasets.find_one(sort=[("createdAt", DESCENDING)])
    return from_document(document) if document else None


def by_id(dataset_id: str) -> Dataset | None:
    """Набор по идентификатору: перепланирование считает по набору родителя, а не по текущему."""
    document = db.datasets.find_one({"_id": dataset_id})
    return from_document(document) if document else None


def scenarios_for(dataset_id: str) -> list[Scenario]:
    """Сценарии набора. У загруженного диспетчером набора их нет — список будет пустым."""
    return [
        scenario_from_document(document)
        for document in db.scenarios.find({"datasetId": dataset_id}).sort("_id")
    ]


def scenario_by_id(scenario_id: str) -> Scenario | None:
    document = db.scenarios.find_one({"_id": scenario_id})
    return scenario_from_document(document) if document else None


def to_document(dataset: Dataset) -> dict:
    document = dataset.model_dump(by_alias=True, exclude={"id"})
    document["_id"] = dataset.id
    return document


def from_document(document: dict) -> Dataset:
    return Dataset(
        id=document["_id"], **{k: v for k, v in document.items() if k != "_id"}
    )


def scenario_to_document(scenario: Scenario) -> dict:
    document = scenario.model_dump(by_alias=True, exclude={"id"})
    document["_id"] = scenario.id
    return document


def scenario_from_document(document: dict) -> Scenario:
    return Scenario(
        id=document["_id"], **{k: v for k, v in document.items() if k != "_id"}
    )
