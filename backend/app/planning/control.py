"""День людей: как контрольную выборку распределили диспетчеры (PLAN 6.10, блок 51).

В контрольной выборке есть бригада каждой заявки, но нет времени работ. День восстанавливается
нашим же расписанием при самых мягких допущениях: у бригады все навыки, оборудование и
инструменты, автомобиль, смена 09:30–23:00, день начинается с первой её заявки (дорога до неё не
считается), заявки идут по концу окна. Поэтому любое опоздание здесь — следствие самого
распределения, а не наших допущений.

Справка, а не вход алгоритма: считает её скрипт подготовки, в расчёт плана она не попадает.
"""

from ..dictionaries import (
    SHIFT_PRESETS,
    DurationMode,
    Equipment,
    Skill,
    Tool,
    Transport,
)
from ..models import ControlRun, Engineer, Input
from ..routing.matrices import Travel, point_id
from ..timeutil import to_km, to_seconds
from . import schedule

# Самая длинная смена, которую допускают организаторы (ответ 29.09).
SHIFT = SHIFT_PRESETS["2-2-long"]


def people_day(data: Input, assignment: dict[str, str], travel: Travel) -> ControlRun:
    """`assignment` — заявка -> бригада контрольного дня; заявок вне него в дне людей нет."""
    requests = {request.id: request for request in data.requests}
    days: dict[str, list] = {}
    for request_id, brigade in assignment.items():
        days.setdefault(brigade, []).append(requests[request_id])

    late = over_shift = meters = 0
    for brigade, route in days.items():
        # По концу окна (правило EDD — меньше всего опозданий у одного исполнителя), при
        # равном — по началу, затем порядок файла. По началу окна авария с окном на весь день
        # вставала бы первой и сдвигала утро: опоздание от нашего порядка, а не от людей.
        route.sort(key=lambda r: (to_seconds(r.window_end), to_seconds(r.window_start)))
        engineer = Engineer(
            id=brigade,
            name=brigade,
            start_lat=data.office.lat,
            start_lon=data.office.lon,
            shift_start=SHIFT["start"],
            shift_end=SHIFT["end"],
            skills=list(Skill),
            transport=Transport.CAR,
            equipment=list(Equipment),
            tools=list(Tool),
        )
        # День с первой заявки: первая дуга — из точки в неё же, 0 с и 0 м.
        # День людей — при мягких допущениях и по нормативу: сложности в контроле нет.
        start = schedule.Position(
            point_id(route[0].id),
            None,
            equipment_used=0,
            duration_mode=DurationMode.NORM,
            finished={},
            turns={},
            # День людей — факт выгрузки, а не наш расчёт: обед в него не вставляем (блок 79).
            lunch_done=True,
            after_work=False,
        )
        visits = schedule.build_schedule(engineer, route, travel, start)
        # Опоздание — начало позже конца окна, правило ТЗ (п. 2.2), а не `WINDOW_RULE`: факт
        # дня людей не должен зависеть от настройки нашего расчёта, а `control.json` — от `.env`.
        late += sum(
            visit.start > to_seconds(request.window_end)
            for visit, request in zip(visits, route, strict=True)
        )
        over_shift += visits[-1].end > to_seconds(SHIFT["end"])
        meters += schedule.meters(visits)

    return ControlRun(
        engineers=len(days),
        assigned=len(assignment),
        km=to_km(meters),
        late=late,
        over_shift=over_shift,
    )
