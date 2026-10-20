"""Настройки вариантов расчёта (PLAN 6.14).

Здесь только таблица настроек: что подставить вместо `OBJECTIVE_MODE` и лимита поиска,
когда один и тот же день считается несколькими способами. Сам расчёт, сохранение и общий
`compareGroupId` — в `service.compare`: обратная зависимость завела бы цикл импорта, а
состав модулей в PLAN 4.1 называет этот файл именно настройками.

`emergency_first` — основная модель, то же, что `OBJECTIVE_MODE` по умолчанию. «Быстрый
расчёт» считается ею же: он показывает, во что обходится отказ от поиска у основной
модели, а не у одного из сравнительных порядков.
"""

from dataclasses import dataclass

from ..models import Algorithm, Variant

# Предел поиска на вариант (PLAN 6.14, замер блока 45). На «Востоке» и «Югоцентре» вариант
# укладывается в прежние 10 с (7,5 и 5,1 с), на «Юго-востоке» (83 заявки) идёт 14 с. Меньший
# предел укладывался бы везде, но на «Востоке» вариант терял 48 км: 400 решений дают 231,5 км,
# 800 — 183,7, как и 1 600. Событие по варианту ищет с пределом перепланирования. Время —
# страховка, как и у первичного расчёта (PLAN 6.5). Быстрый вариант показывает, во что обходится отказ от
# поиска: одно первое решение (`LOCAL_CHEAPEST_INSERTION`), предел решений у него не
# действует, а лимит времени — та же страховка. С учётом сложности и разными скоростями
# инженеров (блок 68) матрица времени у каждого своя, и решение дороже: «Аварии сразу» на
# «Юго-востоке» с расширенным составом идёт 28 с на свободной машине (по нормативу — 16 с).
# С резервом и рейтингом (блок 71) и обедом в модели (блок 79) «Рейтинг в приоритете» на
# «Юго-востоке» с урезанным составом идёт 59 с, и прежняя страховка в 60 с его обрывала.
# Поэтому страховка — 120 с: на числа она не влияет, только не даёт часам сломать повторяемость.
SOLUTION_LIMIT = 800
TIME_LIMIT_SEC = 120
FAST_TIME_LIMIT_SEC = 2


@dataclass(frozen=True)
class Setup:
    """Чем этот вариант отличается от обычного расчёта.

    `mode` — ключ `optimizer.ORDERS`, то есть порядок критериев цели; у базового варианта
    он не используется, потому что солвер в нём не участвует вовсе. `guided` — вести ли
    поиск после первого решения; без поиска решение одно, и `solution_limit` не действует.
    `staged` — аварии вперёд (PLAN 6.5, блок 49); `None` — как `OBJECTIVE_STAGED`. Действует
    только в `emergency_first` с поиском.
    """

    variant: Variant
    algorithm: Algorithm
    mode: str
    time_limit: int
    solution_limit: int = SOLUTION_LIMIT
    guided: bool = True
    staged: bool | None = None


SETUPS: dict[Variant, Setup] = {
    Variant.BASELINE: Setup(
        Variant.BASELINE, Algorithm.BASELINE, "count_first", TIME_LIMIT_SEC
    ),
    Variant.EMERGENCY_FIRST: Setup(
        Variant.EMERGENCY_FIRST, Algorithm.OPTIMIZED, "emergency_first", TIME_LIMIT_SEC
    ),
    # Основная модель поэтапно (PLAN 6.5, блок 49): аварии как можно раньше. Отдельной
    # строкой, а не умолчанием: на замере она теряет заявки и местами бригаду, и эта цена
    # должна быть видна рядом с основной моделью.
    Variant.EMERGENCY_ASAP: Setup(
        Variant.EMERGENCY_ASAP,
        Algorithm.OPTIMIZED,
        "emergency_first",
        TIME_LIMIT_SEC,
        staged=True,
    ),
    Variant.MAX_REQUESTS: Setup(
        Variant.MAX_REQUESTS, Algorithm.OPTIMIZED, "count_first", TIME_LIMIT_SEC
    ),
    Variant.URGENT_FIRST: Setup(
        Variant.URGENT_FIRST, Algorithm.OPTIMIZED, "urgent_first", TIME_LIMIT_SEC
    ),
    Variant.PRIORITY_FIRST: Setup(
        Variant.PRIORITY_FIRST, Algorithm.OPTIMIZED, "priority_first", TIME_LIMIT_SEC
    ),
    # Больше работы лучшим по рейтингу (блок 71): бригады набора плюс резерв, его добавляет
    # `service.staffed`.
    Variant.RATING_FIRST: Setup(
        Variant.RATING_FIRST, Algorithm.OPTIMIZED, "rating_first", TIME_LIMIT_SEC
    ),
    Variant.FAST: Setup(
        Variant.FAST,
        Algorithm.OPTIMIZED,
        "emergency_first",
        FAST_TIME_LIMIT_SEC,
        guided=False,
    ),
}
