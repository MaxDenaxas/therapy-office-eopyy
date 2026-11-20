"""Working-day calendar: weekends, Greek public holidays (incl. Orthodox Easter), overrides."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.scheduling import CLOSED, OPEN, greek_public_holidays, non_working_days, working_days


def orthodox_easter(year: int) -> date:
    """Meeus' Julian algorithm + 13 days (valid 1900–2099). Independent of the library."""
    a, b, c = year % 4, year % 7, year % 19
    d = (19 * c + 15) % 30
    e = (2 * a + 4 * b - d + 34) % 7
    month = (d + e + 114) // 31
    day = (d + e + 114) % 31 + 1
    return date(year, month, day) + timedelta(days=13)


def test_known_orthodox_easter_dates():
    assert [orthodox_easter(y) for y in (2025, 2026, 2027)] == [
        date(2025, 4, 20),
        date(2026, 4, 12),
        date(2027, 5, 2),
    ]


@pytest.mark.parametrize("year", range(2024, 2036))
def test_easter_related_holidays_match_an_independent_calculation(year):
    easter = orthodox_easter(year)
    found = greek_public_holidays(date(year, 1, 1), date(year, 12, 31))
    for offset in (-48, -2, 1, 50):  # Clean Monday, Good Friday, Easter Monday, Whit Monday
        assert easter + timedelta(days=offset) in found, (year, offset)


@pytest.mark.parametrize("year", [2025, 2026, 2027])
def test_fixed_public_holidays(year):
    found = greek_public_holidays(date(year, 1, 1), date(year, 12, 31))
    for month, day in ((1, 1), (1, 6), (3, 25), (5, 1), (8, 15), (10, 28), (12, 25), (12, 26)):
        assert date(year, month, day) in found


def test_labour_day_moved_out_of_holy_week_2027():
    """1 May 2027 is Holy Saturday; the observed Labour Day moves to Tuesday 4 May."""
    found = greek_public_holidays(date(2027, 4, 26), date(2027, 5, 7))
    assert date(2027, 5, 4) in found
    assert date(2027, 5, 4) not in working_days(date(2027, 5, 3), date(2027, 5, 7))


def test_weekends_are_never_working_days():
    days = working_days(date(2025, 11, 1), date(2025, 11, 30))
    assert days and all(d.weekday() < 5 for d in days)
    assert non_working_days(date(2025, 11, 1), date(2025, 11, 2)) == {
        date(2025, 11, 1): "weekend",
        date(2025, 11, 2): "weekend",
    }


def test_easter_week_2026():
    days = working_days(date(2026, 4, 6), date(2026, 4, 17))
    assert date(2026, 4, 10) not in days and date(2026, 4, 13) not in days  # Good Fri, Easter Mon
    reasons = non_working_days(date(2026, 4, 10), date(2026, 4, 13))
    assert reasons[date(2026, 4, 10)].startswith("public holiday")


def test_center_closure_and_open_override():
    start, end = date(2025, 10, 27), date(2025, 10, 31)
    plain = working_days(start, end)
    assert date(2025, 10, 28) not in plain  # Ochi Day
    with_overrides = working_days(
        start, end, {date(2025, 10, 28): OPEN, date(2025, 10, 30): CLOSED}
    )
    assert date(2025, 10, 28) in with_overrides and date(2025, 10, 30) not in with_overrides
    assert (
        non_working_days(start, end, {date(2025, 10, 30): CLOSED})[date(2025, 10, 30)]
        == "center closure"
    )


def test_open_override_cannot_open_a_weekend():
    assert working_days(date(2025, 11, 1), date(2025, 11, 1), {date(2025, 11, 1): OPEN}) == []


def test_unknown_override_kind_is_an_error():
    with pytest.raises(ValueError, match="unknown override kind"):
        working_days(date(2025, 11, 3), date(2025, 11, 7), {date(2025, 11, 4): "maybe"})


def test_period_spanning_new_year():
    days = working_days(date(2025, 12, 22), date(2026, 1, 9))
    for holiday in (date(2025, 12, 25), date(2025, 12, 26), date(2026, 1, 1), date(2026, 1, 6)):
        assert holiday not in days
