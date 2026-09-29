"""Прогноз опозданий (PLAN 6.18).

Прогоны дня со случайными отклонениями длительностей работ и переездов: порядок маршрутов
не меняется, расписание каждого прогона считается теми же формулами, что и план (PLAN 6.2).
Своей проверки ограничений здесь нет и быть не должно — отклонения подставляются в копии
заявок и в обёртку над таблицами переездов, а считает по-прежнему `schedule.build_schedule`.

Это **модельная** оценка: множители заданы нами, а не получены из истории. Seed фиксирован,
поэтому два расчёта одного плана дают один прогноз.
"""

import random
from dataclasses import dataclass

from ..config import settings
from ..dictionaries import Transport
from ..models import (
    EngineerForecast,
    Forecast,
    Input,
    Request,
    RequestForecast,
    Risk,
)
from ..routing.matrices import Travel
from ..timeutil import to_clock_over_midnight, to_seconds
from .schedule import Position, build_schedule, latest_start

# Границы уровней риска (PLAN 6.18). Это словарь диспетчера, а не настройка: «средний риск»
# обязан означать одно и то же во всех расчётах и на всех стендах.
MEDIUM_FROM = 0.10
HIGH_FROM = 0.30


@dataclass(frozen=True)
class _Deviated:
    """Таблицы переездов со случайно отклонившимся временем.

    `schedule.run` спрашивает у таблиц одно — плечо между двумя точками, — поэтому подменяется
    ровно один метод, а формулы расписания остаются в единственном экземпляре. Метры не
    трогаем: километры маршрута от отклонения времени не зависят.
    """

    tables: Travel
    rnd: random.Random

    def travel(
        self, transport: Transport, from_id: str, to_id: str
    ) -> tuple[int, int] | None:
        leg = self.tables.travel(transport, from_id, to_id)
        if leg is None:
            return None
        seconds, meters = leg
        factor = _factor(self.rnd, settings.forecast_travel_factor)
        return round(seconds * factor), meters


def compute(
    data: Input,
    travel: Travel,
    routes: dict[str, list[Request]],
    positions: dict[str, Position],
) -> Forecast:
    """Вероятность опоздания по каждой заявке и выхода за смену по каждой бригаде.

    `routes` и `positions` — остаток дня и точки, от которых он считается: те же, по которым
    построено расписание плана. Закреплённые стопы сюда не попадают — работа по ним уже идёт
    или бригада к ним выехала, и отклонять там нечего (PLAN 6.12).
    """
    rnd = random.Random(settings.forecast_seed)
    runs = settings.forecast_runs
    deviated = _Deviated(travel, rnd)

    starts: dict[str, list[int]] = {
        request.id: [] for route in routes.values() for request in route
    }
    late = dict.fromkeys(starts, 0)
    over = {engineer.id: 0 for engineer in data.engineers if routes[engineer.id]}

    for _ in range(runs):
        for engineer in data.engineers:
            route = routes[engineer.id]
            if not route:
                continue
            stretched = [_stretch(request, rnd) for request in route]
            visits = build_schedule(
                engineer, stretched, deviated, positions[engineer.id]
            )
            for request, visit in zip(stretched, visits, strict=True):
                starts[request.id].append(visit.start)
                # Правило окна читается там же, где его читает расписание. У растянувшейся
                # работы `latest_start` сдвигается ровно на её отклонение, поэтому при
                # `fit_in_window` это и есть «окончание позже конца окна» (PLAN 6.18).
                late[request.id] += visit.start > latest_start(request)
            over[engineer.id] += visits[-1].end > to_seconds(engineer.shift_end)

    return Forecast(
        requests={
            request_id: _request_forecast(late[request_id] / runs, times)
            for request_id, times in starts.items()
        },
        engineers={
            engineer_id: EngineerForecast(over_shift_probability=round(count / runs, 3))
            for engineer_id, count in over.items()
        },
    )


def _stretch(request: Request, rnd: random.Random) -> Request:
    """Копия заявки с отклонившейся длительностью работы.

    Копия, а не правка на месте: заявки входа принадлежат плану, и прогон дня их не трогает.
    """
    minutes = round(
        request.service_duration_min * _factor(rnd, settings.forecast_work_factor)
    )
    return request.model_copy(update={"service_duration_min": minutes})


def _factor(rnd: random.Random, bounds: tuple[float, float, float]) -> float:
    """Жребий из треугольного распределения.

    В настройках множители записаны по-человечески — `(низ, мода, верх)`, у `random` порядок
    другой — `(низ, верх, мода)`. Перестановка делается здесь, один раз на весь проект.
    """
    low, mode, high = bounds
    return rnd.triangular(low, high, mode)


def _request_forecast(probability: float, times: list[int]) -> RequestForecast:
    """Вероятность, 90-й процентиль начала и уровень риска по одной заявке.

    Процентиль берётся элементом отсортированного списка, без интерполяции: наружу уходит
    время одного из прогонов, а не среднее между двумя.
    """
    p90 = sorted(times)[int(0.9 * (len(times) - 1))]
    probability = round(probability, 3)
    return RequestForecast(
        late_probability=probability,
        # Прогон — не план: в нём работа может уехать и за полночь, и падать на этом нельзя.
        p90_start=to_clock_over_midnight(p90),
        risk=_risk(probability),
    )


def _risk(probability: float) -> Risk:
    """Низкий < 10 %, средний 10–30 %, высокий > 30 % (PLAN 6.18)."""
    if probability > HIGH_FROM:
        return Risk.HIGH
    return Risk.MEDIUM if probability >= MEDIUM_FROM else Risk.LOW
