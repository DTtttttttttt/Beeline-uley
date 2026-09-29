from contextlib import suppress

from pymongo import ASCENDING, MongoClient
from pymongo.errors import OperationFailure, PyMongoError

from .config import settings

# tz_aware: Mongo хранит время в UTC без пояса, и без этого флага прочитанный
# createdAt был бы наивным, а только что созданный — с поясом.
client = MongoClient(settings.mongo_url, serverSelectionTimeoutMS=2000, tz_aware=True)
db = client[settings.mongo_db]

datasets = db.datasets
matrices = db.matrices
plans = db.plans
geocodes = db.geocodes
scenarios = db.scenarios


def ensure_indexes() -> None:
    datasets.create_index([("name", ASCENDING)])
    # Ключ кэша матриц — отпечаток точек, а не набор (PLAN 5.4): одни и те же точки
    # считаются один раз, и предпосчитанные таблицы примера подходят импортированному
    # набору, хотя идентификатор у него каждый раз новый.
    matrices.create_index(
        [("pointsHash", ASCENDING), ("profile", ASCENDING)], unique=True
    )
    # Прежний уникальный индекс остался бы в базе с прошлых запусков и запрещал бы
    # вторую таблицу того же набора — например, встроенного после пересборки с другим seed.
    with suppress(OperationFailure):
        matrices.drop_index("datasetId_1_profile_1")
    plans.create_index([("datasetId", ASCENDING)])
    plans.create_index([("parentPlanId", ASCENDING)])
    geocodes.create_index([("address", ASCENDING)], unique=True)
    scenarios.create_index([("datasetId", ASCENDING)])


def ping() -> bool:
    try:
        client.admin.command("ping")
        return True
    except PyMongoError:
        return False
