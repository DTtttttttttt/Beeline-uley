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
# Кэш решений солвера (PLAN 6.5, блок 45): ключ — отпечаток входа в `_id`, другого индекса
# не нужно.
solutions = db.solutions
# Вложения ассистента (блок 60): текст файла живёт сутки — переписка дольше не нужна.
attachments = db.attachments
# Скорость инженеров по выполненным заявкам (блок 68): одна запись на человека в пространстве.
profiles = db.engineer_profiles
# Отзывы клиентов (блок 70): одна оценка на заявку в пространстве людей.
reviews = db.reviews


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
    profiles.create_index(
        [("namespace", ASCENDING), ("engineerId", ASCENDING)], unique=True
    )
    reviews.create_index(
        [("namespace", ASCENDING), ("requestId", ASCENDING)], unique=True
    )
    reviews.create_index([("namespace", ASCENDING), ("engineerId", ASCENDING)])
    plans.create_index([("parentPlanId", ASCENDING)])
    # Черновики предпросмотра (PLAN 6.12, блок 53) живут до `draftExpiresAt`. У применённого
    # плана поле пустое, а TTL не трогает документы без даты в нём.
    plans.create_index([("draftExpiresAt", ASCENDING)], expireAfterSeconds=0)
    geocodes.create_index([("address", ASCENDING)], unique=True)
    # Вложения ассистента живут сутки (блок 60): переписка о файле дольше не длится.
    attachments.create_index([("createdAt", ASCENDING)], expireAfterSeconds=24 * 3600)
    # Кэш решений живёт месяц: правка кода меняет отпечаток (PLAN 6.5), и прежние ключи
    # больше никто не спросит, а потерянная запись стоит лишь одного поиска.
    solutions.create_index(
        [("createdAt", ASCENDING)], expireAfterSeconds=30 * 24 * 3600
    )


def ping() -> bool:
    try:
        client.admin.command("ping")
        return True
    except PyMongoError:
        return False
