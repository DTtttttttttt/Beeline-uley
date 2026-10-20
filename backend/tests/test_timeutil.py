import pytest

from app.timeutil import to_clock, to_seconds


@pytest.mark.parametrize(
    "clock, seconds",
    [("00:00", 0), ("00:01", 60), ("10:30", 37800), ("23:59", 86340)],
)
def test_round_trip(clock, seconds):
    assert to_seconds(clock) == seconds
    assert to_clock(seconds) == clock


@pytest.mark.parametrize(
    "clock", ["9:00", "09:0", "25:00", "10:60", "10-30", "", "1030"]
)
def test_bad_clock(clock):
    with pytest.raises(ValueError):
        to_seconds(clock)


@pytest.mark.parametrize("seconds", [-1, 86400])
def test_seconds_outside_day(seconds):
    with pytest.raises(ValueError):
        to_clock(seconds)


def test_seconds_inside_minute_are_dropped():
    assert to_clock(37859) == "10:30"
