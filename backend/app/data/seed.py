"""Встроенный набор данных: файл с диска -> коллекция `datasets` (PLAN 4.1 блок 4.4)."""

import json
import random
from datetime import UTC, datetime, timedelta

from pydantic import TypeAdapter
from pymongo import DESCENDING

from .. import db
from ..config import settings
from ..dictionaries import CREW_NAMES, DurationMode, ReviewSource, Skill, Transport
from ..models import (
    Control,
    Dataset,
    DemoCatalog,
    DemoCrew,
    DemoRequests,
    Review,
)
from ..routing import matrices

# Встроенный набор лежит под постоянным идентификатором: матрицы (блок 5) кэшируются
# по datasetId, и после перезапуска или reset они должны остаться пригодными.
BUILTIN_ID = "builtin"
# Пространство людей встроенного и демо-наборов (`profiles.namespace`): люди одни, из `crews.json`.
DEMO_SPACE = "demo"

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
DEMO_CREWS = {"reduced": 9, "staff": 12, "extended": 16, "shortage": 5}
# Сколько людей в `crews.json`: составы берут его начало, остальные — резерв режима «Рейтинг
# в приоритете» (PLAN 9.1, блок 71). У загруженного набора резерв добирается до того же числа.
CREW_POOL = 40
# Учебные составы (блок 55): людей заведомо меньше, чем заявок, — показать отказы и «Что
# поможет». Чек-лист набора (2.5) к ним не применяется, в замеры README они не входят.
DEMO_TRAINING = {"shortage"}
# Эта пара и есть встроенный набор: выбор её — «Вернуть демонстрационный набор».
DEMO_DEFAULT = ("vostok", "staff")


def load_builtin(mode: DurationMode = DurationMode.COMPLEXITY) -> Dataset:
    """Читает data/seed/dataset.json и проверяет его моделями.

    Режим длительности (PLAN 9.1) — не часть файла, а выбор диспетчера; по умолчанию демо
    считается со сложностью.
    """
    path = settings.data_dir / "seed" / "dataset.json"
    raw = json.loads(path.read_text("utf-8"))
    return Dataset(
        id=BUILTIN_ID,
        built_in=True,
        created_at=datetime.now(UTC),
        control=load_control(DEMO_DEFAULT[0]),
        duration_mode=mode,
        **raw,
    )


# Файл целиком — словарь «участок -> сводка»: прежний плоский формат или список падают
# `ValidationError` на старте, а не молча убирают строку у всех наборов.
CONTROLS = TypeAdapter(dict[str, Control])


def load_control(requests_id: str) -> Control | None:
    """Сводка контрольной выборки участка: data/seed/control.json (`prepare_data.py --control`).

    Файл — словарь по участкам демо-наборов (`DEMO_REQUESTS`): контрольный день — про заявки
    участка, а не про состав, поэтому сводку несёт любая пара с этими заявками (блок 51).
    Справочная строка метрик, а не вход (PLAN 2.1): файла или участка нет — строки просто нет.
    Проверяется моделью здесь, в одном месте: испорченный файл даёт `ValidationError`
    (её ловит старт сервиса), а не `TypeError` где-то при сборке набора.
    """
    path = settings.data_dir / "seed" / "control.json"
    if not path.exists():
        return None
    return CONTROLS.validate_json(path.read_text("utf-8")).get(requests_id)


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


def load_reviews() -> list[dict]:
    """История отзывов демо-людей (`prepare_data.py --reviews`): нет файла — нет истории."""
    path = settings.data_dir / "seed" / "reviews.json"
    return json.loads(path.read_text("utf-8")) if path.exists() else []


def seed_reviews(now: datetime | None = None) -> None:
    """Кладёт историю отзывов демо-людей (PLAN 9.1, блок 70) в пространство `DEMO_SPACE`.

    В файле — сколько дней назад, а не дата: иначе «изменение за месяц» старело бы вместе с
    файлом. Перезапись по ключу: номера истории шестизначные и с заявками наборов не
    пересекаются, поэтому живые оценки она не трогает.
    """
    now = now or datetime.now(UTC)
    for item in load_reviews():
        review = Review(
            request_id=item["requestId"],
            engineer_id=item["engineerId"],
            stars=item["stars"],
            tags=item["tags"],
            comment=item["comment"],
            client_name=item["clientName"],
            source=ReviewSource.DEMO,
            at=now - timedelta(days=item["daysAgo"]),
        )
        db.reviews.replace_one(
            {"namespace": DEMO_SPACE, "requestId": review.request_id},
            {"namespace": DEMO_SPACE} | review.model_dump(by_alias=True),
            upsert=True,
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
    stored = db.datasets.find_one(
        {"_id": BUILTIN_ID}, {"updatedAt": 1, "durationMode": 1}
    )
    if stored is None:
        db.datasets.insert_one(to_document(load_builtin()))
    else:
        document = to_document(load_builtin())
        # Сводку дописываем и правленому набору: это справка об участке, а не часть набора.
        # Файл убрали — убираем и строку, а не оставляем в базе устаревшую.
        fields = ["control"]
        if stored.get("updatedAt") is None:
            fields += ["name", "office", "requests", "engineers"]
        # Набор из базы до блока 66 режима не знает: выбор диспетчера не трогаем, а
        # отсутствующий режим — умолчание демо (PLAN 9.1).
        if "durationMode" not in stored:
            fields.append("durationMode")
        db.datasets.update_one(
            {"_id": BUILTIN_ID}, {"$set": {key: document[key] for key in fields}}
        )
    # Демо-пары, лежащие в базе, — тем же правилом: пересборка `crews.json` или файла заявок
    # (блок 47 — смены 2/2 и 5/2) иначе доезжала бы до пары только при повторном выборе, а
    # текущей парой стенд считал бы события по прежним данным. Пару, которой больше нет в
    # каталоге, не трогаем: собрать её нечем.
    # Сводку участка — и правленой паре, как встроенному набору (блок 51).
    for stored_pair in db.datasets.find(
        {"_id": {"$regex": "^demo:"}}, {"_id": 1, "updatedAt": 1, "durationMode": 1}
    ):
        _, requests_id, crew_id = stored_pair["_id"].split(":")
        if requests_id not in DEMO_REQUESTS or crew_id not in DEMO_CREWS:
            continue
        document = to_document(
            Dataset(
                id=stored_pair["_id"],
                created_at=datetime.now(UTC),
                duration_mode=DurationMode.COMPLEXITY,
                **demo_raw(requests_id, crew_id),
            )
        )
        fields = ["control"]
        if stored_pair.get("updatedAt") is None:
            fields += ["name", "office", "requests", "engineers"]
        if "durationMode" not in stored_pair:
            fields.append("durationMode")
        db.datasets.update_one(
            {"_id": stored_pair["_id"]},
            {"$set": {key: document[key] for key in fields}},
        )
    # Матрицы кладём всегда: том Mongo мог остаться с прошлых блоков, когда их ещё не было,
    # и тогда демо молча считало бы по прямой при живом data/seed/matrices.json.
    seed_matrices()
    seed_reviews()


def reset_builtin(mode: DurationMode = DurationMode.COMPLEXITY) -> Dataset:
    """Перечитывает файл и делает встроенный набор текущим: у него самый свежий createdAt."""
    dataset = load_builtin(mode)
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
    remote = [request for request in requests if _remote(request, office) is True]
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


def _remote(request: dict, office: dict) -> bool | None:
    """Дальше ли заявка `REMOTE_M` от офиса; без координат — `None`, ни туда ни сюда."""
    if request["lat"] is None:
        return None
    point = (request["lat"], request["lon"])
    return matrices.haversine((office["lat"], office["lon"]), point) > REMOTE_M


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
                break
            engineer["startLat"], engineer["startLon"] = center
            settled += 1
    give_homes(engineers, requests, office)
    return settled


def give_homes(engineers: list[dict], requests: list[dict], office: dict) -> None:
    """Дом есть у каждого (блок 73): адрес — уже геокодированный адрес заявки набора.

    Заселённый в удалённый город живёт в центре кластера и стартует из дома; остальные
    живут в пределах `REMOTE_M` от офиса и стартуют, как раньше, из офиса — точки пары и
    готовые таблицы переездов не меняются. Дом зависит от участка, поэтому в `crews.json`
    лежит только домашнее оборудование, а адрес выбирается здесь — своим потоком на человека.
    """
    near = [request for request in requests if _remote(request, office) is False]
    for engineer in engineers:
        start = (engineer["startLat"], engineer["startLon"])
        if start == (office["lat"], office["lon"]):
            place = random.Random(f"home:{engineer['id']}").choice(near)
        else:
            place = next(r for r in requests if (r["lat"], r["lon"]) == start)
        engineer["home"] = {
            "address": place["address"],
            "lat": place["lat"],
            "lon": place["lon"],
            "equipment": engineer.pop("homeEquipment"),
        }


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
        "control": load_control(requests_id),
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
                training=key in DEMO_TRAINING,
            )
        )
    return DemoCatalog(requests=requests, crews=crews)


def load_demo(
    requests_id: str,
    crew_id: str,
    mode: DurationMode = DurationMode.COMPLEXITY,
) -> Dataset:
    """Делает демо-пару текущим набором. Неизвестный идентификатор — `KeyError`.

    Пара по умолчанию — встроенный набор: с ним остаётся факт того же дня.
    Остальные лежат под постоянным идентификатором пары и перезаписываются, а не копятся.
    Готовые таблицы кладутся в кэш, как у `reset_builtin`: сервис мог подняться без Mongo, и
    тогда старт их не положил — пара ушла бы в Valhalla за тысячами пар.
    """
    if (requests_id, crew_id) == DEMO_DEFAULT:
        return reset_builtin(mode)
    dataset = demo_dataset(requests_id, crew_id, mode)
    db.datasets.replace_one({"_id": dataset.id}, to_document(dataset), upsert=True)
    seed_matrices()
    return dataset


def demo_dataset(
    requests_id: str, crew_id: str, mode: DurationMode = DurationMode.COMPLEXITY
) -> Dataset:
    """Демо-пара набором, без записи в базу: её же считает прогрев кэша решений."""
    if (requests_id, crew_id) == DEMO_DEFAULT:
        return load_builtin(mode)
    return Dataset(
        id=f"demo:{requests_id}:{crew_id}",
        built_in=True,
        created_at=datetime.now(UTC),
        duration_mode=mode,
        **demo_raw(requests_id, crew_id),
    )


def current() -> Dataset | None:
    """Текущий набор — загруженный последним (PLAN 5.4: импорт и reset делают набор текущим)."""
    document = db.datasets.find_one(sort=[("createdAt", DESCENDING)])
    return from_document(document) if document else None


def by_id(dataset_id: str) -> Dataset | None:
    """Набор по идентификатору: перепланирование считает по набору родителя, а не по текущему."""
    document = db.datasets.find_one({"_id": dataset_id})
    return from_document(document) if document else None


def to_document(dataset: Dataset) -> dict:
    document = dataset.model_dump(by_alias=True, exclude={"id"})
    document["_id"] = dataset.id
    return document


def from_document(document: dict) -> Dataset:
    return Dataset(
        id=document["_id"], **{k: v for k, v in document.items() if k != "_id"}
    )
