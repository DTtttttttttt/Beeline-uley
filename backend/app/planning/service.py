"""Сборка плана: набор → расчёт → метрики → независимая проверка → сохранение (PLAN 4.1).

`build_plan` ничего не знает про Mongo и HTTP — её и проверяют тесты; `create_plan` добавляет
таблицы переездов, линии маршрутов и сохранение. Планы неизменяемы: каждый расчёт создаёт
новый документ.

Перепланирование по событию идёт **через те же функции**: `replan.State` описывает, что уже
закреплено и откуда бригады продолжают, а сборка, объяснения, метрики и проверка остаются
в одном экземпляре (PLAN 6.12).
"""

from datetime import UTC, datetime
from time import perf_counter
from uuid import uuid4

from .. import db
from ..config import settings
from ..dictionaries import Outcome
from ..models import (
    Algorithm,
    Closure,
    Dataset,
    Engineer,
    Event,
    Explanation,
    Input,
    Metrics,
    Plan,
    ReplanMode,
    Request,
    Route,
    RouteExplanation,
    Stop,
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
    explain,
    forecast,
    manual,
    metrics,
    optimizer,
    repair,
    replan,
    status,
    validator,
    variants,
)
from .manual import Manual
from .replan import State
from .schedule import (
    Position,
    Visit,
    build_schedule,
    day_start,
    meters,
    start_positions,
)
from .variants import Setup


def build_plan(
    dataset: Dataset,
    travel: Travel,
    algorithm: Algorithm = Algorithm.BASELINE,
    state: State | None = None,
    setup: Setup | None = None,
    edit: Manual | None = None,
) -> Plan:
    """План по набору данных, без Mongo. Проверка кладётся в `plan.validation` (PLAN 6.7).

    `state` — состояние дня на момент события (PLAN 6.12): тогда планируется только остаток,
    а закреплённые стопы переходят в новую версию как есть.

    `setup` — настройка варианта сравнения (PLAN 6.14): порядок критериев и лимит поиска
    вместо тех, что стоят в `settings`. Без него считается обычный план дня.

    `edit` — готовые маршруты ручной правки (PLAN 6.16): алгоритма у неё нет, распределение
    задал диспетчер, и считать тут нечего — остальное собирается как у любого другого плана.
    """
    # В plan.input — весь день: закреплённые заявки, срочная из события и всё остальное.
    # При перепланировании вход целиком берётся у родителя, а не у набора: новая версия — это
    # прошлый план плюс событие, и набор в базе к тому моменту мог смениться (reset, импорт).
    base = state.parent.input if state else dataset
    data = Input(
        office=base.office,
        requests=state.requests if state else dataset.requests,
        engineers=state.engineers if state else base.engineers,
    )
    committed = state.committed if state else {}
    # Где у бригад начался день: офис или их дом (PLAN 2.4), а у вышедшей в течение дня —
    # время её выхода. По этим позициям пересчитывается закреплённая часть маршрута.
    starts = {
        engineer.id: day_start(data.office, engineer) for engineer in data.engineers
    }
    # Откуда планируется остаток: при перепланировании — точки продолжения из состояния
    # дня, иначе то же начало дня с учётом выбывших бригад.
    positions = state.positions if state else start_positions(data)
    # Планируется только незакреплённое: закреплённое уже роздано и пересмотру не подлежит.
    free = Input(
        office=data.office,
        requests=state.free() if state else data.requests,
        engineers=data.engineers,
    )

    # Закрепления ручной правки наследуются версией и сужают выбор бригады (PLAN 6.16, п. 6).
    pinned = _held(state.parent.pinned, data.engineers) if state else {}
    # Встраивание — только для самого события. Ручная правка поверх такой версии наследует
    # её режим (PLAN 6.16), но распределение там задал диспетчер, и вставлять заново нечего.
    inserting = edit is None and state is not None and state.mode is ReplanMode.INSERT
    if edit is not None:
        routes, unassigned, fallback = edit.routes, _left(free, edit.routes), False
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
        routes, unassigned, fallback = _optimize(free, travel, state, setup, pinned)
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

    # Объяснения строятся по готовому плану, поэтому оба алгоритма получают их одинаково.
    # Кандидаты считаются по остатку дня и от текущего состояния бригад — именно это и
    # нужно диспетчеру после события.
    reasons, explanations = explain.explain_requests(
        free, travel, routes, tails, unassigned, positions, risk.requests
    )
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
            _route(engineer_id, stops, len(heads[engineer_id]))
            for engineer_id, stops in visits.items()
        ],
        assignments=assignments,
        pinned=_pins(edit.pinned if edit else pinned, assignments),
        unassigned=reasons,
        deferred=deferred,
        closed=closed,
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
        forecast=risk,
        approximate=travel.approximate,
        approximate_note=explain.approximate_note(data, travel),
        fallback_to_baseline=fallback,
        created_at=datetime.now(UTC),
    )
    if state:
        plan.diff = diff.compare(state.parent, plan)
    plan.validation = validator.validate(plan, travel)
    return plan


def _optimize(
    data: Input,
    travel: Travel,
    state: State | None = None,
    setup: Setup | None = None,
    pinned: dict[str, str] | None = None,
) -> tuple[dict[str, list[Request]], list[Request], bool]:
    """Оптимизация, досчёт и выбор лучшего из двух планов (PLAN 6.5, 6.6).

    Оптимизированный план никогда не хуже базового: если он проиграл по цели или солвер
    не нашёл решения, возвращается базовый и план помечается `fallbackToBaseline` —
    подмена не должна быть незаметной.

    В сравнении вариантов (`setup`, PLAN 6.14) подмена базовым не применяется: вариант
    показывают ровно таким, каким его сделала его же настройка, иначе сравнивать было бы
    нечего. Базовый вариант там считается отдельной строкой таблицы, поэтому здесь он
    не считается вовсе. Случай «солвер не нашёл решения» — исключение: плана в нём просто
    нет, и вернуть вместо него нечего, кроме базового, зато это видно по флагу.
    """
    positions = state.positions if state else start_positions(data)
    pinned = pinned or {}
    mode = setup.mode if setup else settings.objective_mode
    # Лимит поиска: у варианта свой (PLAN 6.14), у перепланирования короче первичного
    # расчёта — прошлый план подаётся стартовым решением, и искать с нуля не приходится.
    if setup:
        limit = setup.time_limit
    elif state:
        limit = settings.replan_time_limit_sec
    else:
        limit = settings.solver_time_limit_sec
    weights = optimizer.make_weights(
        optimizer.ORDERS[mode], optimizer.bounds(data, travel)
    )
    solved = optimizer.solve(
        data,
        travel,
        candidates.allowed(data, travel, positions, pinned),
        weights,
        limit,
        positions,
        _initial(state) if state else None,
        guided=setup.guided if setup else True,
    )
    if solved is None:
        return *baseline.assign(data, travel, positions, pinned), True

    routes, unassigned = solved
    unassigned = repair.fill(
        data, routes, unassigned, travel, weights, positions, pinned
    )
    if setup:
        return routes, unassigned, False

    base = baseline.assign(data, travel, positions, pinned)
    if _cost(data, travel, routes, unassigned, weights, positions) <= _cost(
        data, travel, *base, weights, positions
    ):
        return routes, unassigned, False
    return *base, True


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
    repair.fill(
        data,
        routes,
        [known[state.event.request.id]],
        travel,
        weights,
        state.positions,
        pinned,
    )
    return routes


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

    Отменённая заявка сюда попасть может: незнакомые заявки `optimizer` отбрасывает сам.
    А вот выбывшая бригада — нет: её старт прижат к концу смены, и прошлые стопы сделали бы
    стартовое решение недопустимым целиком. Тогда солвер молча искал бы с нуля, да ещё на
    укороченном лимите перепланирования, которым он расплачивается как раз за этот прогрев.
    """
    fixed = {
        request.id for requests in state.committed.values() for request in requests
    }
    withdrawn = (
        state.event.engineer_id
        if state.event and state.event.type == "engineer_unavailable"
        else None
    )
    return {
        route.engineer_id: [
            stop.request_id for stop in route.stops if stop.request_id not in fixed
        ]
        for route in state.parent.routes
        if route.engineer_id != withdrawn
    }


def _cost(
    data: Input,
    travel: Travel,
    routes: dict[str, list[Request]],
    unassigned: list[Request],
    weights: dict[str, int],
    positions: dict[str, Position],
) -> int:
    """Значение цели для готового плана — тем же кодом, что строит цель солвера."""
    driven = sum(
        meters(
            build_schedule(
                engineer,
                routes[engineer.id],
                travel,
                positions[engineer.id],
            )
        )
        for engineer in data.engineers
    )
    return optimizer.cost(routes, unassigned, driven, weights)


def create_plan(dataset: Dataset, algorithm: Algorithm = Algorithm.BASELINE) -> Plan:
    """Первичный расчёт дня по набору данных."""
    return _finish(dataset, matrices.get(dataset), algorithm, None)


def compare(dataset: Dataset, wanted: list[Variant]) -> list[Plan]:
    """Один день несколькими настройками расчёта (PLAN 6.14).

    Таблицы переездов читаются один раз на все варианты: они описывают точки набора и от
    настройки не зависят, а поход за ними — это Mongo, а при пустом кэше и Valhalla.
    Варианты считаются по очереди: каждый занимает солвером целое ядро, и параллельный
    запуск только поделил бы между ними одно и то же процессорное время.

    Планы сохраняются как обычные, со своими `id` и общим `compareGroupId`: любой из них
    открывается по `GET /api/plans/{id}` и годится в родители события.
    """
    travel = matrices.get(dataset)
    group = uuid4().hex
    return [
        _finish(dataset, travel, setup.algorithm, None, setup, group)
        for setup in (variants.SETUPS[variant] for variant in wanted)
    ]


def replan_plan(parent: Plan, dataset: Dataset, event: Event, mode: ReplanMode) -> Plan:
    """Новая версия плана после события (PLAN 6.12). `replan.ReplanError` — событие не применимо.

    Алгоритм наследуется от родителя: посчитать базовый план оптимизатором значило бы
    подменить точку отсчёта, с которой этот же план сравнивают (PLAN 6.6).
    """
    travel = matrices.get(dataset)
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
    )
    _add_points(travel, carried)
    matrices.ensure_profiles(travel, carried)
    state = replan.prepare(parent, event, mode, travel)
    day = Input(
        office=parent.input.office,
        requests=state.requests,
        engineers=state.engineers,
    )
    _add_points(travel, day)
    # Бригада могла выйти на транспорте, которым в наборе никто не ездил: такого профиля
    # в таблицах нет, и первый же её переезд уронил бы расчёт (PLAN 6.19).
    matrices.ensure_profiles(travel, day)
    return _finish(dataset, travel, _algorithm_of(parent), state, _setup_of(parent))


def manual_plan(
    parent: Plan,
    dataset: Dataset,
    request_id: str,
    engineer_id: str | None,
    position: int | None,
) -> Plan:
    """Новая версия плана после ручного переназначения (PLAN 6.16). `ReplanError` — отказ.

    Точки и профили досчитываются так же, как при событии: у родителя может стоять заявка
    или бригада, которой в наборе нет (PLAN 5.5), и без её плеч расчёт упал бы 500.
    """
    travel = matrices.get(dataset)
    _add_points(travel, parent.input)
    matrices.ensure_profiles(travel, parent.input)
    state = manual.day(parent, travel)
    edit = manual.move(state, travel, request_id, engineer_id, position)
    return _finish(
        dataset, travel, Algorithm.MANUAL, state, _setup_of(parent), edit=edit
    )


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
    Вместе с порядком наследуется и остальная настройка, включая лимит поиска: у «быстрого
    расчёта» событие считается так же быстро, как и сам вариант.
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
) -> Plan:
    """Расчёт, линии маршрутов и сохранение, если проверка прошла.

    Непрошедший проверку план не сохраняется: коллекция не должна копить заведомо битые
    документы, на которые вешается `parentPlanId`. API отвечает на такой план 500.
    Линии рисуются там же, внутри: непрошедший план никто не увидит, а запросы к Valhalla
    стоят дороже всего остального. Рисуются до сохранения — иначе сохранённый план
    пришлось бы догонять запросами при каждом чтении.
    """
    started = perf_counter()
    plan = build_plan(dataset, travel, algorithm, state, setup, edit)
    plan.compare_group_id = group
    if not plan.validation.ok:
        return plan

    geometry.fill(plan, travel)
    # Время расчёта — то, что диспетчер прождал: поиск, досчёт, объяснения, проверка и линии
    # маршрутов. Оно стоит столбцом в сравнении вариантов (PLAN 6.14), поэтому попадает
    # в документ до сохранения. Тысячные, а не десятые: базовый вариант на маленьком наборе
    # считается быстрее, чем за десятую секунды, и округление превратило бы замер в ноль.
    plan.compute_sec = round(perf_counter() - started, 3)
    db.plans.insert_one(to_document(plan))
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
    return {
        request.id: Explanation(
            text=explain.committed_text(request, engineers[engineer_id], state.time)
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


def _route(engineer_id: str, visits: list[Visit], committed: int = 0) -> Route:
    # Километры маршрута считаются от суммы метров, а не от суммы округлённых стопов (PLAN 6.7).
    return Route(
        engineer_id=engineer_id,
        km=to_km(meters(visits)),
        stops=[_stop(visit, number < committed) for number, visit in enumerate(visits)],
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
        committed=committed,
    )
