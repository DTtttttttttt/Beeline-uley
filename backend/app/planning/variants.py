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

# Лимит на вариант (PLAN 6.14). Быстрый вариант показывает, во что обходится отказ от
# поиска: одно первое решение (`LOCAL_CHEAPEST_INSERTION`), лимит ему нужен только как
# страховка.
TIME_LIMIT_SEC = 10
FAST_TIME_LIMIT_SEC = 2


@dataclass(frozen=True)
class Setup:
    """Чем этот вариант отличается от обычного расчёта.

    `mode` — ключ `optimizer.ORDERS`, то есть порядок критериев цели; у базового варианта
    он не используется, потому что солвер в нём не участвует вовсе. `guided` — вести ли
    поиск после первого решения.
    """

    variant: Variant
    algorithm: Algorithm
    mode: str
    time_limit: int
    guided: bool = True


SETUPS: dict[Variant, Setup] = {
    Variant.BASELINE: Setup(
        Variant.BASELINE, Algorithm.BASELINE, "count_first", TIME_LIMIT_SEC
    ),
    Variant.EMERGENCY_FIRST: Setup(
        Variant.EMERGENCY_FIRST, Algorithm.OPTIMIZED, "emergency_first", TIME_LIMIT_SEC
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
    Variant.FAST: Setup(
        Variant.FAST,
        Algorithm.OPTIMIZED,
        "emergency_first",
        FAST_TIME_LIMIT_SEC,
        guided=False,
    ),
}
