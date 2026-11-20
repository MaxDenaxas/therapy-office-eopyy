"""Inputs, outputs and errors of the scheduling engine (pure data, no database).

The engine proposes *planned* dates (D-08). It never records attendance and never
decides whether a schedule may be issued.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any


@dataclass(frozen=True)
class Rules:
    """From center_settings; never hard-coded."""

    max_therapists_per_day: int = 2
    max_sessions_per_therapy_per_day: int = 1
    prefer_gap_days: int = 1  # preferred empty calendar days between sessions of a therapy

    def __post_init__(self) -> None:
        if self.max_therapists_per_day < 1 or self.max_sessions_per_therapy_per_day < 1:
            raise ValueError("daily limits must be at least 1")
        if self.prefer_gap_days < 0:
            raise ValueError("prefer_gap_days must not be negative")


@dataclass(frozen=True)
class TherapyRequest:
    """One therapy of one document period: what must be planned."""

    key: int  # period_therapies.id (or any unique id in tests)
    therapy: str  # therapy_types.code; "same therapy" for the daily limit
    therapist_id: int
    window_start: date  # referral window = document period window
    window_end: date
    quantity: int  # referral_periods.authorized_quantity
    fixed_dates: tuple[date, ...] = ()  # kept as they are (e.g. the administrator's manual dates)


@dataclass(frozen=True)
class BusySession:
    """A session of the same patient that is not being (re)planned: other periods, or
    anything else that must be respected as it is."""

    day: date
    therapist_id: int
    therapy: str


@dataclass(frozen=True)
class Conflict:
    """A machine-readable reason a schedule cannot be produced or accepted."""

    code: str
    message: str
    therapy_key: int | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ScheduleWarning:
    """A soft preference that could not be met (the schedule is still valid)."""

    code: str
    message: str
    therapy_key: int | None = None
    days: tuple[date, ...] = ()


@dataclass(frozen=True)
class Proposal:
    dates: dict[int, tuple[date, ...]]  # therapy key -> all planned dates (fixed + new), sorted
    warnings: tuple[ScheduleWarning, ...]
    cost: float


class SchedulingError(Exception):
    """No valid schedule exists (or the input is invalid). ``conflicts`` explains why."""

    def __init__(self, conflicts: list[Conflict]):
        self.conflicts = list(conflicts)
        super().__init__("No valid schedule: " + "; ".join(c.message for c in self.conflicts))
