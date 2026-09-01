"""Filters that shape numbers into text in templates."""
import pytest

from app.templating import _duration

HOUR = 3600
DAY = 86400


@pytest.mark.parametrize("seconds,expected", [
    (0, "0h"),
    (59, "0h"),
    (90 * 60, "1h"),
    (7 * HOUR, "7h"),
    (23 * HOUR, "23h"),
    (DAY, "1d"),
    (9 * DAY, "9d"),
    (89 * DAY, "89d"),
    (90 * DAY, "3mo"),
    (200 * DAY, "6mo"),
    (364 * DAY, "11mo"),
    (365 * DAY, "1y"),
    (800 * DAY, "2y"),
    (3650 * DAY, "10y"),
])
def test_duration_stays_short_enough_for_a_table_column(seconds, expected):
    assert _duration(seconds) == expected


def test_duration_of_nothing_is_empty_not_zero():
    assert _duration(None) == ""


def test_duration_of_a_future_timestamp_does_not_go_negative():
    """Publishers do post-date things; a feed cannot be silent for -3 days."""
    assert _duration(-500) == "0h"


def test_duration_survives_a_value_it_cannot_read():
    assert _duration("not a number") == ""


def test_duration_never_says_twelve_months():
    """That is a year, and the year branch says it better."""
    assert "12mo" not in {_duration(d * DAY) for d in range(90, 365)}
