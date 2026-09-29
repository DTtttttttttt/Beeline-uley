"""Причины неназначения, объяснения назначений и текст маршрута (PLAN 6.8, 6.9).

Единственное место с текстами для диспетчера, кроме справочника русских названий. Своей
проверки ограничений здесь нет: коды берутся у `schedule.first_violation`, разбор ресурса —
у `resource_checks`, цена вставки — у `best_insertion`. Единицы переводит `timeutil`.
"""

from ..dictionaries import (
    EQUIPMENT_NAMES,
    OUTCOME_NAMES,
    SKILL_NAMES,
    TOOL_NAMES,
    TRANSPORT_NAMES,
    WORK_PRIORITY_NAMES,
    Skill,
    WorkPriority,
)
from ..models import (
    Candidate,
    Closure,
    Engineer,
    Explanation,
    Input,
    Request,
    RequestForecast,
    Slot,
    Unassigned,
)
from ..routing.matrices import DETOUR, Travel
from ..routing.valhalla import PROFILE_BY_TRANSPORT
from ..timeutil import to_clock, to_clock_over_midnight, to_km, to_minutes
from .schedule import (
    Position,
    Violation,
    Visit,
    best_insertion,
    build_schedule,
    capacity,
    first_violation,
    idle_slots,
    latest_start,
    meters,
    over_capacity,
    resource_checks,
    units,
)

# Каскад PLAN 6.8: чем дальше бригада прошла по проверке, тем старше код причины. Берём
# самое дальнее продвижение по всем бригадам — это и есть «нет навыка» → «у подходящих по
# навыку нет транспорта» → …, только одним проходом.
RANK = {
    Violation.NO_SKILL: 0,
    Violation.NO_TRANSPORT: 1,
    Violation.NO_EQUIPMENT: 2,
    Violation.NO_CAPACITY: 3,
    Violation.NO_ROUTE: 4,
    Violation.WINDOW_END: 5,
    Violation.SHIFT_END: 5,
}
# Окно и смена — одна причина для диспетчера: заявка не помещается по времени.
CODE = {
    Violation.NO_SKILL: "NO_SKILL",
    Violation.NO_TRANSPORT: "NO_TRANSPORT",
    Violation.NO_EQUIPMENT: "NO_EQUIPMENT",
    Violation.NO_CAPACITY: "NO_CAPACITY",
    Violation.NO_ROUTE: "NO_ROUTE",
    Violation.WINDOW_END: "NO_TIME",
    Violation.SHIFT_END: "NO_TIME",
}
NOT_FITTED = "NOT_FITTED"
# Не вывод расчёта, а решение диспетчера: заявку отложили на завтра (PLAN 6.19).
DEFERRED = "DEFERRED"
SWAP_TEXT = {
    Violation.WINDOW_END: "нарушит окно заявки",
    Violation.SHIFT_END: "не уложится в смену",
    Violation.NO_ROUTE: "нет пути между точками",
}


def explain_requests(
    data: Input,
    travel: Travel,
    routes: dict[str, list[Request]],
    visits: dict[str, list[Visit]],
    unassigned: list[Request],
    positions: dict[str, Position] | None = None,
    risk: dict[str, RequestForecast] | None = None,
) -> tuple[list[Unassigned], dict[str, Explanation]]:
    """Причины неназначения и объяснение по каждой заявке набора (PLAN 6.8, 6.9).

    Один проход на обе задачи: текст причины считается один раз и попадает и в `unassigned`,
    и в объяснение заявки — иначе формулировки разъехались бы.

    `risk` — прогноз опозданий (PLAN 6.18): у назначенной заявки он дописывает строку о риске
    и запасе до конца окна. У неназначенной риска нет — опаздывать не с чем.
    """
    positions = positions or {}
    left = {request.id for request in unassigned}
    placed = {
        request.id: (engineer, visit)
        for engineer in data.engineers
        for request, visit in zip(routes[engineer.id], visits[engineer.id], strict=True)
    }

    items: list[Unassigned] = []
    explanations: dict[str, Explanation] = {}
    for request in data.requests:
        candidates = [
            _candidate(request, engineer, routes, visits, travel, positions)
            for engineer in data.engineers
        ]
        if request.id in left:
            code, reason = _reason(request, data, travel, visits, positions)
            items.append(
                Unassigned(request_id=request.id, reason_code=code, reason_text=reason)
            )
            text = _unassigned_text(request, reason, data, candidates)
        else:
            engineer, visit = placed[request.id]
            text = _assigned_text(
                request,
                engineer,
                visit,
                data,
                candidates,
                (risk or {}).get(request.id),
            )
        explanations[request.id] = Explanation(text=text, candidates=candidates)

    return items, explanations


def route_text(
    engineer: Engineer,
    requests: list[Request],
    travel: Travel,
    position: Position,
) -> str:
    """Почему маршрут такой: порядок, пробег, окончание и что даст перестановка соседей (PLAN 6.9)."""
    if not requests:
        return f"{engineer.name}: заявок нет, инженер не задействован."

    visits = build_schedule(engineer, requests, travel, position)
    base = meters(visits)
    # Вместимость показываем только тем, у кого она есть: у автомобиля ограничения нет.
    limit = capacity(engineer)
    carried = (
        ""
        if limit is None
        else f" Оборудование: {position.equipment_used + units(requests)} из "
        f"{limit} ед. на день."
    )
    head = (
        f"{engineer.name}: {_plural(len(requests), ('заявка', 'заявки', 'заявок'))}, "
        f"{_km(base)} км, окончание {to_clock(visits[-1].end)} "
        f"при смене до {engineer.shift_end}.{carried} "
        "Порядок задан окнами заявок и длиной смены."
    )
    swaps = [
        f"№{requests[i].id} ↔ №{requests[i + 1].id} — "
        f"{_swap(engineer, requests, i, travel, position, base)}"
        for i in range(len(requests) - 1)
    ]
    return head + (f" Перестановка соседних: {'; '.join(swaps)}." if swaps else "")


# Какие работы открывает навык (PLAN 2.3): по нему бригада вообще попадает в кандидаты.
WORK_BY_SKILL = {
    Skill.LOCAL: "локальные заявки",
    Skill.CONNECTION: "подключения и дозаказы",
    Skill.EMERGENCY: "аварии",
}


def assignment_text(
    engineer: Engineer, requests: list[Request], visits: list[Visit], load: float
) -> str:
    """«Почему эти заявки у этой бригады» — объяснение уровня бригады (PLAN 6.9, блок 29).

    Ресурсы бригады, какие работы они ей открывают, что она взяла и сколько смены это
    занимает. `requests` и `visits` — весь её день, вместе с закреплённым; `load` — загрузка
    смены из метрик: правило у неё одно, и второй копии формулы здесь нет.
    """
    head = (
        f"{engineer.name}: навыки {_quoted(SKILL_NAMES[s] for s in engineer.skills)}; "
        f"транспорт «{TRANSPORT_NAMES[engineer.transport]}»; "
        f"оборудование {_quoted(EQUIPMENT_NAMES[e] for e in engineer.equipment) or 'нет'}; "
        f"инструменты {_quoted(TOOL_NAMES[t] for t in engineer.tools) or 'нет'}. "
    )
    open_ = ", ".join(
        WORK_BY_SKILL[skill] for skill in Skill if skill in engineer.skills
    )
    closed = [WORK_BY_SKILL[skill] for skill in Skill if skill not in engineer.skills]
    # Навык — единственное, что открывает работу целиком. Транспорт, оборудование и
    # инструменты требует конкретная заявка, поэтому о них честно сказать «доступно» нельзя.
    access = f"По навыкам открыты {open_}" + (
        f"; закрыты — {', '.join(closed)}" if closed else ""
    )
    access += (
        "; дальше решают транспорт, оборудование и инструменты, которые требует заявка"
    )
    gone = (
        f" С {engineer.unavailable_from} не работает — новых заявок не берёт."
        if engineer.unavailable_from
        else ""
    )
    if not requests:
        return f"{head}{access}.{gone} Заявок нет — инженер не задействован."

    kinds = {kind: 0 for kind in WorkPriority}
    for request in requests:
        kinds[request.work_priority] += 1
    split = ", ".join(
        f"{WORK_PRIORITY_NAMES[kind].lower()} — {count}"
        for kind, count in kinds.items()
        if count
    )
    return (
        f"{head}{access}.{gone} В маршруте "
        f"{_plural(len(requests), ('заявка', 'заявки', 'заявок'))} ({split}), "
        f"{_km(meters(visits))} км; переезды и работы занимают {round(load * 100)} % "
        f"смены {engineer.shift_start}–{engineer.shift_end}."
    )


def approximate_note(data: Input, travel: Travel) -> str | None:
    """Оповещение: чьи километры и времена посчитаны по прямой, а не по дорогам (PLAN 5.6).

    Называем транспорт, а не профиль Valhalla: диспетчер видит бригады, а `pedestrian` ему
    ни о чём не говорит. Общественный транспорт выводится из пешехода и автомобиля
    (PLAN 3.3), поэтому отказ любого из них помечает приблизительным и его.
    """
    if not travel.approximate_profiles:
        return None

    used = dict.fromkeys(engineer.transport for engineer in data.engineers)
    struck = [
        transport
        for transport in used
        if PROFILE_BY_TRANSPORT[transport] in travel.approximate_profiles
    ]
    head = (
        f"Valhalla не ответила, расстояния посчитаны по прямой (× {DETOUR} "
        "и городская скорость): "
    )
    if len(struck) == len(used):
        return head + "километры и времена всего плана приблизительные."
    return head + (
        f"у инженеров с транспортом {_quoted(map(TRANSPORT_NAMES.get, struck))} "
        "километры и времена приблизительные, у остальных — по дорогам."
    )


EDIT_REFUSAL = {
    Violation.NO_SKILL: "у инженера больше нет нужного навыка",
    Violation.NO_TRANSPORT: "транспорт инженера больше не подходит",
    Violation.NO_EQUIPMENT: "у инженера больше нет нужного оборудования или инструмента",
    Violation.NO_CAPACITY: "столько оборудования на его транспорте за день не унести",
    Violation.NO_ROUTE: "до адреса больше не добраться на его транспорте",
    Violation.WINDOW_END: "работа выходит за окно заявки",
    Violation.SHIFT_END: "работа выходит за смену",
}


def edit_refusal(
    engineer: Engineer, committed: list[Request], violation: Violation
) -> str:
    """Почему правку бригады не приняли: она ломает уже начатую работу (PLAN 6.19)."""
    numbers = ", ".join(f"№{request.id}" for request in committed)
    return (
        f"{engineer.name}: правка ломает уже начатую работу ({numbers}) — "
        f"{EDIT_REFUSAL[violation]}"
    )


def edit_moves_committed(
    engineer: Engineer, request: Request, departure: int, time: int
) -> str:
    """Почему правку не приняли: по новым полям бригада выехала бы уже после события."""
    return (
        f"{engineer.name}: по правленым полям выезд к заявке №{request.id} — "
        f"{to_clock_over_midnight(departure)}, а на {to_clock(time)} эта работа уже "
        "начата. Такую правку принимает только пересчёт всего дня заново."
    )


def manual_refusal(
    engineer: Engineer,
    request: Request,
    violation: Violation,
    visit: Visit | None,
    route: list[Request],
    spots: list[int],
) -> str:
    """Почему заявку сюда поставить нельзя и куда её поставить можно (PLAN 6.16, шаг 4).

    `request` — та заявка, из-за которой маршрут стал недопустим: вставка сдвигает всё,
    что стоит после неё, поэтому виноватой может оказаться и соседняя работа.
    """
    return (
        f"Нельзя: {engineer.name} — {_manual_reason(engineer, request, violation, visit)}"
        + (_spots_text(route, spots))
    )


def manual_breaks_route(
    engineer: Engineer,
    moved: Request,
    request: Request,
    violation: Violation,
    visit: Visit | None,
) -> str:
    """Отказ по прежней бригаде: без снятой заявки её маршрут не строится (PLAN 6.16, шаг 3)."""
    return (
        f"Нельзя: без заявки №{moved.id} маршрут не строится — {engineer.name}, "
        f"{_manual_reason(engineer, request, violation, visit)}"
    )


def _manual_reason(
    engineer: Engineer, request: Request, violation: Violation, visit: Visit | None
) -> str:
    """Нарушение маршрута — фразой для диспетчера, с той цифрой, на которой всё упёрлось.

    Имя бригады стоит перед фразой отдельно и в именительном падеже: имена приходят из
    чужого файла, и склонять их нечем — «у Андреев нет навыка» читалось бы хуже, чем
    «Андреев — нет навыка».
    """
    if violation is Violation.NO_SKILL:
        return f"нет навыка «{SKILL_NAMES[request.skill]}»"
    if violation is Violation.NO_TRANSPORT:
        return (
            f"транспорт «{TRANSPORT_NAMES[engineer.transport]}», а заявке "
            f"№{request.id} нужен «{TRANSPORT_NAMES[request.required_transport]}»"
        )
    if violation is Violation.NO_EQUIPMENT:
        return f"нет {_lacking(request, [engineer])}"
    if violation is Violation.NO_CAPACITY:
        # Говорим про маршрут целиком: заявке может быть нужна одна единица, но вместе с
        # уже взятыми их больше, чем бригада унесёт за день.
        return (
            f"оборудования в маршруте стало бы больше {capacity(engineer)} ед. — "
            f"столько на транспорте «{TRANSPORT_NAMES[engineer.transport]}» за день "
            "не унести"
        )
    if violation is Violation.NO_ROUTE:
        return (
            f"до заявки №{request.id} не добраться на транспорте "
            f"«{TRANSPORT_NAMES[engineer.transport]}»"
        )
    if violation is Violation.WINDOW_END:
        return (
            f"заявка №{request.id} началась бы "
            f"в {to_clock_over_midnight(visit.start)}, окно до {request.window_end}"
        )
    return (
        f"заявка №{request.id} закончилась бы в {to_clock_over_midnight(visit.end)}, "
        f"смена до {engineer.shift_end}"
    )


def _spots_text(route: list[Request], spots: list[int]) -> str:
    """Допустимые позиции — местами в маршруте, а не номерами: номер диспетчер не вводит.

    Перечисляются, только если они есть (PLAN 6.16, шаг 4): у отказа по навыку или
    оборудованию их не бывает вовсе, и приписка «других мест нет» была бы шумом.
    """
    if not spots:
        return ""
    places = ", ".join(
        "в конец маршрута" if index == len(route) else f"перед №{route[index].id}"
        for index in spots
    )
    return f". Поставить можно: {places}"


def reaction_text(minutes: int) -> str:
    """Почему окно аварии, пришедшей днём, короче присланного (PLAN 6.12, ответ 19).

    Без этой оговорки окно в два часа выглядело бы опечаткой в заявке.
    """
    return (
        f"Конец окна — срок реакции на аварию: {minutes} мин с момента, когда работу "
        "можно начать."
    )


def inserted_text(time: str, placed: bool) -> str:
    """Приписка к объяснению заявки, встроенной без пересчёта (PLAN 6.12, режим `insert`)."""
    if placed:
        return (
            f"Заявка поступила в {time} и встроена в свободное время инженера; "
            "остальной план ради неё не пересматривался."
        )
    return (
        f"Заявка поступила в {time}; остальной план ради неё не пересматривался. "
        "Чтобы искать ей место перестановкой других работ, пересчитайте день "
        "с момента события."
    )


def deferred_text(request: Request, time: str) -> str:
    """Подпись под перенесённой заявкой: решение диспетчера, а не вывод расчёта (PLAN 6.19)."""
    return (
        f"Заявка №{request.id} ({WORK_PRIORITY_NAMES[request.work_priority]}) перенесена "
        f"на следующий день: решение диспетчера в {time}. В расчёт остатка дня она не входит."
    )


def closed_text(request: Request, engineer: Engineer, closure: Closure) -> str:
    """Подпись под закрытой заявкой вместо таблицы кандидатов (PLAN 6.19, ответ 1).

    Кандидатов у неё нет и быть не может: выбор не просто сделан, а уже исполнен — диспетчер
    записал, чем кончилась работа. Показывать рядом «а подошёл бы Егоров» значило бы
    предлагать пересмотреть факт.
    """
    return (
        f"Заявка №{request.id} закрыта диспетчером: "
        f"{OUTCOME_NAMES[closure.outcome].lower()}, {engineer.name}, {closure.time}. "
        "Факт записан со слов инженера, в перепланирование заявка больше не входит."
    )


def failed_text(request: Request, engineer: Engineer, closure: Closure) -> str:
    """Почему невыполненная заявка осталась без исполнителя (PLAN 6.8, 6.19).

    Код причины у неё общий с переносом — `DEFERRED`: для диспетчера это одна строка
    «сегодня не делаем», а чем именно кончилось, говорит текст.
    """
    return (
        f"{engineer.name}: заявку №{request.id} "
        f"({WORK_PRIORITY_NAMES[request.work_priority].lower()}) выполнить невозможно — "
        f"факт в {closure.time}. "
        "Заявка переносится на следующий день и в расчёт остатка дня не входит."
    )


def _requests_list(request_ids: list[str], data: Input) -> str:
    """«заявку №5» или «3 заявки» и построчно номер с адресом: диспетчер проверяет, что выбрано.

    У списка адреса важнее номеров: «все заявки в Кашире» модель выбирает по адресам, и
    ошибку выбора — лишний город, чужой адрес — видно только в них.
    """
    if len(request_ids) == 1:
        return f"заявку №{request_ids[0]}"
    address = {request.id: request.address for request in data.requests}
    lines = "\n".join(f"• №{id} — {address.get(id, '')}" for id in request_ids)
    count = _plural(len(request_ids), ("заявку", "заявки", "заявок"))
    return f"{count}:\n{lines}\n"


def event_text(event, data: Input) -> str:
    """Что сделает событие, разобранное ассистентом из текста диспетчера (блок 42).

    Диспетчер видит эту фразу под кнопкой «Применить» и решает, то ли модель поняла. Имена
    берутся из входа плана, а не из ответа модели: показывать нужно то, что реально уйдёт.
    """
    if event.type == "engineer_unavailable":
        name = next((e.name for e in data.engineers if e.id == event.engineer_id), "")
        return f"Сделать недоступным: {name} с {event.time}"
    if event.type == "cancel_request":
        return f"Отменить {_requests_list(event.request_ids, data)} в {event.time}"
    if event.type == "defer_request":
        return (
            f"Перенести {_requests_list(event.request_ids, data)} "
            f"на следующий день, {event.time}"
        )
    if event.type == "close_request":
        end = f", работа кончилась в {event.actual_end}" if event.actual_end else ""
        return (
            f"Закрыть заявку №{event.request_id}: "
            f"{OUTCOME_NAMES[event.outcome].lower()}, {event.time}{end}"
        )
    raise ValueError(f"событие {event.type} ассистент не разбирает")


def close_refusal(request_id: str, closure: Closure) -> str:
    """Заявка уже закрыта: факт записывают один раз, и пересматривать его нечем.

    Один текст на два отказа — на повторное закрытие и на попытку переставить закрытую
    заявку вручную: диспетчеру в обоих случаях нужно одно и то же, чем и когда она кончилась.
    """
    return (
        f"Заявка №{request_id} уже закрыта в {closure.time}: "
        f"{OUTCOME_NAMES[closure.outcome].lower()}"
    )


def close_not_started(request: Request, name: str, departure: str, time: int) -> str:
    """Закрыть можно только начатую работу: к ней бригада выехала не позже события."""
    return (
        f"Работа ещё не начиналась: к заявке №{request.id} {name} выезжает в {departure}, "
        f"а событие в {to_clock(time)}"
    )


def close_before_departure(request: Request, at: str, departure: str) -> str:
    """Факт не может кончиться раньше, чем бригада к заявке выехала."""
    return (
        f"Заявка №{request.id}: фактическое окончание {at} раньше выезда {departure} — "
        "работа не могла кончиться до того, как инженер к ней поехал"
    )


def close_over_closed(request: Request, later: str) -> str:
    """Невыполненной нельзя объявить работу, после которой у бригады уже закрыт факт."""
    return (
        f"Заявку №{request.id} нельзя закрыть как невыполненную: после неё у этой же "
        f"инженера уже закрыта заявка №{later}"
    )


def committed_text(request: Request, engineer: Engineer, time: int) -> str:
    """Объяснение закреплённой заявки при перепланировании (PLAN 6.12, шаг 1).

    Таблицу кандидатов ей не строим и не переносим из прошлой версии: выбор уже сделан и
    пересмотру не подлежит, а прежние кандидаты к моменту события устарели.
    """
    return (
        f"Заявка №{request.id} "
        f"({WORK_PRIORITY_NAMES[request.work_priority].lower()}) закреплена: "
        f"{engineer.name}. На момент события {to_clock(time)} работа уже шла или инженер "
        "был в пути, поэтому назначение не пересматривалось."
    )


def committed_prefix(requests: list[Request], time: int) -> str:
    """Приписка перед текстом маршрута: что закреплено и дальше не двигается.

    Бригаду называет сам текст маршрута, поэтому здесь имени нет — иначе оно повторялось бы
    в одной фразе дважды.
    """
    if not requests:
        return ""
    names = ", ".join(f"№{request.id}" for request in requests)
    return (
        f"К {to_clock(time)} закреплено "
        f"{_plural(len(requests), ('заявка', 'заявки', 'заявок'))} ({names}) — "
        "они не пересматриваются, дальше только остаток дня. "
    )


# --- причины неназначения (PLAN 6.8) -----------------------------------------


def _reason(
    request: Request,
    data: Input,
    travel: Travel,
    visits: dict[str, list[Visit]],
    positions: dict[str, Position],
) -> tuple[str, str]:
    """Код и текст причины. Проверка — «влезает ли заявка в пустой маршрут бригады» (PLAN 6.3)."""
    engineers = {engineer.id: engineer for engineer in data.engineers}
    violations = {
        engineer.id: first_violation(
            engineer, [request], travel, positions[engineer.id]
        )
        for engineer in data.engineers
    }

    fitting = [engineers[eid] for eid, item in violations.items() if item is None]
    if fitting:
        return NOT_FITTED, _not_fitted_text(fitting, visits)
    if not violations:
        # Набор без бригад: исполнителя нет ни у одной заявки — ровно то, о чём NO_SKILL.
        return CODE[Violation.NO_SKILL], _stuck_text(
            request, Violation.NO_SKILL, [], travel, positions
        )

    worst = max(violations.values(), key=lambda violation: RANK[violation])
    stuck = [engineers[eid] for eid, item in violations.items() if item == worst]
    return CODE[worst], _stuck_text(request, worst, stuck, travel, positions)


def _stuck_text(
    request: Request,
    worst: Violation,
    stuck: list[Engineer],
    travel: Travel,
    positions: dict[str, Position],
) -> str:
    """Текст причины по самому дальнему нарушению и бригадам, которые на нём остановились."""
    if worst == Violation.NO_SKILL:
        return f"Нет исполнителя с навыком «{SKILL_NAMES[request.skill]}»"

    if worst == Violation.NO_TRANSPORT:
        name = TRANSPORT_NAMES[request.required_transport]
        return f"Нужен транспорт «{name}» — у инженеров с нужным навыком его нет"

    if worst == Violation.NO_EQUIPMENT:
        # Чего не хватает хоть кому-то из подходящих по навыку: у одной бригады нет роутера,
        # у другой ноутбука — тогда полного набора нет ни у кого, и назвать нужно оба.
        return (
            f"Не хватает {_lacking(request, stuck)} — "
            "такого набора нет ни у одного подходящего инженера"
        )

    if worst == Violation.NO_CAPACITY:
        # Оборудование есть, но за раз его больше, чем бригада унесёт за день (или чем у неё
        # осталось после начатой работы, PLAN 6.12).
        # Не меньше нуля: закреплённая часть могла уйти за вместимость, посчитанная при
        # другой настройке, — «не больше −1» диспетчеру ничего не скажет.
        left = max(max(0, capacity(e) - positions[e.id].equipment_used) for e in stuck)
        transports = _quoted(
            dict.fromkeys(TRANSPORT_NAMES[engineer.transport] for engineer in stuck)
        )
        return (
            f"Нужно {units([request])} ед. оборудования — инженеры с транспортом "
            f"{transports} на день унесут не больше {left}"
        )

    if worst == Violation.NO_ROUTE:
        transports = _quoted(
            dict.fromkeys(TRANSPORT_NAMES[engineer.transport] for engineer in stuck)
        )
        return (
            f"До адреса не добраться на транспорте подходящих инженеров: {transports}"
        )

    # Окно или смена: показываем, на чём именно упирается ближайшая подходящая бригада.
    # `build_schedule` здесь не падает: до времени доходят только проезжие маршруты.
    # Времена печатает `to_clock_over_midnight`: причина описывает как раз то, чего в плане
    # быть не может, — работу за полночь или сутки пути, и падать на них нельзя.
    visits = {
        engineer.id: build_schedule(
            engineer, [request], travel, positions[engineer.id]
        )[0]
        for engineer in stuck
    }
    head = (
        f"Не помещается в окно {request.window_start}–{request.window_end} "
        "или в смену: "
    )
    if worst == Violation.WINDOW_END:
        arrival = min(visit.arrival for visit in visits.values())
        return head + (
            f"ближайший инженер прибудет в {to_clock_over_midnight(arrival)}"
        )

    soonest = min(stuck, key=lambda engineer: visits[engineer.id].end)
    return head + (
        f"{soonest.name} — окончание работы "
        f"в {to_clock_over_midnight(visits[soonest.id].end)} "
        f"при смене до {soonest.shift_end}"
    )


def _not_fitted_text(fitting: list[Engineer], visits: dict[str, list[Visit]]) -> str:
    """Бригады, в чей пустой маршрут заявка встала бы, и чем они заняты сейчас."""
    busy = [
        f"{engineer.name} (на заявке №{visits[engineer.id][-1].request_id} "
        f"до {to_clock(visits[engineer.id][-1].end)})"
        if visits[engineer.id]
        else f"{engineer.name} (день свободен, но заявка в маршрут не встаёт)"
        for engineer in fitting
    ]
    return (
        "Не помещается в найденный план без перераспределения других работ. "
        f"Подходят: {', '.join(busy)}"
    )


# --- объяснения заявок (PLAN 6.9) --------------------------------------------


def _candidate(
    request: Request,
    engineer: Engineer,
    routes: dict[str, list[Request]],
    visits: dict[str, list[Visit]],
    travel: Travel,
    positions: dict[str, Position],
) -> Candidate:
    """Бригада в таблице объяснения: ✓/✗ по навыку, транспорту, оборудованию и времени.

    Прирост считается от маршрута **без** объясняемой заявки. У своей бригады это разница
    с планом — та цена, которую заявка стоит на самом деле; `best_insertion` дал бы самую
    дешёвую из вставок, а солвер мог поставить стоп и не в самое дешёвое место. У чужой
    бригады другой цены и нет: там прирост — как раз цена лучшей вставки.

    `timeOk` — «заявка помещается в день этой бригады»: у своей это факт плана, у чужой —
    успешная вставка. Не прошедшая по ресурсу бригада вставки не получает.

    Свободные окна берутся от **текущего** маршрута бригады, вместе с объясняемой заявкой:
    диспетчер смотрит на день таким, какой он есть.
    """
    checks = resource_checks(request, engineer)
    position = positions[engineer.id]
    route = [item for item in routes[engineer.id] if item.id != request.id]
    mine = len(route) != len(routes[engineer.id])

    if first_violation(engineer, route, travel, position) is Violation.NO_ROUTE:
        # Без объясняемой заявки маршрут непроезжий: объезд её точки — другое плечо матрицы,
        # и его может не быть (PLAN 3.4). Прирост тогда не от чего считать.
        extra = None
    elif mine:
        extra = meters(visits[engineer.id]) - meters(
            build_schedule(engineer, route, travel, position)
        )
    else:
        spot = best_insertion(engineer, route, request, travel, position)
        extra = None if spot is None else spot[1]

    # «Оборудование ✓» — и вид есть, и в дневную вместимость заявка ещё влезает: иначе
    # бригада без места в рюкзаке получала бы «не помещается в день», хотя дело не во времени.
    carries = (
        mine
        or not request.required_equipment
        or not over_capacity(
            engineer, position.equipment_used + units(route) + units([request])
        )
    )
    return Candidate(
        engineer_id=engineer.id,
        skill_ok=checks[Violation.NO_SKILL],
        transport_ok=checks[Violation.NO_TRANSPORT],
        equipment_ok=checks[Violation.NO_EQUIPMENT] and carries,
        time_ok=mine or extra is not None,
        extra_km=None if extra is None else to_km(extra),
        free_slots=[
            Slot(start=to_clock(begin), end=to_clock(finish))
            for begin, finish in idle_slots(engineer, visits[engineer.id], position)
        ],
    )


def _assigned_text(
    request: Request,
    engineer: Engineer,
    visit: Visit,
    data: Input,
    candidates: list[Candidate],
    risk: RequestForecast | None = None,
) -> str:
    """«Почему заявка у этой бригады»: учтённые ограничения и сравнение с остальными."""
    road = request.base_norm_min - request.service_duration_min
    travel_min = to_minutes(visit.travel_sec)
    # Раннее прибытие даёт ожидание (PLAN 6.2) — в объяснении это видно, а не скрыто.
    begin = (
        f"ожидание до {to_clock(visit.start)}, начало в окне"
        if visit.start > visit.arrival
        else f"начало {to_clock(visit.start)} в окне"
    )
    mine = next(item for item in candidates if item.engineer_id == engineer.id)
    return (
        f"Заявка №{request.id} "
        f"({WORK_PRIORITY_NAMES[request.work_priority].lower()}) "
        f"назначена: {engineer.name}. "
        f"Учтено: навык «{SKILL_NAMES[request.skill]}» есть; "
        f"{_equipment_clause(request)}; {_transport_clause(request)}; "
        f"переезд {travel_min} мин, прибытие {to_clock(visit.arrival)}, "
        f"{begin} {request.window_start}–{request.window_end}; "
        f"норматив {request.base_norm_min} мин = {road} дорога + "
        f"{request.service_duration_min} работа, фактическая дорога {travel_min} мин; "
        f"окончание {to_clock(visit.end)} при смене до {engineer.shift_end}"
        # Прироста нет, когда без этой заявки маршрут бригады не строится: считать не от чего.
        f"{'' if mine.extra_km is None else f'; прирост пробега {_signed(mine.extra_km)} км'}. "
        f"{_risk_clause(request, visit, risk)}"
        f"Другие инженеры: {_verdicts(request, data, candidates, engineer.id)}"
    )


def _risk_clause(request: Request, visit: Visit, risk: RequestForecast | None) -> str:
    """«Риск опоздания 35 %: запас до конца окна 8 мин» (PLAN 6.18).

    Запас считается от `latest_start` — единственного места, где живёт правило окна: при
    `fit_in_window` до конца окна должна поместиться ещё и сама работа.
    """
    if risk is None:
        return ""
    spare = to_minutes(latest_start(request) - visit.start)
    return (
        f"Риск опоздания {round(risk.late_probability * 100)} %: "
        f"запас до конца окна {spare} мин. "
    )


def _unassigned_text(
    request: Request, reason: str, data: Input, candidates: list[Candidate]
) -> str:
    """Та же причина, что в `unassigned`, плюс разбор по каждой бригаде."""
    return (
        f"Заявка №{request.id} "
        f"({WORK_PRIORITY_NAMES[request.work_priority].lower()}) не назначена: "
        f"{reason[0].lower()}{reason[1:]}. "
        f"Инженеры: {_verdicts(request, data, candidates, None)}"
    )


def _verdicts(
    request: Request, data: Input, candidates: list[Candidate], chosen: str | None
) -> str:
    """Строка по каждой бригаде набора, кроме выбранной: что подошло, а что нет."""
    engineers = {engineer.id: engineer for engineer in data.engineers}
    return "; ".join(
        f"{engineers[item.engineer_id].name} — "
        f"{_verdict(request, engineers[item.engineer_id], item)}"
        for item in candidates
        if item.engineer_id != chosen
    )


def _verdict(request: Request, engineer: Engineer, candidate: Candidate) -> str:
    if not candidate.skill_ok:
        return f"нет навыка «{SKILL_NAMES[request.skill]}»"
    if not candidate.transport_ok:
        return (
            f"транспорт «{TRANSPORT_NAMES[engineer.transport]}», "
            f"нужен «{TRANSPORT_NAMES[request.required_transport]}»"
        )
    if not candidate.equipment_ok:
        if lacking := _lacking(request, [engineer]):
            return f"нет {lacking}"
        return (
            f"не унесёт: за день берёт не больше {capacity(engineer)} ед. оборудования"
        )
    if not candidate.time_ok:
        return f"не помещается в день (смена до {engineer.shift_end})"
    return f"подходит, прирост {_fixed(candidate.extra_km)} км"


def _equipment_clause(request: Request) -> str:
    if not request.required_equipment:
        clause = "оборудование не требуется"
    else:
        names = _quoted(EQUIPMENT_NAMES[item] for item in request.required_equipment)
        clause = f"нужно оборудование {names} — есть"
    if request.required_tools:
        names = _quoted(TOOL_NAMES[item] for item in request.required_tools)
        need = (
            "нужен инструмент"
            if len(request.required_tools) == 1
            else "нужны инструменты"
        )
        clause += f"; {need} {names} — есть"
    return clause


def _lacking(request: Request, engineers: list[Engineer]) -> str:
    """Чего из нужного заявке нет хоть у одной из бригад: «оборудования «Роутер» и
    инструмента «Ноутбук»» — в родительном падеже, после «нет» и «не хватает».

    Пустая строка — всего хватает: тогда дело не в наличии, а во вместимости (PLAN 6.2).
    """
    equipment = [
        EQUIPMENT_NAMES[item]
        for item in request.required_equipment
        if any(item not in engineer.equipment for engineer in engineers)
    ]
    tools = [
        TOOL_NAMES[item]
        for item in request.required_tools
        if any(item not in engineer.tools for engineer in engineers)
    ]
    return " и ".join(
        f"{noun} {_quoted(names)}"
        for noun, names in (("оборудования", equipment), ("инструмента", tools))
        if names
    )


def _transport_clause(request: Request) -> str:
    if request.required_transport is None:
        return "транспорт не требуется"
    return f"нужен транспорт «{TRANSPORT_NAMES[request.required_transport]}» — есть"


# --- перестановки и единицы --------------------------------------------------


def _swap(
    engineer: Engineer,
    requests: list[Request],
    index: int,
    travel: Travel,
    position: Position,
    base: int,
) -> str:
    """Что будет, если поменять местами соседние заявки: нарушение или разница пробега."""
    swapped = (
        requests[:index]
        + [requests[index + 1], requests[index]]
        + requests[index + 2 :]
    )
    violation = first_violation(engineer, swapped, travel, position)
    if violation is not None:
        return SWAP_TEXT[violation]

    delta = meters(build_schedule(engineer, swapped, travel, position)) - base
    if to_km(abs(delta)) == 0:
        return "без изменений"
    return f"{_signed(to_km(delta))} км"


def _km(meters: int) -> str:
    """5049 -> «5,0»: километры с одним знаком и запятой, как их читает диспетчер (PLAN 6.9)."""
    return _fixed(to_km(meters))


def _fixed(km: float) -> str:
    return f"{km:.1f}".replace(".", ",")


def _signed(km: float) -> str:
    """«+1,9» или «−0,4»: у прироста важен знак, а таблица переездов не обязана быть
    треугольной — снятие или перестановка стопа может маршрут и укоротить."""
    return f"{'−' if km < 0 else '+'}{_fixed(abs(km))}"


def _quoted(names) -> str:
    return ", ".join(f"«{name}»" for name in names)


def _plural(count: int, forms: tuple[str, str, str]) -> str:
    """«1 заявка», «2 заявки», «5 заявок» — иначе текст маршрута читается как машинный."""
    tail, hundred = count % 10, count % 100
    if tail == 1 and hundred != 11:
        form = forms[0]
    elif 2 <= tail <= 4 and not 12 <= hundred <= 14:
        form = forms[1]
    else:
        form = forms[2]
    return f"{count} {form}"
