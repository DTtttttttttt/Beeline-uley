"""Цепочка версий плана (PLAN 4.1, 6.17).

`history` показывает получившуюся цепочку версий. Своего расчёта здесь нет — каждую
версию посчитал `service.replan_plan` по событию (PLAN 6.12).
"""

from .. import db
from ..models import Diff, History, PlanVersion
from . import diff, service

# Поля строки истории. Документ плана несёт весь вход, объяснение на каждую заявку и
# геометрию маршрутов — цепочка из пяти версий встроенного набора это мегабайты, а истории
# нужны событие, режим и метрики. В базе поля лежат в camelCase: `service.to_document`
# пишет `by_alias=True`.
FIELDS = {
    "parentPlanId": 1,
    "algorithm": 1,
    "variant": 1,
    "event": 1,
    "replanMode": 1,
    "metrics": 1,
    "computeSec": 1,
    "createdAt": 1,
}


def history(plan_id: str) -> History | None:
    """Цепочка версий от первой к запрошенной и сравнение с исходной (PLAN 6.17).

    Поднимаемся по `parentPlanId`, а не спускаемся: у одной версии может быть несколько
    потомков (два события от одного плана), и «цепочка» стала бы деревом. `None` — плана нет.
    """
    document = db.plans.find_one({"_id": plan_id}, FIELDS)
    if document is None:
        return None
    versions: list[PlanVersion] = []
    while document:
        versions.append(
            PlanVersion(
                id=document["_id"],
                **{k: v for k, v in document.items() if k != "_id"},
            )
        )
        parent = document.get("parentPlanId")
        document = db.plans.find_one({"_id": parent}, FIELDS) if parent else None
    versions.reverse()
    return History(versions=versions, diff=_from_first(versions))


def _from_first(versions: list[PlanVersion]) -> Diff | None:
    """«Сравнить с исходным»: изменения первой версии к последней (PLAN 6.17).

    Это не `plan.diff` — тот считан против родителя. `diff.compare` родства не требует и
    сравнивает любые два плана, поэтому своего кода здесь нет. Сравнивать нечего, пока
    версия одна.
    """
    if len(versions) < 2:
        return None
    return diff.compare(
        service.get_plan(versions[0].id), service.get_plan(versions[-1].id)
    )
