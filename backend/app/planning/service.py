"""Сборка плана: набор → расчёт → метрики → независимая проверка → сохранение (PLAN 4.1).

`build_plan` не знает про HTTP и сама в Mongo не ходит — её и проверяют тесты. Исключение одно:
кэш решений солвера, который включает флагом `cache` только `_finish` (PLAN 6.5). `create_plan`
добавляет таблицы переездов, линии маршрутов и сохранение. Планы неизменяемы: каждый расчёт
создаёт новый документ.

Перепланирование по событию идёт **через те же функции**: `replan.State` описывает, что уже
закреплено и откуда бригады продолжают, а сборка, объяснения, метрики и проверка остаются
в одном экземпляре (PLAN 6.12).
"""

import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from itertools import count, islice
from math import ceil
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import ortools

from .. import db
from ..config import settings
from ..data import seed
from ..dictionaries import (
    RESERVE_NAME,
    SHIFT_PRESETS,
    DurationMode,
    Equipment,
    Outcome,
    Skill,
    Tool,
    Transport,
    WorkPriority,
)
from ..models import (
    Algorithm,
    Closure,
    Dataset,
    DaySummary,
    Engineer,
    EngineerSummary,
    Event,
    Explanation,
    Input,
    Lunch,
    Metrics,
    NormMetrics,
    Plan,
    Preview,
    PreviewOption,
    ReplanMode,
    Request,
    RequestComplexity,
    Route,
    RouteExplanation,
    SearchStats,
    Stop,
    SummaryRequest,
    Unassigned,
    Variant,
)
from ..routing import geometry, matrices
from ..routing.matrices import Travel
from ..timeutil import to_clock, to_km, to_minutes, to_seconds
from . import (
    baseline,
    candidates,
    diff,
    duration,
    explain,
    facts,
    forecast,
    manual,
    metrics,
    optimizer,
    parallel,
    profiles,
    repair,
    replan,
    reviews,
    status,
    validator,
    variants,
)
from .bounds import lower_bound
from .manual import Manual
from .replan import State
from .schedule import (
    Position,
    Visit,
    best_insertion,
    build_schedule,
    capped,
    culprit,
    day_start,
    finishes,
    first_violation,
    lunch_after,
    lunch_slot,
    meters,
    start_positions,
)
from .variants import Setup


def _code_digest() -> str:
    """Отпечаток кода бэкенда — версия решений в кэше (PLAN 6.5, блок 45).

    Решение зависит от модели OR-Tools, досчёта, базового варианта, отбора кандидатов,
    проверки маршрута, единиц и таблиц переездов. Вести версию руками — значит однажды
    забыть её поднять, и после перезапуска диспетчер получил бы решение прежнего кода.
    Поэтому версия — хэш всех исходников `app/`: любая правка кода сбрасывает кэш. Лишний
    сброс после правки текстов ассистента стоит одного поиска, забытый — неверного плана.
    """
    root = Path(__file__).resolve().parents[1]
    digest = sha256()
    for path in sorted(root.rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


CODE_DIGEST = _code_digest()
log = logging.getLogger(__name__)
# Сколько живёт вариант предпросмотра, если его не применили (PLAN 6.12, блок 53).
DRAFT_TTL = timedelta(hours=1)


def build_plan(
    dataset: Dataset,
    travel: Travel,
    algorithm: Algorithm = Algorithm.BASELINE,
    state: State | None = None,
    setup: Setup | None = None,
    edit: Manual | None = None,
    cache: bool = False,
) -> Plan:
    """План по набору данных, без Mongo. Проверка кладётся в `plan.validation` (PLAN 6.7).

    `state` — состояние дня на момент события (PLAN 6.12): тогда планируется только остаток,
    а закреплённые стопы переходят в новую версию как есть.

    `setup` — настройка варианта сравнения (PLAN 6.14): порядок критериев и лимит поиска
    вместо тех, что стоят в `settings`. Без него считается обычный план дня.

    `edit` — готовые маршруты ручной правки (PLAN 6.16): алгоритма у неё нет, распределение
    задал диспетчер, и считать тут нечего — остальное собирается как у любого другого плана.

    `cache` — брать решение солвера из кэша `solutions` и класть его туда (PLAN 6.5). Это
    единственный путь `build_plan` в Mongo, и включает его только `_finish`; действует он лишь
    для первичного расчёта и вариантов — у события свой вход, и его решение не кэшируется.
    """
    # В plan.input — весь день: закреплённые заявки, срочная из события и всё остальное.
    # При перепланировании вход целиком берётся у родителя, а не у набора: новая версия — это
    # прошлый план плюс событие, и набор в базе к тому моменту мог смениться (reset, импорт).
    base = state.parent.input if state else dataset
    data = Input(
        office=base.office,
        requests=state.requests if state else dataset.requests,
        engineers=state.engineers if state else base.engineers,
        duration_mode=base.duration_mode,
    )
    committed = state.committed if state else {}
    # Где у бригад начался день: офис или их дом (PLAN 2.4), а у вышедшей в течение дня —
    # время её выхода. По этим позициям пересчитывается закреплённая часть маршрута.
    # Начатые работы кончаются по факту (блок 68): закрытые — когда закрыли, затянувшиеся —
    # не раньше, чем они ещё шли.
    facts = finishes(state.closed, state.running) if state else {}
    starts = {
        engineer.id: day_start(data.office, engineer, data.duration_mode, facts)
        for engineer in data.engineers
    }
    # Откуда планируется остаток: при перепланировании — точки продолжения из состояния
    # дня, иначе то же начало дня с учётом выбывших бригад.
    positions = state.positions if state else start_positions(data)
    # Планируется только незакреплённое: закреплённое уже роздано и пересмотру не подлежит.
    free = Input(
        office=data.office,
        requests=state.free() if state else data.requests,
        engineers=data.engineers,
        duration_mode=data.duration_mode,
    )

    # Закрепления ручной правки наследуются версией и сужают выбор бригады (PLAN 6.16, п. 6).
    # Аварию события диспетчер мог отдать выбранному инженеру (блок 74): в `insert` и `full`
    # выезд не расписывается, и выбор держится тем же закреплением.
    pinned = _held(state.parent.pinned | state.pins, data.engineers) if state else {}
    # Встраивание — только для самого события. Ручная правка поверх такой версии наследует
    # её режим (PLAN 6.16), но распределение там задал диспетчер, и вставлять заново нечего.
    inserting = edit is None and state is not None and state.mode is ReplanMode.INSERT
    search = None  # статистика поиска есть только там, где искал солвер
    delayed: set[str] = set()  # не взял никто после задержки (блок 68)
    if edit is not None:
        routes, unassigned, fallback = edit.routes, _left(free, edit.routes), False
    elif edit is None and state is not None and state.mode is ReplanMode.DELAY:
        routes, delayed = _delay(free, travel, state, setup, pinned)
        unassigned = _left(free, routes)
        fallback = state.parent.fallback_to_baseline
    elif inserting:
        routes = _insert(free, travel, state, setup, pinned)
        # Маршруты — родительские: если там оптимизация уступила базовому, то и здесь.
        unassigned = _left(free, routes)
        fallback = state.parent.fallback_to_baseline
    elif algorithm == Algorithm.BASELINE:
        (routes, unassigned), fallback = (
            baseline.assign(free, travel, positions, pinned),
            False,
        )
    elif algorithm == Algorithm.OPTIMIZED:
        routes, unassigned, fallback, search = (
            _remembered(free, travel, setup)
            if cache and state is None
            else _optimize(free, travel, state, setup, pinned)
        )
    else:
        # Молча посчитать базовый вариант вместо запрошенного нельзя: подмена обязана быть видна.
        raise ValueError(
            "ручное переназначение считается по готовым маршрутам (PLAN 6.16)"
        )

    # Расписание в два прохода: закреплённая часть считается от офиса и начала смены,
    # остаток — от точки продолжения. Бригада без закреплённых стопов могла простаивать до
    # момента события, и одним проходом от офиса это ожидание не выражается (PLAN 6.12).
    heads = {
        engineer.id: build_schedule(
            engineer, committed.get(engineer.id, []), travel, starts[engineer.id]
        )
        for engineer in data.engineers
    }
    tails = {
        engineer.id: build_schedule(
            engineer, routes[engineer.id], travel, positions[engineer.id]
        )
        for engineer in data.engineers
    }
    visits = {
        engineer_id: heads[engineer_id] + tails[engineer_id] for engineer_id in heads
    }

    # Прогноз опозданий считается по остатку дня: закреплённые стопы — факт, отклонять
    # в них нечего (PLAN 6.18). Он идёт перед объяснениями: строка о риске — часть текста
    # объяснения заявки, а не приписка к нему.
    risk = forecast.compute(data, travel, routes, positions)
    # «Было» для подсказки риска (блок 67): те же маршруты, но работа по нормативу.
    norm_risk = (
        forecast.compute(
            data.model_copy(update={"duration_mode": DurationMode.NORM}),
            travel,
            routes,
            {
                engineer_id: replace(position, duration_mode=DurationMode.NORM)
                for engineer_id, position in positions.items()
            },
        ).requests
        if data.duration_mode == DurationMode.COMPLEXITY
        else {}
    )

    # Объяснения строятся по готовому плану, поэтому оба алгоритма получают их одинаково.
    # Кандидаты считаются по остатку дня и от текущего состояния бригад — именно это и
    # нужно диспетчеру после события.
    reasons, explanations = explain.explain_requests(
        free, travel, routes, tails, unassigned, positions, risk.requests
    )
    # Задержку причина неназначения сама не видит: в пустой маршрут заявка, может, и встала
    # бы, но этот день её уже не возьмёт (блок 68). Причина держится цепочкой тиков часов, пока
    # заявку снова не назначат; другой пересчёт называет свою причину.
    if state and state.mode is ReplanMode.DELAY:
        delayed |= {
            item.request_id
            for item in state.parent.unassigned
            if item.reason_code == explain.DELAYED
        }
    for item in reasons:
        if item.request_id in delayed:
            item.reason_code = explain.DELAYED
            item.reason_text = explain.DELAYED_TEXT
            explanations[item.request_id] = Explanation(text=explain.DELAYED_TEXT)
    # Что поможет неназначенным (блок 55) — по тем же маршрутам и позициям, что кандидаты.
    # Новая бригада при перепланировании выходит в момент события, как `add_engineer`.
    joined = state.event.time if state and state.event else None
    for request_id, items in explain.remedies(
        free,
        travel,
        routes,
        unassigned,
        positions,
        # Список запасных читается с диска — только когда есть кому помогать.
        _pool(dataset, data) if unassigned else [],
        joined,
    ).items():
        explanations[request_id].remedies = items
    explanations |= _committed_explanations(data, state)
    # Приписки к заявке события — и в объяснение, и в причину: таблица неназначенных
    # показывает причину, а не объяснение (PLAN 6.8).
    # Тексты причин и объяснений точкой не кончаются, поэтому приписка идёт через неё.
    for request_id, note in _event_notes(data, state, inserting, unassigned).items():
        explanation = explanations[request_id]
        explanation.text = f"{explanation.text.rstrip('. ')}. {note}"
        for item in reasons:
            if item.request_id == request_id:
                item.reason_text = f"{item.reason_text.rstrip('. ')}. {note}"
    # Перенесённые заявки не планируются, но из дня не исчезают: диспетчер видит их среди
    # неназначенных с пометкой переноса (PLAN 6.19). Там же оказывается и та, которую
    # бригада не смогла выполнить, — у неё свой текст, но причина та же: сегодня не делаем.
    deferred = state.deferred if state else {}
    closed = state.closed if state else {}
    postponed = [request for request in data.requests if request.id in deferred]
    texts = {
        request.id: _postponed_text(request, data, deferred, closed)
        for request in postponed
    }
    reasons += [
        Unassigned(
            request_id=request.id,
            reason_code=explain.DEFERRED,
            reason_text=texts[request.id],
        )
        for request in postponed
    ]
    explanations |= {
        request.id: Explanation(text=texts[request.id]) for request in postponed
    }
    # Факт старше всех прочих объяснений: у закрытой заявки не таблица кандидатов, а подпись.
    explanations |= _closed_explanations(data, closed)

    assignments = _assignments(data, _full(data, committed, routes))
    # Метрики раньше объяснений: загрузку смены объяснение бригады берёт у них (PLAN 6.9).
    figures = metrics.compute(
        data,
        visits,
        unassigned + postponed,
        len(deferred),
        sum(item.late_probability for item in risk.requests.values()),
        closed,
    )
    # Оценка снизу — по назначенным заявкам, выполнимые — по причинам неназначения: у
    # `NOT_FITTED` есть бригада, в чей пустой маршрут заявка помещается (PLAN 6.8, 6.10).
    figures.engineers_lower_bound = lower_bound(
        data, travel, {visit.request_id for stops in visits.values() for visit in stops}
    )
    figures.requests_ceiling = figures.assigned_count + sum(
        item.reason_code == explain.NOT_FITTED for item in reasons
    )
    plan = Plan(
        id=uuid4().hex,
        parent_plan_id=state.parent.id if state else None,
        dataset_id=dataset.id,
        algorithm=algorithm,
        variant=setup.variant if setup else None,
        event=state.event if state else None,
        replan_mode=state.mode if state else None,
        input=data,
        # Маршруты всех бригад, включая простаивающие: пустой маршрут — тоже ответ диспетчеру.
        # Линии маршрутов проставляет `create_plan`: за ними надо идти в Valhalla, а
        # `build_plan` про HTTP не знает.
        routes=[
            _route(
                engineer.id,
                visits[engineer.id],
                len(heads[engineer.id]),
                _eaten(engineer, heads[engineer.id], tails[engineer.id], positions),
            )
            for engineer in data.engineers
        ],
        assignments=assignments,
        pinned=_pins(edit.pinned if edit else pinned, assignments),
        unassigned=reasons,
        deferred=deferred,
        closed=closed,
        running=state.running if state else {},
        # Статус вычисляется по расписанию и моменту события, а не хранится (PLAN 6.19).
        statuses=status.compute(
            visits,
            closed,
            deferred,
            to_seconds(state.event.time) if state and state.event else None,
        ),
        explanations=explanations,
        route_explanations=_route_explanations(
            data, travel, routes, committed, positions, state, visits, figures
        ),
        metrics=figures,
        complexity={
            request.id: RequestComplexity(
                complexity=duration.complexity(request) / duration.UNIT,
                level=duration.level(request),
            )
            for request in data.requests
        },
        forecast=risk,
        norm_forecast=norm_risk,
        approximate=travel.approximate,
        approximate_note=explain.approximate_note(data, travel),
        fallback_to_baseline=fallback,
        search=search,
        created_at=datetime.now(UTC),
    )
    if state:
        plan.diff = diff.compare(state.parent, plan)
    # Условия досчёта, которых в плане нет (блок 52): закрепления, действовавшие в расчёте, и
    # ужатые сроки аварий — то же правило, что в `_optimize` и `_insert`; встраивание ужимает
    # сроки и у варианта без поиска.
    staged = _staged(
        setup.mode if setup else settings.objective_mode,
        setup,
        inserting or (setup.guided if setup else True),
    )
    plan.validation = validator.validate(plan, travel, pinned, staged)
    return plan


def _pool(dataset: Dataset, data: Input) -> list[Engineer]:
    """Запасные люди встроенного набора для «Что поможет» (блок 55).

    Это `crews.json` без тех, кто уже в наборе: у демо-пары следующие по списку люди — те же,
    что в составе побольше. Старт — офис набора, как у добавленной диспетчером бригады. У
    загруженного набора своих запасных нет — там остаётся типовая бригада.
    """
    if not dataset.built_in:
        return []
    taken = {engineer.id for engineer in data.engineers}
    start = {"startLat": data.office.lat, "startLon": data.office.lon}
    # Дома у запасного нет: адрес дома раздаётся по участку (`seed.give_homes`), а запасной
    # стартует из офиса — домашнее оборудование ему не нужно.
    return [
        Engineer(**({k: v for k, v in raw.items() if k != "homeEquipment"} | start))
        for raw in seed.load_crew_pool()
        if raw["id"] not in taken
    ]


def staffed(dataset: Dataset, mode: str) -> Dataset:
    """Бригады набора плюс резерв — только в `rating_first` (PLAN 9.1, блок 71).

    Резерв встроенного набора — люди `crews.json` вне набора, как у «Что поможет» (`_pool`).
    У загруженного своих людей вне набора нет: до `seed.CREW_POOL` его добирают типовые
    универсалы — все навыки, оборудование и инструменты, автомобиль, смена 2/2, из офиса.
    Резерв идёт после бригад набора: базовый вариант перебирает их по порядку. Входит в
    `Input`, а с ним — в отпечаток кэша и в `plan.input`. Рейтинг ставит `reviews.snapshot`.
    """
    if mode != "rating_first":
        return dataset
    pool = _pool(dataset, dataset)
    if not dataset.built_in:
        taken = {engineer.id for engineer in dataset.engineers}
        free = (number for number in count(1) if f"reserve-{number}" not in taken)
        shift = SHIFT_PRESETS["2-2"]
        pool = [
            Engineer(
                id=f"reserve-{number}",
                name=RESERVE_NAME.format(number),
                start_lat=dataset.office.lat,
                start_lon=dataset.office.lon,
                shift_start=shift["start"],
                shift_end=shift["end"],
                skills=list(Skill),
                transport=Transport.CAR,
                equipment=list(Equipment),
                tools=list(Tool),
            )
            for number in islice(free, max(0, seed.CREW_POOL - len(dataset.engineers)))
        ]
    reserve = [engineer.model_copy(update={"reserve": True}) for engineer in pool]
    return dataset.model_copy(update={"engineers": dataset.engineers + reserve})


def _optimize(
    data: Input,
    travel: Travel,
    state: State | None = None,
    setup: Setup | None = None,
    pinned: dict[str, str] | None = None,
) -> tuple[dict[str, list[Request]], list[Request], bool, SearchStats | None]:
    """Оптимизация, досчёт и выбор лучшего из двух планов (PLAN 6.5, 6.6).

    Оптимизированный план никогда не хуже базового: если он проиграл по цели или солвер
    не нашёл решения, возвращается базовый и план помечается `fallbackToBaseline` —
    подмена не должна быть незаметной.

    В сравнении вариантов (`setup`, PLAN 6.14) подмена базовым не применяется: вариант
    показывают ровно таким, каким его сделала его же настройка, иначе сравнивать было бы
    нечего. Базовый вариант там считается отдельной строкой таблицы, поэтому здесь он
    не считается вовсе. Случай «солвер не нашёл решения» — исключение: плана в нём просто
    нет, и вернуть вместо него нечего, кроме базового, зато это видно по флагу.

    Последним отдаётся статистика поиска — и тогда, когда победил базовый: она описывает
    сам поиск, а что из него взяли, говорит `fallbackToBaseline`.
    """
    positions = state.positions if state else start_positions(data)
    pinned = pinned or {}
    mode = setup.mode if setup else settings.objective_mode
    # Пределы поиска: у перепланирования короче первичного расчёта — прошлый план подаётся
    # стартовым решением, и искать с нуля не приходится. Это и у события по варианту: от
    # варианта наследуются порядок критериев и `guided`, а не его пределы (PLAN 6.14).
    # Поиск останавливает число решений, время — страховка (PLAN 6.5).
    if state:
        limit = settings.replan_time_limit_sec
        solutions = settings.replan_solution_limit
    elif setup:
        limit, solutions = setup.time_limit, setup.solution_limit
    else:
        limit = settings.solver_time_limit_sec
        solutions = settings.solver_solution_limit
    bounds = optimizer.bounds(data, travel)
    weights = optimizer.make_weights(optimizer.ORDERS[mode], bounds)
    allowed = candidates.allowed(data, travel, positions, pinned)
    initial = _initial(state) if state else None
    guided = setup.guided if setup else True
    # Вход, на котором ищут солвер и досчёт: при авариях вперёд у назначенных этапом 1
    # аварий окно ужато до их срока, иначе досчёт вставил бы работу перед аварией.
    staged = _staged(mode, setup, guided)
    stages = (
        _stages(
            data,
            travel,
            allowed,
            weights,
            bounds,
            limit,
            solutions,
            positions,
            initial,
            pinned,
        )
        if staged
        else None
    )
    if stages is not None:
        solved, tight = stages
    else:
        tight = data
        solved = optimizer.solve(
            data,
            travel,
            allowed,
            weights,
            limit,
            positions,
            initial,
            guided=guided,
            solution_limit=solutions,
        )
    if solved is None:
        return *baseline.assign(data, travel, positions, pinned), True, None

    routes = solved.routes
    unassigned = repair.fill(
        tight, routes, solved.unassigned, travel, weights, positions, pinned
    )
    # В план идут заявки с исходными окнами: ужатое окно — средство расчёта, а не факт.
    known = {request.id: request for request in data.requests}
    routes = {
        engineer_id: [known[request.id] for request in route]
        for engineer_id, route in routes.items()
    }
    unassigned = [known[request.id] for request in unassigned]
    if setup:
        return routes, unassigned, False, solved.stats

    base = baseline.assign(data, travel, positions, pinned)
    if _cost(data, travel, routes, unassigned, weights, positions, staged) <= _cost(
        data, travel, *base, weights, positions, staged
    ):
        return routes, unassigned, False, solved.stats
    return *base, True, solved.stats


def _staged(mode: str, setup: Setup | None, guided: bool) -> bool:
    """Считать ли аварии вперёд (PLAN 6.5, блок 49).

    Только в `emergency_first` и только с поиском: в остальных порядках авария не старший
    критерий, а «быстрый расчёт» показывает цену отказа от поиска, а не от этапов. У варианта
    своя настройка, если он её задал; иначе — `OBJECTIVE_STAGED`.
    """
    staged = (
        settings.objective_staged
        if setup is None or setup.staged is None
        else setup.staged
    )
    return staged and guided and mode == "emergency_first"


def _stages(
    data: Input,
    travel: Travel,
    allowed: dict[str, list[str]],
    weights: dict[str, int],
    bounds: dict[str, int],
    limit: int,
    solutions: int,
    positions: dict[str, Position],
    initial: dict[str, list[str]] | None,
    pinned: dict[str, str],
) -> tuple[optimizer.Solved, Input] | None:
    """Аварии вперёд: сначала одни аварии как можно раньше, потом весь день (PLAN 6.5).

    Мягкую границу на начало аварии в общую модель не поставить: штраф за секунду ожидания
    умножил бы все старшие веса, и цель вышла бы за int64. Поэтому этапов два.

    Этап 1 — модель из одних аварий, порядок `STAGE1_ORDER`: число аварий, затем их ожидание
    (`SetCumulVarSoftUpperBound` от того же момента, что и метрика, `metrics.ready`), затем
    пробег. Этап 2 — весь день с весами режима; назначенные этапом 1 аварии обязательны,
    бригаду им выбирает этап 2, а начать их можно не позже достигнутого плюс
    `EMERGENCY_SLACK_MIN` — это ужатое окно (`capped`). Возвращает решение и вход с ужатыми
    окнами: на нём же идёт досчёт. `None` — этапы не сложились (аварий нет, часы кончились
    раньше первого решения), и день считается как обычно.

    Стартовое решение этапа 2 — маршруты этапа 1, а при перепланировании — маршруты родителя
    без аварий, в которые досчёт вставил ужатые аварии: так прошлый план остаётся тёплым
    стартом (блок 44). Не встали — старт берётся у этапа 1. Лимит времени — один на оба
    этапа: этапу 2 достаётся то, что не потратил этап 1.
    """
    emergencies = {
        request.id
        for request in data.requests
        if request.work_priority is WorkPriority.EMERGENCY and allowed[request.id]
    }
    if not emergencies:
        return None
    opened = metrics.day_opens(data)
    first = optimizer.solve(
        data,
        travel,
        {key: value if key in emergencies else [] for key, value in allowed.items()},
        optimizer.make_weights(optimizer.STAGE1_ORDER, bounds),
        limit,
        positions,
        initial,
        solution_limit=solutions,
        # Спуск, а не GLS: в модели из одной аварии GLS до предела не доходит никогда.
        descent=True,
        soft={
            request.id: metrics.ready(request, opened)
            for request in data.requests
            if request.id in emergencies
        },
    )
    if first is None:
        return None

    slack = settings.emergency_slack_min * 60
    deadlines = {
        # Срок — целая минута: так его хранит окно заявки.
        visit.request_id: -(-visit.start // 60) * 60 + slack
        for engineer in data.engineers
        for visit in build_schedule(
            engineer, first.routes[engineer.id], travel, positions[engineer.id]
        )
    }
    tight = _capped(data, deadlines)
    # Кроме назначенных аварий, планировать нечего — этап 2 повторил бы этап 1, а GLS в
    # модели из одних обязательных заявок мог бы не найти ни одного хода (см. `_search`).
    if all(request.id in deadlines for request in data.requests if allowed[request.id]):
        return first, tight
    start = None
    if initial is not None:
        start = _warm(
            tight, travel, allowed, weights, positions, initial, pinned, deadlines
        )
    if start is None:
        start = {
            engineer_id: [request.id for request in route]
            for engineer_id, route in first.routes.items()
        }
    second = optimizer.solve(
        tight,
        travel,
        allowed,
        weights,
        max(1, limit - ceil(first.stats.sec)),
        positions,
        start,
        solution_limit=solutions,
        required=frozenset(deadlines),
    )
    if second is None:
        return None
    return second._replace(stats=_joined(first.stats, second.stats)), tight


def _capped(data: Input, deadlines: dict[str, int]) -> Input:
    """Вход, где у заявок со сроком ужато окно (`schedule.capped`), остальные как есть."""
    return Input(
        office=data.office,
        requests=[
            capped(request, deadlines[request.id], data.engineers, data.duration_mode)
            if request.id in deadlines
            else request
            for request in data.requests
        ],
        engineers=data.engineers,
        duration_mode=data.duration_mode,
    )


def _warm(
    tight: Input,
    travel: Travel,
    allowed: dict[str, list[str]],
    weights: dict[str, int],
    positions: dict[str, Position],
    initial: dict[str, list[str]],
    pinned: dict[str, str],
    deadlines: dict[str, int],
) -> dict[str, list[str]] | None:
    """Стартовое решение этапа 2 при перепланировании: прошлый план с авариями в срок.

    Маршруты родителя без аварий этапа 1 подрезаются до допустимого той же арифметикой, что и
    стартовое решение солвера (`optimizer.trimmed`), затем досчёт ставит в них аварии с
    ужатыми окнами. Не встала хоть одна — `None`, и этап 2 стартует от маршрутов этапа 1:
    старт без обязательной заявки солвер всё равно не прочитал бы.
    """
    known = {request.id: request for request in tight.requests}
    emergencies = [request for request in tight.requests if request.id in deadlines]
    others = [
        request
        for request in tight.requests
        if allowed[request.id] and request.id not in deadlines
    ]
    kept = optimizer.trimmed(
        initial, tight.engineers, others, allowed, travel, positions
    )
    routes = {
        engineer_id: [known[key] for key in route]
        for engineer_id, route in kept.items()
    }
    if repair.fill(tight, routes, emergencies, travel, weights, positions, pinned):
        return None
    return {
        engineer_id: [request.id for request in route]
        for engineer_id, route in routes.items()
    }


def _joined(
    first: SearchStats | None, second: SearchStats | None
) -> SearchStats | None:
    """Статистика двух этапов как одного поиска (блок 49).

    Решения и секунды складываются. Остановлен временем — если временем остановлен любой
    этап: неповторяемый этап 1 делает неповторяемым и весь план, и кэш его не возьмёт.
    Цель и кривая — этапа 2: это цель самого плана.
    """
    if first is None or second is None:
        return second
    return second.model_copy(
        update={
            "solutions": first.solutions + second.solutions,
            "stopped_by": "time"
            if "time" in (first.stopped_by, second.stopped_by)
            else "solutions",
            "sec": round(first.sec + second.sec, 3),
        }
    )


def _remembered(
    data: Input, travel: Travel, setup: Setup | None
) -> tuple[dict[str, list[Request]], list[Request], bool, SearchStats | None]:
    """`_optimize` первичного расчёта или варианта через кэш решений (PLAN 6.5, блок 45).

    С пределом числа решений тот же вход даёт то же решение, поэтому повторный расчёт берёт
    его из кэша, а не ждёт поиска. Пишется только поиск, остановленный пределом решений.
    Без статистики решение не пишется: «солвер не нашёл решения» бывает лишь тогда, когда
    часы кончились раньше первого решения (у каждой заявки есть дизъюнкция, пустое решение
    допустимо всегда), а набор без единого кандидата и так считается без поиска.
    """
    key = _fingerprint(data, travel, setup)
    remembered = _recall(key, data)
    if remembered is not None:
        return remembered
    solved = _optimize(data, travel, None, setup)
    stats = solved[3]
    if stats is not None and stats.stopped_by == "solutions":
        _remember(key, *solved)
    return solved


def _fingerprint(data: Input, travel: Travel, setup: Setup | None) -> str:
    """Отпечаток входа `_optimize`: всё, от чего зависит решение (PLAN 6.5, блок 45).

    Таблицы переездов входят **содержимым**, а не отпечатком точек: у таблицы, посчитанной
    по прямой, точки те же, что у дорожной, и её решение отдалось бы плану на дорогах, а
    коэффициенты пробок и общественного транспорта вшиты в сами числа. Лимит времени не
    входит: кэшируется только поиск, остановленный пределом решений, а его траектория от
    часов не зависит. Вариант от первичного расчёта отличается и при том же порядке
    критериев: подмены базовым у него нет (PLAN 6.14).
    """
    payload = {
        "code": CODE_DIGEST,
        "ortools": ortools.__version__,
        # Порядок заявок и бригад значим: его читает базовый вариант. Рейтинг влияет на
        # расчёт только в `rating_first` (блок 71): в остальных режимах новый отзыв не должен
        # сбрасывать кэш решений.
        "input": data.model_dump(
            mode="json",
            exclude=None
            if (setup.mode if setup else settings.objective_mode) == "rating_first"
            else {"engineers": {"__all__": {"rating"}}},
        ),
        "points": travel.points,
        "travel": {
            profile: [matrix.durations, matrix.distances]
            for profile, matrix in travel.matrices.items()
        },
        "setup": (
            [setup.variant, setup.mode, setup.guided, setup.solution_limit]
            if setup
            else [None, settings.objective_mode, True, settings.solver_solution_limit]
        ),
        # Аварии вперёд (блок 49): поэтапно ли и с каким запасом к сроку аварии.
        "staged": _staged(
            setup.mode if setup else settings.objective_mode,
            setup,
            setup.guided if setup else True,
        )
        and settings.emergency_slack_min,
        "windowRule": settings.window_rule,
        "equipmentCapacity": settings.equipment_capacity,
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return sha256(text.encode()).hexdigest()


def _recall(
    key: str, data: Input
) -> tuple[dict[str, list[Request]], list[Request], bool, SearchStats | None] | None:
    """Решение из кэша; `None` — такой вход ещё не считали.

    Заявки восстанавливаются по входу: он целиком в ключе, поэтому каждая на месте.
    """
    document = db.solutions.find_one({"_id": key})
    if document is None:
        return None
    known = {request.id: request for request in data.requests}
    routes = {
        engineer_id: [known[request_id] for request_id in request_ids]
        for engineer_id, request_ids in document["routes"]
    }
    unassigned = [known[request_id] for request_id in document["unassigned"]]
    stats = document["stats"] and SearchStats.model_validate(document["stats"])
    return routes, unassigned, document["fallback"], stats


def _remember(
    key: str,
    routes: dict[str, list[Request]],
    unassigned: list[Request],
    fallback: bool,
    stats: SearchStats | None,
) -> None:
    """Кладёт решение в кэш. Маршруты — парами, а не словарём: идентификатор бригады приходит
    из чужого файла, и точка или `$` в нём не годятся в имя поля Mongo. `replace_one` с
    `upsert`, а не `insert_one`: два одинаковых расчёта одновременно не упадут на дубле ключа.
    """
    db.solutions.replace_one(
        {"_id": key},
        {
            "routes": [
                [engineer_id, [request.id for request in requests]]
                for engineer_id, requests in routes.items()
            ],
            "unassigned": [request.id for request in unassigned],
            "fallback": fallback,
            "stats": stats.model_dump(by_alias=True) if stats else None,
            "createdAt": datetime.now(UTC),
        },
        upsert=True,
    )


def _insert(
    data: Input,
    travel: Travel,
    state: State,
    setup: Setup | None,
    pinned: dict[str, str],
) -> dict[str, list[Request]]:
    """Режим `insert`: остаток дня родителя как есть плюс новая заявка в свободное время.

    Обычная заявка, пришедшая днём, не должна перестраивать сформированный план (ответы
    организаторов, PLAN 6.12). Поэтому никто, кроме неё, не меняет ни бригаду, ни место в
    маршруте, а прежние неназначенные так и остаются без исполнителя. Хвост маршрута от
    точки продолжения пересчитывается в те же времена, что у родителя: выезд к первому
    незакреплённому стопу и так позже события. Сдвигаются только стопы после вставки.

    Куда вставить, решает досчёт (`repair.fill`) теми же весами цели, что у солвера: у
    уже задействованной бригады место дешевле, чем у простаивающей.
    """
    known = {request.id: request for request in data.requests}
    stops = {route.engineer_id: route.stops for route in state.parent.routes}
    routes = {
        engineer.id: [
            known[stop.request_id]
            for stop in stops[engineer.id]
            if stop.request_id in known
        ]
        for engineer in data.engineers
    }
    mode = setup.mode if setup else settings.objective_mode
    weights = optimizer.make_weights(
        optimizer.ORDERS[mode], optimizer.bounds(data, travel)
    )
    # День считали аварии вперёд (блок 49): заявка встраивается, не отодвигая аварию дальше
    # её прежнего начала плюс запас — иначе обещание варианта молча ломалось бы на каждой
    # обычной заявке, пришедшей днём. Прежнее начало напечатано с точностью до минуты, запас
    # это покрывает.
    tight = data
    if _staged(mode, setup, True):
        slack = settings.emergency_slack_min * 60
        tight = _capped(
            data,
            {
                stop.request_id: to_seconds(stop.start) + slack
                for route in state.parent.routes
                for stop in route.stops
                if stop.request_id in known
                and known[stop.request_id].work_priority is WorkPriority.EMERGENCY
            },
        )
    narrow = {request.id: request for request in tight.requests}
    routes = {
        engineer_id: [narrow[request.id] for request in route]
        for engineer_id, route in routes.items()
    }
    repair.fill(
        tight,
        routes,
        [narrow[state.event.request.id]],
        travel,
        weights,
        state.positions,
        pinned,
    )
    return {
        engineer_id: [known[request.id] for request in route]
        for engineer_id, route in routes.items()
    }


def _delay(
    data: Input,
    travel: Travel,
    state: State,
    setup: Setup | None,
    pinned: dict[str, str],
) -> tuple[dict[str, list[Request]], set[str]]:
    """Режим `delay` (блок 68): остаток дня родителя как есть, починка только при конфликте.

    Факты часов дня (досрочное закрытие, затянувшаяся работа) уже сдвинули точки продолжения.
    У бригады, чей остаток стал недопустимым, снимаются виновники (`schedule.culprit`), и
    каждый ставится обратно: сначала к ней же, на лучшее место, затем досчётом к другим теми же
    весами цели, иначе он не назначен — «не удалось после задержки». Остальные бригады и
    прежние неназначенные не трогаются: день не переоптимизируется на каждом тике часов.
    """
    known = {request.id: request for request in data.requests}
    stops = {route.engineer_id: route.stops for route in state.parent.routes}
    routes = {
        engineer.id: [
            known[stop.request_id]
            for stop in stops.get(engineer.id, [])
            if stop.request_id in known
        ]
        for engineer in data.engineers
    }
    dropped: list[tuple[Engineer, Request]] = []
    for engineer in data.engineers:
        route, at = routes[engineer.id], state.positions[engineer.id]
        while first_violation(engineer, route, travel, at) is not None:
            guilty = culprit(engineer, route, travel, at)[0]
            route.remove(guilty)
            dropped.append((engineer, guilty))
    if not dropped:
        return routes, set()
    mode = setup.mode if setup else settings.objective_mode
    weights = optimizer.make_weights(
        optimizer.ORDERS[mode], optimizer.bounds(data, travel)
    )
    delayed = set()
    for engineer, request in dropped:
        spot = best_insertion(
            engineer, routes[engineer.id], request, travel, state.positions[engineer.id]
        )
        if spot is not None:
            routes[engineer.id].insert(spot[0], request)
        elif repair.fill(
            data, routes, [request], travel, weights, state.positions, pinned
        ):
            delayed.add(request.id)
    return routes, delayed


def _event_notes(
    data: Input, state: State | None, inserting: bool, unassigned: list[Request]
) -> dict[str, str]:
    """Что дописать к заявке, пришедшей событием: срок реакции и встраивание (PLAN 6.12).

    Сужено ли окно, видно без догадок: событие хранит заявку такой, какой её прислали, а
    во входе дня — с окном после срока реакции. Сравнение с настройкой и типом работы
    промахивалось бы: у аварии, пришедшей со своим коротким окном, сужать было нечего.
    """
    if state is None or state.event is None or state.event.type not in replan.ADDING:
        return {}
    sent = state.event.request
    (stored,) = [request for request in data.requests if request.id == sent.id]
    notes = []
    if stored.window_end != sent.window_end:
        notes.append(explain.reaction_text(settings.emergency_response_min))
    if inserting:
        placed = all(request.id != sent.id for request in unassigned)
        notes.append(explain.inserted_text(state.event.time, placed))
    return {sent.id: " ".join(notes)} if notes else {}


def _initial(state: State) -> dict[str, list[str]]:
    """Маршруты прошлого плана как стартовое решение — без закреплённых стопов (PLAN 6.5).

    Здесь только «что было». До допустимого старт подрезает `optimizer.solve` — по тем же
    заявкам и доменам бригад, из которых он строит модель: отменённые заявки, стопы
    выбывшей бригады и опоздавшие после события снимаются там (блок 44).
    """
    fixed = {
        request.id for requests in state.committed.values() for request in requests
    }
    return {
        route.engineer_id: [
            stop.request_id for stop in route.stops if stop.request_id not in fixed
        ]
        for route in state.parent.routes
    }


def _cost(
    data: Input,
    travel: Travel,
    routes: dict[str, list[Request]],
    unassigned: list[Request],
    weights: dict[str, int],
    positions: dict[str, Position],
    staged: bool = False,
) -> tuple[int, int, int]:
    """Значение цели для готового плана — тем же кодом, что строит цель солвера.

    Тройкой «всё, кроме пробега; ожидание аварий; пробег». Пробег — младший критерий с
    весом 1 (PLAN 6.5), поэтому без ожидания тройки сравниваются ровно как сама цель. При
    авариях вперёд (блок 49) ожидание стоит над пробегом: иначе базовый план, равный по
    старшим критериям и короче на пару километров, вернул бы аварии в конец дня.
    """
    days = {
        engineer.id: build_schedule(
            engineer, routes[engineer.id], travel, positions[engineer.id]
        )
        for engineer in data.engineers
    }
    driven = sum(meters(visits) for visits in days.values())
    waited = (
        metrics.emergency_wait(data, days, metrics.day_opens(data)) if staged else 0
    )
    total = optimizer.cost(routes, unassigned, driven, weights, data.engineers)
    return total - driven * weights["distance"], waited, driven


def create_plan(dataset: Dataset, algorithm: Algorithm = Algorithm.BASELINE) -> Plan:
    """Первичный расчёт дня по набору данных."""
    dataset = reviews.snapshot(staffed(dataset, settings.objective_mode))
    return _finish(dataset, matrices.get(dataset), algorithm, None)


def compare(dataset: Dataset, wanted: list[Variant]) -> list[Plan]:
    """Один день несколькими настройками расчёта (PLAN 6.14).

    Таблицы переездов читаются один раз на все варианты: они описывают точки набора и от
    настройки не зависят, а поход за ними — это Mongo, а при пустом кэше и Valhalla.
    Варианты считаются по очереди: каждый занимает солвером целое ядро, и параллельный
    запуск только поделил бы между ними одно и то же процессорное время.

    Планы сохраняются как обычные, со своими `id` и общим `compareGroupId`: любой из них
    открывается по `GET /api/plans/{id}` и годится в родители события.

    «Рейтинг в приоритете» считается по набору с резервом (`staffed`): таблицы у него свои —
    резерв загруженного набора ездит на автомобиле, которого в наборе могло не быть.
    """
    group = uuid4().hex
    # С учётом сложности (PLAN 9.1) — с чем сравнить: основной вариант того же дня по
    # нормативу. Отдельным планом он не сохраняется, у вариантов группы — только его итоги,
    # и считается до них: план после сохранения не правится.
    norm = None
    (plain_day, travel, main), *days = _compared(dataset, wanted)
    if dataset.duration_mode == DurationMode.COMPLEXITY:
        plain = build_plan(
            plain_day, travel, Algorithm.OPTIMIZED, setup=main, cache=True
        )
        norm = NormMetrics(
            assigned_count=plain.metrics.assigned_count,
            engineers_used=plain.metrics.engineers_used,
            total_km=plain.metrics.total_km,
        )
    return [
        _finish(day, travel, setup.algorithm, None, setup, group, norm=norm)
        for day, travel, setup in days
    ]


def _compared(
    dataset: Dataset, wanted: list[Variant]
) -> list[tuple[Dataset, Travel, Setup]]:
    """Что считает сравнение: первым — тот же день по нормативу, затем варианты по порядку.

    Одно место и для `compare`, и для прогрева кэша (`warm_up`): разойдись они в наборе
    или таблицах — у прогрева был бы другой отпечаток, и кэш остался бы холодным.
    """
    days = {
        setup.mode: reviews.snapshot(staffed(dataset, setup.mode))
        for setup in (variants.SETUPS[variant] for variant in wanted)
        if setup.mode == "rating_first"
    }
    tables = {mode: matrices.get(day) for mode, day in days.items()}
    dataset = reviews.snapshot(dataset)
    travel = matrices.get(dataset)
    plain = dataset.model_copy(update={"duration_mode": DurationMode.NORM})
    return [(plain, travel, variants.SETUPS[Variant.EMERGENCY_FIRST])] + [
        (days.get(setup.mode, dataset), tables.get(setup.mode, travel), setup)
        for setup in (variants.SETUPS[variant] for variant in wanted)
    ]


def warm_up() -> None:
    """Решения всех демо-пар — в кэш `solutions` заранее (запускает `main.lifespan`).

    Без него первое «Использовать демо-набор» каждой пары ждёт поиска минуту. Считается
    ровно то, что посчитает сравнение, и тем же `_remembered`: посчитанное берётся из кэша
    за миг, поэтому повторный старт ничего не стоит, а правка кода (`CODE_DIGEST`) сама
    отправляет пары на пересчёт. Объяснения и линии не строятся — в кэше их нет.
    """
    for requests_id in seed.DEMO_REQUESTS:
        for crew_id in seed.DEMO_CREWS:
            started = perf_counter()
            dataset = seed.demo_dataset(requests_id, crew_id)
            for day, travel, setup in _compared(dataset, list(Variant)):
                if setup.algorithm == Algorithm.OPTIMIZED:
                    data = Input(
                        office=day.office,
                        requests=day.requests,
                        engineers=day.engineers,
                        duration_mode=day.duration_mode,
                    )
                    _remembered(data, travel, setup)
            log.info(
                "Прогрев кэша решений: %s · %s — %.0f с",
                requests_id,
                crew_id,
                perf_counter() - started,
            )


def replan_plan(
    parent: Plan, dataset: Dataset, event: Event, mode: ReplanMode, draft: bool = False
) -> Plan:
    """Новая версия плана после события (PLAN 6.12). `replan.ReplanError` — событие не применимо.

    Алгоритм наследуется от родителя: посчитать базовый план оптимизатором значило бы
    подменить точку отсчёта, с которой этот же план сравнивают (PLAN 6.6). `draft` — «что
    будет, если» ассистента (блок 61): черновик, версией его делает `commit`.
    """
    travel = matrices.get(dataset)
    state = replan_state(parent, event, mode, travel)
    return _finish(
        dataset, travel, _algorithm_of(parent), state, _setup_of(parent), draft=draft
    )


def replan_state(parent: Plan, event: Event, mode: ReplanMode, travel: Travel) -> State:
    """Состояние дня после события, без Mongo: досчитывает точки и профили в `travel`.

    Отдельно от `replan_plan`, чтобы событие можно было посчитать и без базы — так его
    считают предпросмотр и часы дня, — не копируя подготовку. **`travel` правится на месте**
    и на каждое событие должен быть свежим, как у `replan_plan` из кэша: в общей таблице
    копились бы точки заявок, уже ушедших из дня, и с ними пометка `approximate` и самое
    длинное плечо, от которого считаются веса цели.
    """
    # Заявка и бригада из события в набор не попадают (PLAN 5.5), а таблицы переездов
    # читаются по набору, поэтому их точки досчитываются здесь и на каждой версии заново:
    # два запроса на профиль против пересчёта всей матрицы и затёртого кэша встроенного
    # набора. Вход родителя — до фиксации: по нему считается расписание закреплённой части,
    # и в цепочке событий среди его заявок уже есть заявка прошлой версии.
    _add_points(travel, parent.input)
    # Сущность события — до `prepare`: она проверяет правку бригады по её закреплённой
    # части, и без точки нового дома или профиля нового транспорта проверка упала бы
    # `KeyError` в 500 вместо ответа диспетчеру (PLAN 6.19).
    carried = Input(
        office=parent.input.office,
        requests=[event.request] if getattr(event, "request", None) else [],
        engineers=[event.engineer] if getattr(event, "engineer", None) else [],
        duration_mode=parent.input.duration_mode,
    )
    _add_points(travel, carried)
    matrices.ensure_profiles(travel, carried)
    state = replan.prepare(parent, event, mode, travel)
    day = Input(
        office=parent.input.office,
        requests=state.requests,
        engineers=state.engineers,
        duration_mode=parent.input.duration_mode,
    )
    _add_points(travel, day)
    # Бригада могла выйти на транспорте, которым в наборе никто не ездил: такого профиля
    # в таблицах нет, и первый же её переезд уронил бы расчёт (PLAN 6.19).
    matrices.ensure_profiles(travel, day)
    return state


def preview(parent: Plan, dataset: Dataset, event: Event) -> Preview:
    """Варианты реакции на событие — черновиками (PLAN 6.12, блок 53).

    Режимы: по умолчанию для события, пересчёт остатка, если умолчание — встраивание, и
    пересчёт всего дня. Режим, не принявший событие, называется с причиной; не принял ни
    один — ошибка режима по умолчанию, как у `POST /events`. Состояние дня готовится здесь:
    оно правит таблицы переездов и читает Mongo (`_algorithm_of`), а процессы пула получают
    только `build_plan` со всеми данными. Таблицы одни на все режимы: режимы одного события
    досчитывают одни и те же точки, и свежие таблицы на режим только повторили бы поход
    в Valhalla за точкой новой заявки.

    Варианты с одинаковыми маршрутами схлопываются в первый: выбирать между ними нечего.
    """
    started = perf_counter()
    default = replan.default_mode(event)
    wanted = [default]
    if default is ReplanMode.INSERT:
        wanted.append(ReplanMode.FROM_EVENT)
    wanted.append(ReplanMode.FULL)

    travel = matrices.get(dataset)
    prepared: dict[ReplanMode, State] = {}
    refused: dict[ReplanMode, replan.ReplanError] = {}
    for mode in wanted:
        try:
            prepared[mode] = replan_state(parent, event, mode, travel)
        except replan.ReplanError as error:
            refused[mode] = error
    if not prepared:
        raise refused[default]

    algorithm, setup = _algorithm_of(parent), _setup_of(parent)
    plans = parallel.run(
        build_plan,
        [(dataset, travel, algorithm, state, setup) for state in prepared.values()],
    )
    # Время расчёта у каждого черновика — общее ожидание пула плюс его собственные линии: линии
    # рисуются по очереди, и без этого каждый следующий черновик нёс бы чужие.
    solved = perf_counter() - started
    unique: dict[tuple, tuple[ReplanMode, Plan]] = {}
    for mode, plan in zip(prepared, plans, strict=True):
        key = tuple(
            (
                route.engineer_id,
                tuple((stop.request_id, stop.start) for stop in route.stops),
            )
            for route in plan.routes
        )
        unique.setdefault(key, (mode, plan))
    # Вариант, не прошедший проверку, — 500 на весь предпросмотр (API, `_checked`); тогда не
    # сохраняется и ни один из остальных: черновики без ответа лежали бы до TTL.
    valid = all(plan.validation.ok for _, plan in unique.values())
    options = [
        PreviewOption(
            mode=mode,
            plan=_store(plan, travel, perf_counter() - solved, draft=True)
            if valid
            else plan,
            summary=diff.summary(parent, plan, event),
        )
        for mode, plan in unique.values()
    ]
    return Preview(
        options=options,
        recommended=recommended(options, default),
        refused={mode: str(error) for mode, error in refused.items()},
        emergency_candidates=next(
            (state.candidates for state in prepared.values() if state.candidates), []
        ),
    )


def recommended(options: list[PreviewOption], default: ReplanMode) -> ReplanMode:
    """Какой вариант предпросмотра советовать диспетчеру (PLAN 6.12, блок 53).

    Режим по умолчанию: обычная заявка встраивается и сложившийся план не перестраивает
    (ответ 18). Другой — только если выполняет больше аварий, а при равных — больше заявок:
    ради километров дёргать бригады и клиентов не стоит. Пересчёт всего дня не советуется
    никогда: он переставляет уже начатые работы, это сравнительный расчёт. Не принял событие
    режим по умолчанию — первый из оставшихся.
    """
    if all(option.mode is not default for option in options):
        return options[0].mode

    def done(option: PreviewOption) -> tuple[int, int]:
        metrics = option.plan.metrics
        return (
            metrics.assigned_by_work_priority[WorkPriority.EMERGENCY],
            metrics.assigned_count,
        )

    # Умолчание идёт первым, а `max` при равенстве оставляет первого.
    return max(
        (option for option in options if option.mode is not ReplanMode.FULL), key=done
    ).mode


def commit(plan_id: str) -> Plan | None:
    """Применить вариант предпросмотра: снять отметку черновика (PLAN 6.12, блок 53).

    Содержимое плана не меняется — неизменяемость соблюдена, меняется только то, что план
    теперь версия цепочки. Остальные варианты того же события удаляются: выбор сделан. Чужой
    предпросмотр того же родителя (вторая вкладка, другое событие) остаётся — его применят или
    отбросят там. Повторное применение (второй клик) возвращает тот же план. `None` —
    черновика нет: его удалил TTL.
    """
    flipped = db.plans.update_one(
        {"_id": plan_id, "draft": True},
        {"$set": {"draft": False, "draftExpiresAt": None}},
    ).modified_count
    document = db.plans.find_one({"_id": plan_id})
    if document is None:
        return None
    if flipped:
        # Черновик стал версией — его закрытия теперь факты (блок 68).
        _learn(from_document(document))
        # Событие сравнивается в том виде, как оно лежит в базе: варианты одного предпросмотра
        # записаны из одного объекта события.
        db.plans.delete_many(
            {
                "parentPlanId": document["parentPlanId"],
                "event": document["event"],
                "draft": True,
            }
        )
    return from_document(document)


def _learn(plan: Plan) -> None:
    """Скорости в профилях по новым закрытиям версии (блок 68): сравнение — с родителем.

    Новый инженер начинает с чистого профиля (правки 82). Нет ни одного выполненного — и
    родитель не нужен: в базу за ним не ходим.
    """
    profiles.welcome(plan)
    if not any(closure.outcome is Outcome.DONE for closure in plan.closed.values()):
        return
    parent = get_plan(plan.parent_plan_id) if plan.parent_plan_id else None
    profiles.learn(plan, parent.closed if parent else {})


def clock(parent: Plan, dataset: Dataset, time: str) -> list[Plan]:
    """Часы дня (PLAN 9.1, блоки 68, 82): факты начатых работ к `time` — версиями по порядку.

    Что узнали часы, решает `facts.observe`, и одно его событие — одна версия режима `delay`,
    как обычное событие. Событие встаёт в самый ранний момент (блок 69), поэтому скачок на
    вечер — это десятки версий подряд; их считает один вызов, а не клиент по одной, и каждая
    сохраняется, как раньше. Таблицы переездов читаются один раз: часы точек не добавляют.
    Линии маршрутов, где точки не поменялись, берутся у предыдущей версии (`geometry.fill`).

    Ничего нового — пустой список. Время раньше события родителя — тоже: перемотка назад
    фактов не отменяет. На версии, не прошедшей проверку, цикл обрывается: она не сохранена,
    и следующая повисла бы на плане, которого нет.
    """
    moment = to_seconds(time)
    if parent.event and moment < to_seconds(parent.event.time):
        return []
    travel = matrices.get(dataset)
    _add_points(travel, parent.input)
    versions: list[Plan] = []
    # Каждая версия закрывает работу или чинит остаток, поэтому их не больше, чем стопов, —
    # предел только страхует от бесконечной цепочки.
    for _ in range(sum(len(route.stops) for route in parent.routes) + 1):
        event = facts.observe(parent, travel, moment)
        if event is None:
            break
        state = replan_state(parent, event, ReplanMode.DELAY, travel)
        plan = _finish(
            dataset,
            travel,
            _algorithm_of(parent),
            state,
            _setup_of(parent),
            previous=parent,
        )
        versions.append(plan)
        if not plan.validation.ok:
            break
        parent = plan
    return versions


def day_summary(plan: Plan) -> DaySummary:
    """«Итоги дня» (блок 68): работа план / факт по выполненным, скорость в начале и сейчас.

    План работы — длительность пары на начало дня (`duration.minutes`), факт — от начала работы
    по плану до закрытия. Отменённые собираются по цепочке версий: отменённой заявки во входе
    последней версии уже нет.
    """
    requests = {request.id: request for request in plan.input.requests}
    starts = {
        stop.request_id: to_seconds(stop.start)
        for route in plan.routes
        for stop in route.stops
    }
    now = profiles.speeds(plan.dataset_id)
    engineers = []
    for engineer in plan.input.engineers:
        done = [
            SummaryRequest(
                request_id=request_id,
                plan_min=duration.minutes(
                    requests[request_id], engineer, plan.input.duration_mode
                ),
                fact_min=max(0, (to_seconds(closure.time) - starts[request_id]) // 60),
            )
            for request_id, closure in plan.closed.items()
            if closure.engineer_id == engineer.id
            and closure.outcome is Outcome.DONE
            and request_id in requests
            and request_id in starts
        ]
        engineers.append(
            EngineerSummary(
                engineer_id=engineer.id,
                name=engineer.name,
                plan_min=sum(item.plan_min for item in done),
                fact_min=sum(item.fact_min for item in done),
                speed_start=engineer.speed,
                speed_now=now.get(engineer.id, engineer.speed),
                requests=done,
            )
        )
    cancelled = [
        request_id
        for request_id, closure in plan.closed.items()
        if closure.outcome is Outcome.CANCELLED
    ]
    version: Plan | None = plan
    while version is not None:
        if version.event and version.event.type == "cancel_request":
            cancelled += [
                request_id
                for request_id in version.event.request_ids
                if request_id not in cancelled
            ]
        version = get_plan(version.parent_plan_id) if version.parent_plan_id else None
    failed = [
        request_id
        for request_id, closure in plan.closed.items()
        if closure.outcome is Outcome.FAILED
    ]
    return DaySummary(
        engineers=engineers,
        cancelled=cancelled,
        deferred=[
            request_id for request_id in plan.deferred if request_id not in failed
        ],
        failed=failed,
        unassigned=[
            item.request_id
            for item in plan.unassigned
            if item.reason_code != explain.DEFERRED
        ],
    )


def manual_plan(
    parent: Plan,
    dataset: Dataset,
    request_id: str,
    engineer_id: str | None,
    position: int | None,
    draft: bool = False,
) -> Plan:
    """Новая версия плана после ручного переназначения (PLAN 6.16). `ReplanError` — отказ.
    `draft` — «что будет, если» ассистента (блок 61), как у `replan_plan`.
    """
    travel, state = _manual_day(parent, dataset)
    edit = manual.move(state, travel, request_id, engineer_id, position)
    return _finish(
        dataset,
        travel,
        Algorithm.MANUAL,
        state,
        _setup_of(parent),
        edit=edit,
        draft=draft,
    )


def _manual_day(parent: Plan, dataset: Dataset) -> tuple[Travel, State]:
    """Таблицы и состояние дня для ручной правки (PLAN 6.16).

    Точки и профили досчитываются так же, как при событии: у родителя может стоять заявка
    или бригада, которой в наборе нет (PLAN 5.5), и без её плеч расчёт упал бы 500.
    """
    travel = matrices.get(dataset)
    _add_points(travel, parent.input)
    matrices.ensure_profiles(travel, parent.input)
    return travel, manual.day(parent, travel)


def _algorithm_of(plan: Plan) -> Algorithm:
    """Чем считать остаток дня после события (PLAN 11.1).

    Алгоритм наследуется от родителя, но `manual` — не способ расчёта, а отметка ручной
    правки: распределение там задал диспетчер, и пересчитать им нечего. Поднимаемся по
    цепочке версий до того алгоритма, которым день считался до правки, — иначе первое же
    событие после переназначения отвечало бы 500.
    """
    while plan.algorithm is Algorithm.MANUAL:
        # Ручная правка делается по готовому плану, поэтому родитель у неё есть всегда.
        plan = get_plan(plan.parent_plan_id)
    return plan.algorithm


def _setup_of(plan: Plan) -> Setup | None:
    """Настройка варианта, которым посчитан план, если это вариант сравнения (PLAN 6.14).

    Порядок критериев наследуется от родителя вместе с алгоритмом (PLAN 11.1) и по той же
    причине: день, выбранный диспетчером как «тип работ важнее количества», после события не
    должен молча пересчитаться другой целью — да ещё и потерять подпись варианта.
    Вместе с порядком наследуется `guided`: у «быстрого расчёта» событие тоже считается без
    поиска. Пределы поиска у события свои, перепланирования (PLAN 6.5): с ними ответ на событие
    укладывается в 10 с и на самом большом участке, а пределы варианта рассчитаны на расчёт дня
    с нуля.
    """
    return variants.SETUPS.get(plan.variant) if plan.variant else None


def _add_points(travel: Travel, data: Input) -> None:
    """Точки входа, которых в таблицах набора нет или которые переехали.

    Заявки события, старты новых бригад — и адрес, поправленный событием `update_request`:
    идентификатор заявки от адреса не зависит, поэтому «точка уже есть» ещё не значит,
    что она стоит там же (PLAN 6.19).
    """
    for pid, lat, lon in matrices.points(data):
        travel.sync_point(pid, lat, lon)


def _finish(
    dataset: Dataset,
    travel: Travel,
    algorithm: Algorithm,
    state: State | None,
    setup: Setup | None = None,
    group: str | None = None,
    edit: Manual | None = None,
    draft: bool = False,
    norm: NormMetrics | None = None,
    previous: Plan | None = None,
) -> Plan:
    """Расчёт, линии маршрутов и сохранение, если проверка прошла. `previous` — версия, у
    которой можно взять линии неизменившихся маршрутов (часы дня)."""
    started = perf_counter()
    plan = build_plan(dataset, travel, algorithm, state, setup, edit, cache=True)
    plan.compare_group_id = group
    plan.norm_metrics = norm
    return _store(plan, travel, started, draft=draft, previous=previous)


def _store(
    plan: Plan,
    travel: Travel,
    started: float,
    draft: bool = False,
    previous: Plan | None = None,
) -> Plan:
    """Линии маршрутов и сохранение готового плана, если проверка прошла.

    Непрошедший проверку план не сохраняется: коллекция не должна копить заведомо битые
    документы, на которые вешается `parentPlanId`. API отвечает на такой план 500.
    Линии рисуются здесь, а не в `build_plan`: непрошедший план никто не увидит, запросы к
    Valhalla стоят дороже всего остального, а процесс пула (блок 53) в HTTP не ходит.
    Рисуются до сохранения — иначе сохранённый план пришлось бы догонять запросами при
    каждом чтении. `draft` — вариант предпросмотра (PLAN 6.12): живёт час, если его не
    применят.
    """
    if not plan.validation.ok:
        return plan

    geometry.fill(plan, travel, previous)
    # Время расчёта — то, что диспетчер прождал: поиск, досчёт, объяснения, проверка и линии
    # маршрутов. Оно стоит столбцом в сравнении вариантов (PLAN 6.14), поэтому попадает
    # в документ до сохранения. Тысячные, а не десятые: базовый вариант на маленьком наборе
    # считается быстрее, чем за десятую секунды, и округление превратило бы замер в ноль.
    plan.compute_sec = round(perf_counter() - started, 3)
    if draft:
        plan.draft = True
        plan.draft_expires_at = datetime.now(UTC) + DRAFT_TTL
    db.plans.insert_one(to_document(plan))
    if not draft:
        _learn(plan)
    return plan


def get_plan(plan_id: str) -> Plan | None:
    document = db.plans.find_one({"_id": plan_id})
    return from_document(document) if document else None


def to_document(plan: Plan) -> dict:
    document = plan.model_dump(by_alias=True, exclude={"id"})
    document["_id"] = plan.id
    return document


def from_document(document: dict) -> Plan:
    return Plan(id=document["_id"], **{k: v for k, v in document.items() if k != "_id"})


def _full(
    data: Input,
    committed: dict[str, list[Request]],
    routes: dict[str, list[Request]],
) -> dict[str, list[Request]]:
    """Маршрут целиком: закреплённое начало плюс пересчитанный остаток."""
    return {
        engineer.id: committed.get(engineer.id, []) + routes[engineer.id]
        for engineer in data.engineers
    }


def _left(data: Input, routes: dict[str, list[Request]]) -> list[Request]:
    """Кто остался без бригады после ручной правки: маршруты задал диспетчер (PLAN 6.16)."""
    placed = {request.id for requests in routes.values() for request in requests}
    return [request for request in data.requests if request.id not in placed]


def _held(pinned: dict[str, str], engineers: list[Engineer]) -> dict[str, str]:
    """Закрепление за выбывшей бригадой снимается до расчёта (PLAN 6.16, п. 6).

    Иначе заявка, закреплённая за отпавшей бригадой, не досталась бы никому: запрет
    «только своей бригаде» пережил бы саму бригаду. Снятие видно в `diff` — заявка
    приходит туда как `reassigned`.
    """
    working = {
        engineer.id for engineer in engineers if engineer.unavailable_from is None
    }
    return {
        request_id: engineer_id
        for request_id, engineer_id in pinned.items()
        if engineer_id in working
    }


def _pins(pinned: dict[str, str], assignments: dict[str, str | None]) -> dict[str, str]:
    """В плане остаются только закрепления, которые в нём же и выполнены (PLAN 6.16, п. 6).

    Закреплённая бригада могла перестать успевать — тогда заявка осталась без исполнителя,
    и держать закрепление не за что: следующее перепланирование должно быть свободно.
    """
    return {
        request_id: engineer_id
        for request_id, engineer_id in pinned.items()
        if assignments.get(request_id) == engineer_id
    }


def _postponed_text(
    request: Request,
    data: Input,
    deferred: dict[str, str],
    closed: dict[str, Closure],
) -> str:
    """Почему заявка не делается сегодня: перенёс диспетчер или бригада не смогла (PLAN 6.19)."""
    closure = closed.get(request.id)
    if closure is None:
        return explain.deferred_text(request, deferred[request.id])
    return explain.failed_text(request, _engineer(data, closure.engineer_id), closure)


def _closed_explanations(
    data: Input, closed: dict[str, Closure]
) -> dict[str, Explanation]:
    """Подпись под закрытой фактом заявкой вместо таблицы кандидатов (PLAN 6.19)."""
    return {
        request.id: Explanation(
            text=explain.failed_text(
                request,
                _engineer(data, closed[request.id].engineer_id),
                closed[request.id],
            )
            if closed[request.id].outcome is Outcome.FAILED
            else explain.closed_text(
                request,
                _engineer(data, closed[request.id].engineer_id),
                closed[request.id],
            )
        )
        for request in data.requests
        if request.id in closed
    }


def _engineer(data: Input, engineer_id: str) -> Engineer:
    return next(item for item in data.engineers if item.id == engineer_id)


def _committed_explanations(data: Input, state: State | None) -> dict[str, Explanation]:
    """Подпись под закреплённой заявкой: почему её назначение не пересматривалось."""
    if state is None:
        return {}
    engineers = {engineer.id: engineer for engineer in data.engineers}
    sent = {candidate.engineer_id: candidate for candidate in state.candidates}
    emergency = getattr(state.event, "request", None)
    return {
        request.id: Explanation(
            text=explain.dispatch_text(
                request, engineers[engineer_id], sent[engineer_id], state.time
            )
            if emergency is not None
            and request.id == emergency.id
            and engineer_id in sent
            else explain.committed_text(request, engineers[engineer_id], state.time)
        )
        for engineer_id, requests in state.committed.items()
        for request in requests
    }


def _route_explanations(
    data: Input,
    travel: Travel,
    routes: dict[str, list[Request]],
    committed: dict[str, list[Request]],
    positions: dict[str, Position],
    state: State | None,
    visits: dict[str, list[Visit]],
    figures: Metrics,
) -> dict[str, RouteExplanation]:
    """Два текста на бригаду (PLAN 6.9).

    «Почему эти заявки у неё» — про весь её день, вместе с закреплённым: диспетчер открывает
    карточку и видит день целиком. «Почему такой порядок» — про остаток дня: закреплённые
    стопы перестановке не подлежат.
    """
    texts = {}
    for engineer in data.engineers:
        prefix = (
            explain.committed_prefix(committed.get(engineer.id, []), state.time)
            if state
            else ""
        )
        texts[engineer.id] = RouteExplanation(
            assignment=explain.assignment_text(
                engineer,
                committed.get(engineer.id, []) + routes[engineer.id],
                visits[engineer.id],
                figures.utilization_by_engineer[engineer.id],
            ),
            order=prefix
            + explain.route_text(
                engineer, routes[engineer.id], travel, positions[engineer.id]
            ),
        )
    return texts


def _assignments(data: Input, routes: dict[str, list]) -> dict[str, str | None]:
    """Заявка → бригада, `None` — не назначена. Порядок заявок входа сохраняется."""
    assigned = {
        request.id: engineer_id
        for engineer_id, requests in routes.items()
        for request in requests
    }
    return {request.id: assigned.get(request.id) for request in data.requests}


def _route(
    engineer_id: str,
    visits: list[Visit],
    committed: int = 0,
    lunch: tuple[int, int] | None = None,
) -> Route:
    # Километры маршрута считаются от суммы метров, а не от суммы округлённых стопов (PLAN 6.7).
    return Route(
        engineer_id=engineer_id,
        km=to_km(meters(visits)),
        stops=[_stop(visit, number < committed) for number, visit in enumerate(visits)],
        lunch=_lunch(visits, lunch),
    )


def _eaten(
    engineer: Engineer,
    heads: list[Visit],
    tails: list[Visit],
    positions: dict[str, Position],
) -> tuple[int, int] | None:
    """Когда бригада обедает за день (блоки 79, 82): обед, поставленный проходом, а нет его —
    начатый в простое после закреплённой части до события (`replan.continuation`). Такой
    обед показывается, только если после него есть работа: обед бывает лишь между работами.
    """
    lunch = lunch_slot(heads + tails)
    if lunch is None and heads and tails and positions[engineer.id].lunch_done:
        lunch = lunch_after(engineer, heads)
    return lunch


def _lunch(visits: list[Visit], lunch: tuple[int, int] | None) -> Lunch | None:
    """Обед маршрута (блоки 79, 82): после какой работы — последней, кончившейся до него."""
    if lunch is None or not visits:
        return None
    before = [visit for visit in visits if visit.end <= lunch[0]]
    return Lunch(
        start=to_clock(lunch[0]),
        end=to_clock(lunch[1]),
        after=before[-1].request_id if before else None,
    )


def _stop(visit: Visit, committed: bool = False) -> Stop:
    """Внутренние секунды и метры — в единицы API (PLAN 2.6)."""
    return Stop(
        request_id=visit.request_id,
        departure=to_clock(visit.departure),
        arrival=to_clock(visit.arrival),
        start=to_clock(visit.start),
        end=to_clock(visit.end),
        travel_km=to_km(visit.travel_m),
        travel_min=to_minutes(visit.travel_sec),
        work_min=to_minutes(visit.work_sec),
        committed=committed,
    )
