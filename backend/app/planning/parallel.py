"""Пул процессов для расчётов, которые не ждут друг друга (PLAN 6.12, блок 53).

Солвер занимает ядро целиком, поэтому варианты предпросмотра события считаются в отдельных
процессах: три варианта ждутся как один. Процессы запускаются `spawn`: на Windows другого
способа нет, а на Linux `fork` после создания `MongoClient` небезопасен. Запуск процесса —
это заново импорт OR-Tools и всего `app`, поэтому пул создаётся один раз на жизнь API и
гасится в `lifespan`.

В процессе — только чистые функции вроде `service.build_plan`: без Mongo и HTTP, все данные
приходят аргументами. Настройки процесс читает сам, из того же `.env`, что и API.
"""

from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from multiprocessing import get_context
from threading import Lock

from ..config import settings

_pool: ProcessPoolExecutor | None = None
# Синхронные обработчики FastAPI идут в пуле потоков: два предпросмотра сразу иначе создали бы
# два пула (один утёк бы мимо `shutdown`) или погасили бы пул под чужими задачами.
_lock = Lock()


def run(task: Callable, jobs: list[tuple]) -> list:
    """`task(*job)` для каждой задачи; результаты — в порядке задач.

    `SOLVER_WORKERS=1` или одна задача — по очереди, в своём процессе: пул ради одной задачи
    только добавил бы пересылку данных.
    """
    if settings.solver_workers <= 1 or len(jobs) <= 1:
        return [task(*job) for job in jobs]
    global _pool
    with _lock:
        if _pool is None:
            _pool = ProcessPoolExecutor(
                max_workers=settings.solver_workers, mp_context=get_context("spawn")
            )
        pool = _pool
    try:
        return [future.result() for future in [pool.submit(task, *job) for job in jobs]]
    except BrokenProcessPool:
        # Процесс умер (например, от нехватки памяти) — сломанный пул больше задач не примет.
        # Снимаем именно его, а не тот, что успел поднять соседний запрос: следующий вызов
        # поднимет новый, а этот расчёт честно падает.
        with _lock:
            if _pool is pool:
                _pool = None
        pool.shutdown(wait=False, cancel_futures=True)
        raise


def shutdown() -> None:
    global _pool
    with _lock:
        pool, _pool = _pool, None
    if pool is not None:
        pool.shutdown(cancel_futures=True)
