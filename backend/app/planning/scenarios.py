"""Цепочки событий и цепочки версий (PLAN 4.1, 6.17).

Две стороны одного: `run` прогоняет готовый сценарий событием за событием, `history`
показывает уже получившуюся цепочку версий. Своего расчёта здесь нет — каждая версия
считается тем же `service.replan_plan`, что и событие, набитое руками (PLAN 6.12).
"""

from .. import db
from ..models import Dataset, Diff, History, Plan, PlanVersion, Scenario
from . import diff, replan, service

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


def run(parent: Plan, dataset: Dataset, scenario: Scenario) -> list[Plan]:
    """Сценарий целиком: событие за событием, каждое — от версии предыдущего (PLAN 6.17).

    Режим — тот же, что у события без выбора диспетчера (`replan.default_mode`): обычная
    заявка встраивается, остальное пересчитывает остаток с момента события. `full` не
    бывает никогда: сценарий — это прожитый день, а не сравнительный расчёт «как если бы
    событие было известно с утра» (PLAN 6.12).

    Отказ в середине отменяет весь ответ: половина сценария выглядела бы как успех. Уже
    посчитанные версии остаются в базе — планы неизменяемы, и каждая доступна по своему
    `id`. К причине приписывается номер события: их в сценарии несколько, и без номера
    непонятно, какое именно не применилось.

    На версии, не прошедшей проверку, цепочка обрывается: сохранить её `_finish` отказался,
    и следующее событие считалось бы от плана, которого в базе нет.
    """
    total = len(scenario.events)
    versions: list[Plan] = []
    for number, event in enumerate(scenario.events, start=1):
        try:
            parent = service.replan_plan(
                parent, dataset, event, replan.default_mode(event)
            )
        except replan.ReplanError as error:
            raise replan.ReplanError(
                error.status,
                f"Сценарий «{scenario.name}»: событие {number} из {total} "
                f"в {event.time} — {error}",
            ) from error
        versions.append(parent)
        if not parent.validation.ok:
            # Не прошедшая проверку версия не сохранена (`service._finish`), и считать от
            # неё следующее событие значит наплодить версии с `parentPlanId` в никуда.
            # Дальше не идём: ответом всё равно будет 500 по этой версии.
            break
    return versions
