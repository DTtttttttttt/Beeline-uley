"""Замеры для README: таблицы между метками `<!-- measure:имя -->` (блок 46).

Запуск из backend/:
    python scripts/measure.py           — пересчитать и переписать таблицы README
    python scripts/measure.py --check   — пересчитать и упасть, если README разошёлся

Без HTTP, Mongo и Valhalla: демо-пары считаются на готовых таблицах переездов
(`data/seed/matrices.json`, `data/examples/*-matrices.json`), как и в приложении без Valhalla.
Поиск останавливает предел числа решений (PLAN 6.5), поэтому числа повторяются байт в байт.
Поиск, остановленный временем, или план, не прошедший проверку, роняют скрипт: такой замер
не повторяем, и README он не пишет. Времени расчёта в таблицах нет по той же причине.

Считается на настройках по умолчанию, а не на `.env`: правило окна, порядок критериев или
вместимость из локального файла иначе попали бы в README, и `--check` в CI с ним разошёлся бы.
Из окружения берутся только каталог данных и лимиты времени — те на числа не влияют.
"""

import argparse
import logging
import re
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime
from multiprocessing import get_context
from pathlib import Path
from statistics import mean

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings, settings
from app.data import seed
from app.dictionaries import (
    VARIANT_NAMES,
    WORK_PRIORITY_NAMES,
    DurationMode,
    WorkPriority,
)
from app.models import Algorithm, Dataset, Plan, Rating, ReplanMode, Variant
from app.planning import service, variants
from app.routing import matrices, valhalla
from app.routing.matrices import Travel

README = Path(__file__).resolve().parents[2] / "README.md"
NBSP = " "  # разделитель тысяч: число не переносится посреди
# Столбцы таблиц: базовый, обычный расчёт дня и варианты сравнения, кроме базового — он тот
# же базовый план.
PLANS = ["baseline", "optimized"] + [
    variant for variant in variants.SETUPS if variant is not Variant.BASELINE
]
NAMES = VARIANT_NAMES | {"optimized": "Оптимизация"}
# Оптимизация проиграла базовому по цели, и показан базовый план (PLAN 6.6): подмена в
# таблице видна так же, как в приложении (`fallbackToBaseline`).
FALLBACK = " ↩"
TABLES = ("pairs", "builtin", "builtin-norm")
# Что из окружения оставить: на числа не влияет. Остальное — умолчания `Settings`.
FROM_ENV = {"data_dir", "solver_time_limit_sec", "replan_time_limit_sec"}


# --- расчёт --------------------------------------------------------------------------


def _offline() -> None:
    """Настройки по умолчанию, и Valhalla замер не спрашивает: числа не должны зависеть ни от
    `.env`, ни от того, поднята ли Valhalla.

    Готовые таблицы покрывают все точки демо-пар, поэтому досчитывать нечего. Повторов нет:
    отказ здесь не случайный. Зовётся в каждом процессе: spawn читает `.env` заново.
    """
    for name, field in Settings.model_fields.items():
        if name not in FROM_ENV:
            setattr(settings, name, field.get_default(call_default_factory=True))

    def unavailable(*_):
        raise httpx.ConnectError("замер README не ходит в Valhalla")

    valhalla.matrix = unavailable
    matrices.RETRIES = 0
    logging.disable(logging.WARNING)  # «Valhalla недоступна» здесь — не новость


def _dataset(
    requests_id: str, crew_id: str, mode: DurationMode = DurationMode.COMPLEXITY
) -> Dataset:
    return Dataset(
        id=f"demo:{requests_id}:{crew_id}",
        built_in=True,
        created_at=datetime.now(UTC),
        duration_mode=mode,
        **seed.demo_raw(requests_id, crew_id),
    )


def rated(dataset: Dataset) -> Dataset:
    """Снимок рейтинга по демо-истории (`data/seed/reviews.json`), без Mongo: то же, что даёт
    `reviews.snapshot` сразу после загрузки истории при старте (блок 71)."""
    stars: dict[str, list[int]] = {}
    for item in seed.load_reviews():
        stars.setdefault(item["engineerId"], []).append(item["stars"])
    return dataset.model_copy(
        update={
            "engineers": [
                engineer.model_copy(
                    update={
                        "rating": Rating(stars=round(mean(given), 2), count=len(given))
                        if (given := stars.get(engineer.id))
                        else None
                    }
                )
                for engineer in dataset.engineers
            ]
        }
    )


def _travel(dataset: Dataset) -> Travel:
    """Готовые таблицы пары — по отпечатку её точек, как их находит кэш (PLAN 5.4)."""
    table = matrices.points(dataset)
    digest = matrices.points_hash(table)
    documents = {
        document["profile"]: document
        for document in seed.load_matrices() + seed.load_example_matrices()
        if document["pointsHash"] == digest
    }
    return Travel(
        dataset.id,
        table,
        {
            profile: matrices.from_document(documents[profile])
            for profile in matrices.profiles(dataset)
        },
    )


def _summary(plan: Plan, label: str) -> dict:
    """Числа плана для таблиц. Неповторяемый план — ошибка, а не строка таблицы.

    Неповторяем поиск, который остановили часы, — и тот, что не нашёл решения вовсе: у
    каждой заявки есть дизъюнкция, пустое решение допустимо всегда, поэтому «решения нет»
    значит одно — часы кончились раньше первого решения. Оптимизированный план без
    статистики поиска бывает законно только у встраивания: оно солвер не зовёт.
    """
    if not plan.validation.ok:
        raise RuntimeError(
            f"{label}: план не прошёл проверку — {plan.validation.errors}"
        )
    searched = (
        plan.algorithm is Algorithm.OPTIMIZED
        and plan.replan_mode is not ReplanMode.INSERT
    )
    if (plan.search is None and searched) or (
        plan.search is not None and plan.search.stopped_by != "solutions"
    ):
        raise RuntimeError(
            f"{label}: поиск упёрся в лимит времени, замер не повторяем — машина не "
            "успела; запустите с меньшим --jobs или на свободной машине"
        )
    figures = plan.metrics
    # Граница — оценка снизу (PLAN 6.10): план, которому её не хватило, значит, что неверна
    # оценка, а не план. Самые плотные планы девяти пар проверяются здесь, при каждом замере.
    if figures.engineers_lower_bound > figures.engineers_used:
        raise RuntimeError(
            f"{label}: нижняя граница {figures.engineers_lower_bound} больше "
            f"задействованных бригад ({figures.engineers_used}) — оценка неверна"
        )
    risks = [
        item.risk for item in (plan.forecast.requests if plan.forecast else {}).values()
    ]
    return {
        "total": len(plan.input.requests),
        "assigned": figures.assigned_count,
        "byType": {
            priority: figures.assigned_by_work_priority.get(priority, 0)
            for priority in WorkPriority
        },
        "engineers": figures.engineers_used,
        "km": figures.total_km,
        "travel": figures.travel_min,
        "wait": figures.wait_min,
        "work": figures.work_min,
        "emergencyWait": figures.emergency_wait_min,
        "late": figures.expected_late,
        "risk": [risks.count(level) for level in ("high", "medium", "low")],
        "fallback": plan.fallback_to_baseline,
    }


def pair(
    requests_id: str, crew_id: str, mode: DurationMode = DurationMode.COMPLEXITY
) -> dict[str, dict]:
    """Демо-пара целиком: базовый, обычный расчёт дня и варианты сравнения (PLAN 6.14).

    Демо считается с учётом сложности, как в приложении по умолчанию (PLAN 9.1); `norm` —
    тот же день по нормативу, для сравнения.
    """
    _offline()
    dataset = _dataset(requests_id, crew_id, mode)
    travel = _travel(dataset)
    label = f"{requests_id}:{crew_id}"
    plans = {
        "baseline": service.build_plan(dataset, travel, Algorithm.BASELINE),
        "optimized": service.build_plan(dataset, travel, Algorithm.OPTIMIZED),
    }
    for key in PLANS[2:]:
        setup = variants.SETUPS[key]
        # «Рейтинг в приоритете» — с резервом и рейтингом, как в приложении (блок 71); точки
        # резерва — офис, таблицы те же.
        day = rated(service.staffed(dataset, setup.mode))
        plans[key] = service.build_plan(day, travel, setup.algorithm, None, setup)
    return {key: _summary(plan, f"{label}:{key}") for key, plan in plans.items()}


def measure(jobs: int | None = None) -> dict[str, str]:
    """Все таблицы README: пары считаются параллельно, по процессу на задачу.

    С пределом решений нагрузка машины на результат не влияет, только на время; если её
    не хватило и поиск упёрся в часы, `_summary` скажет об этом и уронит замер. При первой
    ошибке остальное снимается с очереди, а не досчитывается впустую.
    """
    catalog = seed.demo_catalog()
    # Учебный состав нарочно мал: сравнивать его с остальными нечего (блок 55).
    catalog.crews = [crew for crew in catalog.crews if not crew.training]
    pool = ProcessPoolExecutor(max_workers=jobs, mp_context=get_context("spawn"))
    try:
        plain = pool.submit(pair, *seed.DEMO_DEFAULT, DurationMode.NORM)
        futures = {
            (requests.id, crew.id): pool.submit(pair, requests.id, crew.id)
            for requests in catalog.requests
            for crew in catalog.crews
        }
        pairs = {key: future.result() for key, future in futures.items()}
        norm = plain.result()
    finally:
        pool.shutdown(cancel_futures=True)
    return {
        "pairs": pairs_table(catalog, pairs),
        "builtin": builtin_table(pairs[seed.DEMO_DEFAULT]),
        "builtin-norm": builtin_table(norm),
    }


# --- таблицы -------------------------------------------------------------------------


def number(value: int) -> str:
    return f"{value:,}".replace(",", NBSP)


def decimal(value: float) -> str:
    return f"{value:,.1f}".replace(",", NBSP).replace(".", ",")


def _short(plan: dict) -> str:
    cell = f"{plan['assigned']}/{plan['total']} · {plan['engineers']} · {decimal(plan['km'])}"
    return cell + FALLBACK if plan["fallback"] else cell


def _row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def pairs_table(catalog, pairs: dict[tuple[str, str], dict]) -> str:
    lines = [
        (
            "В ячейке — назначено из всего · задействовано бригад · км; «↩» — оптимизация "
            "проиграла базовому по цели, и показан базовый план. Предел поиска — "
            f"{number(settings.solver_solution_limit)} решений на расчёт дня, "
            f"{number(variants.SOLUTION_LIMIT)} на вариант сравнения."
        ),
        "",
        _row(["Заявки", "Инженеры"] + [NAMES[key] for key in PLANS]),
        _row(["---"] * (len(PLANS) + 2)),
    ]
    for requests in catalog.requests:
        for crew in catalog.crews:
            plans = pairs[requests.id, crew.id]
            lines.append(
                _row(
                    [
                        f"{requests.name} ({requests.count})",
                        f"{crew.name} ({crew.count})",
                    ]
                    + [_short(plans[key]) for key in PLANS]
                )
            )
    return "\n".join(lines) + "\n"


def builtin_table(plans: dict[str, dict]) -> str:
    types = [WORK_PRIORITY_NAMES[priority] for priority in WorkPriority]
    lines = [
        _row(
            ["План", "Назначено", *types, "Бригад", "Км", "Переезд, мин"]
            + ["Ожидание, мин", "Работа, мин", "Ожидание аварий, мин"]
            + ["Ожидаемые опоздания", "Риск: высокий / средний / низкий"]
        ),
        _row(["---"] * (len(types) + 10)),
    ]
    for key in PLANS:
        plan = plans[key]
        lines.append(
            _row(
                [NAMES[key] + (FALLBACK if plan["fallback"] else "")]
                + [f"{plan['assigned']}/{plan['total']}"]
                + [str(plan["byType"][priority]) for priority in WorkPriority]
                + [str(plan["engineers"]), decimal(plan["km"])]
                + [
                    number(plan[name])
                    for name in ("travel", "wait", "work", "emergencyWait")
                ]
                + [decimal(plan["late"]), " / ".join(map(str, plan["risk"]))]
            )
        )
    return "\n".join(lines) + "\n"


# --- README --------------------------------------------------------------------------


def _marks(name: str) -> re.Pattern:
    return re.compile(
        rf"(<!-- measure:{name} -->\r?\n).*?(<!-- /measure:{name} -->)", re.DOTALL
    )


def check_marks(text: str, names) -> None:
    """Метки, которой нет, — ошибка, а не тишина. Проверяется до замера: он идёт минуты."""
    missing = [name for name in names if not _marks(name).search(text)]
    if missing:
        raise KeyError(
            "в README нет меток "
            + ", ".join(
                f"<!-- measure:{name} --> … <!-- /measure:{name} -->"
                for name in missing
            )
        )


def render(text: str, tables: dict[str, str]) -> str:
    """README с новыми таблицами между метками.

    Переводы строк — как в самом файле: на Windows git отдаёт его с CRLF, и смешанные
    строки превратили бы каждый запуск в правку всего файла.
    """
    check_marks(text, tables)
    newline = "\r\n" if "\r\n" in text else "\n"
    for name, body in tables.items():
        pattern = _marks(name)
        block = body.replace("\n", newline)
        text = pattern.sub(
            lambda match, block=block: match[1] + block + match[2], text, count=1
        )
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Замеры для таблиц README")
    parser.add_argument(
        "--check",
        action="store_true",
        help="не писать README, а упасть, если он разошёлся с замером",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        help="сколько процессов считает параллельно; по умолчанию — по числу ядер. "
        "Меньше — дольше, зато поиск не упрётся в лимит времени на занятой машине",
    )
    args = parser.parse_args(argv)

    with README.open(encoding="utf-8", newline="") as file:
        old = file.read()
    check_marks(old, TABLES)
    new = render(old, measure(args.jobs))
    if args.check:
        if new != old:
            print(
                "README разошёлся с замером: python scripts/measure.py", file=sys.stderr
            )
            return 1
        print("README сходится с замером")
        return 0
    if new == old:
        print("README не изменился")
        return 0
    with README.open("w", encoding="utf-8", newline="") as file:
        file.write(new)
    print(f"записан {README.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
