"""Scheduling engine: pure functions, no database, no API (M3).

working_days / non_working_days   calendar with Greek public holidays and center overrides
propose                           planned dates for a patient's therapies
check_schedule                    hard-rule violations and soft-preference warnings of any schedule
"""

from .calendar import CLOSED, OPEN, greek_public_holidays, non_working_days, working_days
from .model import (
    BusySession,
    Conflict,
    Proposal,
    Rules,
    ScheduleWarning,
    SchedulingError,
    TherapyRequest,
)
from .rules import check_schedule
from .solver import propose

__all__ = [
    "CLOSED",
    "OPEN",
    "BusySession",
    "Conflict",
    "Proposal",
    "Rules",
    "ScheduleWarning",
    "SchedulingError",
    "TherapyRequest",
    "check_schedule",
    "greek_public_holidays",
    "non_working_days",
    "propose",
    "working_days",
]
