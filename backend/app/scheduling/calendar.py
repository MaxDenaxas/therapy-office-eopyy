"""Working days: weekdays, minus Greek public holidays, minus center closures.

Greek public holidays come from the ``holidays`` package (incl. the Orthodox Easter
dates: Clean Monday, Good Friday, Easter Monday, Whit Monday, and Labour Day moved
when it falls in Holy Week). Center overrides adjust them:
  ``closed``  an extra day off (e.g. a local feast)
  ``open``    the center works on a public holiday
Saturdays and Sundays are never working days; an ``open`` override cannot change that
(scheduling hard rule 1).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, timedelta
from functools import lru_cache

import holidays as holiday_lib

CLOSED, OPEN = "closed", "open"


@lru_cache(maxsize=64)
def _greek_holidays(year: int) -> dict[date, str]:
    return dict(holiday_lib.country_holidays("GR", years=year))


def greek_public_holidays(start: date, end: date) -> dict[date, str]:
    found: dict[date, str] = {}
    for year in range(start.year, end.year + 1):
        found.update({d: n for d, n in _greek_holidays(year).items() if start <= d <= end})
    return found


def days_between(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def non_working_days(
    start: date, end: date, overrides: Mapping[date, str] | None = None
) -> dict[date, str]:
    """Every non-working day in [start, end] with the reason, e.g. 'weekend',
    'public holiday: Μεγάλη Παρασκευή', 'center closure'."""
    overrides = overrides or {}
    for day, kind in overrides.items():
        if kind not in (CLOSED, OPEN):
            raise ValueError(f"unknown override kind {kind!r} for {day}")
    public = greek_public_holidays(start, end)
    result: dict[date, str] = {}
    for day in days_between(start, end):
        if day.weekday() >= 5:
            result[day] = "weekend"
        elif overrides.get(day) == CLOSED:
            result[day] = "center closure"
        elif day in public and overrides.get(day) != OPEN:
            result[day] = f"public holiday: {public[day]}"
    return result


def working_days(start: date, end: date, overrides: Mapping[date, str] | None = None) -> list[date]:
    closed = non_working_days(start, end, overrides)
    return [d for d in days_between(start, end) if d not in closed]
