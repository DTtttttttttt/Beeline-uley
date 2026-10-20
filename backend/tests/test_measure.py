"""Скрипт замеров README (блок 46): метки, `--check`, формат чисел.

Полный замер — минуты, и в pytest он не идёт: его повторяемость проверяет сам `--check`
на настоящем README. Здесь — то, от чего зависит, что README пишется правильно.
"""

import importlib.util
import logging
from pathlib import Path

import pytest

from app.models import Metrics

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "measure.py"
spec = importlib.util.spec_from_file_location("measure", SCRIPT)
measure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(measure)

README = (
    "# Заголовок\r\n"
    "\r\n"
    "<!-- measure:pairs -->\r\n"
    "старая таблица\r\n"
    "<!-- /measure:pairs -->\r\n"
    "\r\n"
    "Текст после.\r\n"
)


def test_table_is_replaced_between_its_marks_only():
    """Всё вне меток остаётся как было, переводы строк — как в файле (CRLF на Windows)."""
    text = measure.render(README, {"pairs": "| a | b |\n| 1 | 2 |\n"})

    assert text == README.replace("старая таблица\r\n", "| a | b |\r\n| 1 | 2 |\r\n")
    assert "\r\n" in text and "\n" not in text.replace("\r\n", "")


def test_missing_mark_is_an_error():
    """Таблица без места в README не пропадает молча."""
    with pytest.raises(KeyError, match="measure:builtin"):
        measure.render(README, {"builtin": "| x |\n"})


@pytest.fixture
def readme(tmp_path, monkeypatch):
    """README во временном каталоге и замер, который возвращает готовую таблицу."""
    path = tmp_path / "README.md"
    path.write_bytes(README.encode())
    monkeypatch.setattr(measure, "README", path)
    monkeypatch.setattr(measure, "TABLES", ("pairs",))
    monkeypatch.setattr(measure, "measure", lambda jobs: {"pairs": "| новая |\n"})
    return path


def test_check_fails_when_readme_differs_and_does_not_write(readme):
    assert measure.main(["--check"]) == 1
    assert readme.read_bytes() == README.encode()


def test_write_then_check_agrees(readme):
    """Записанное скриптом `--check` принимает: повтор даёт тот же файл байт в байт."""
    assert measure.main([]) == 0
    written = readme.read_bytes()

    assert measure.main(["--check"]) == 0
    assert measure.main([]) == 0
    assert readme.read_bytes() == written


def test_numbers_use_comma_and_unbreakable_thousands():
    assert measure.decimal(1001.7) == "1 001,7"
    assert measure.decimal(183.66) == "183,7"
    assert measure.number(2086) == "2 086"


def test_unrepeatable_plan_breaks_the_measurement():
    """Поиск, остановленный часами, в README не попадает: такой замер не повторяем."""

    class Search:
        stopped_by = "time"

    class Validation:
        ok = True

    class Plan:
        validation = Validation()
        search = Search()
        algorithm = measure.Algorithm.OPTIMIZED
        replan_mode = None

    with pytest.raises(RuntimeError, match="лимит времени"):
        measure._summary(Plan(), "восток")

    # Поиск, не нашедший решения вовсе, — тоже часы: пустое решение допустимо всегда.
    Plan.search = None
    with pytest.raises(RuntimeError, match="лимит времени"):
        measure._summary(Plan(), "восток")


def test_bound_above_the_real_plan_breaks_the_measurement():
    """Граница больше факта — ошибка оценки (блок 50): README с ней не пишется."""

    class Validation:
        ok = True

    class Plan:
        validation = Validation()
        search = None
        algorithm = measure.Algorithm.BASELINE
        replan_mode = None
        metrics = Metrics(engineers_used=3, engineers_lower_bound=4)

    with pytest.raises(RuntimeError, match="оценка неверна"):
        measure._summary(Plan(), "восток")


def test_missing_mark_fails_before_the_measurement(readme, monkeypatch):
    """Метки нет — ошибка сразу, а не после минут счёта."""
    monkeypatch.setattr(measure, "TABLES", ("pairs", "builtin"))

    def measured(jobs):
        raise AssertionError("замер не должен начинаться")

    monkeypatch.setattr(measure, "measure", measured)
    with pytest.raises(KeyError, match="measure:builtin"):
        measure.main(["--check"])


@pytest.fixture
def isolated(monkeypatch):
    """`_offline` правит общие настройки, Valhalla и логи: после теста всё как было."""
    for name in measure.Settings.model_fields:
        monkeypatch.setattr(measure.settings, name, getattr(measure.settings, name))
    monkeypatch.setattr(measure.valhalla, "matrix", measure.valhalla.matrix)
    monkeypatch.setattr(measure.matrices, "RETRIES", measure.matrices.RETRIES)
    yield
    logging.disable(logging.NOTSET)


def test_measurement_ignores_the_local_env(isolated):
    """Числа README не зависят от `.env`: правило окна и вместимость — по умолчанию."""
    measure.settings.window_rule = "fit_in_window"
    measure.settings.equipment_capacity = {}
    measure.settings.solver_time_limit_sec = 99

    measure._offline()

    assert measure.settings.window_rule == "start_in_window"
    default = measure.Settings.model_fields["equipment_capacity"]
    assert measure.settings.equipment_capacity == default.get_default(
        call_default_factory=True
    )
    assert measure.settings.solver_time_limit_sec == 99  # время на числа не влияет
