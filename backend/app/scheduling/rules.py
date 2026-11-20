"""Check any schedule (proposed or edited by the administrator) against the rules.

Hard rules -> ``Conflict`` violations (the schedule must not be used):
  NON_WORKING_DAY            weekend, public holiday or center closure
  OUTSIDE_WINDOW             outside the therapy's referral window
  EXCEEDS_QUANTITY           more dates than the authorized quantity
  SAME_THERAPY_DAILY_LIMIT   more sessions of one therapy on a day than allowed (incl. duplicates)
  THERAPIST_DAILY_LIMIT      more distinct therapists on a day than allowed for the patient
  UNKNOWN_THERAPY            dates for a therapy that is not part of the request
Soft preferences -> ``ScheduleWarning`` (valid, but worth a look):
  SHORT_GAP                  two sessions of a therapy closer than the preferred gap
  SHARED_DAY                 several therapies of the patient on the same day
Completeness (planned vs authorized) is reported separately, never as "compliant" (Q-03).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from datetime import date
from itertools import pairwise

from .calendar import non_working_days
from .model import BusySession, Conflict, Rules, ScheduleWarning, TherapyRequest


def check_schedule(
    requests: Iterable[TherapyRequest],
    schedule: Mapping[int, Iterable[date]],
    rules: Rules,
    *,
    busy: Iterable[BusySession] = (),
    overrides: Mapping[date, str] | None = None,
) -> tuple[list[Conflict], list[ScheduleWarning]]:
    requests = {r.key: r for r in requests}
    schedule = {k: sorted(v) for k, v in schedule.items()}
    busy = list(busy)
    violations: list[Conflict] = []
    warnings: list[ScheduleWarning] = []

    for key in sorted(set(schedule) - set(requests)):
        violations.append(
            Conflict("UNKNOWN_THERAPY", f"dates given for unknown therapy {key}", key)
        )
    all_days = [d for k, ds in schedule.items() if k in requests for d in ds]
    closed = non_working_days(min(all_days), max(all_days), overrides) if all_days else {}

    therapists_by_day: dict[date, set[int]] = defaultdict(set)
    therapy_day_count: Counter[tuple[str, date]] = Counter()
    therapies_by_day: dict[date, set[str]] = defaultdict(set)
    for b in busy:
        therapists_by_day[b.day].add(b.therapist_id)
        therapy_day_count[(b.therapy, b.day)] += 1
        therapies_by_day[b.day].add(b.therapy)

    for key in sorted(k for k in schedule if k in requests):
        req, days = requests[key], schedule[key]
        label = f"{req.therapy} (#{key})"
        if len(days) > req.quantity:
            violations.append(
                Conflict(
                    "EXCEEDS_QUANTITY",
                    f"{label}: {len(days)} dates planned but only {req.quantity} authorized",
                    key,
                    {"planned": len(days), "authorized": req.quantity},
                )
            )
        for day in days:
            if not req.window_start <= day <= req.window_end:
                violations.append(
                    Conflict(
                        "OUTSIDE_WINDOW",
                        f"{label}: {day:%d/%m/%Y} is outside the referral window "
                        f"{req.window_start:%d/%m/%Y}–{req.window_end:%d/%m/%Y}",
                        key,
                        {"day": day},
                    )
                )
            if day in closed:
                violations.append(
                    Conflict(
                        "NON_WORKING_DAY",
                        f"{label}: {day:%d/%m/%Y} is not a working day ({closed[day]})",
                        key,
                        {"day": day, "reason": closed[day]},
                    )
                )
            therapists_by_day[day].add(req.therapist_id)
            therapy_day_count[(req.therapy, day)] += 1
            therapies_by_day[day].add(req.therapy)
        gap_needed = rules.prefer_gap_days + 1
        short = [(a, b) for a, b in pairwise(days) if (b - a).days < gap_needed]
        if short:
            warnings.append(
                ScheduleWarning(
                    "SHORT_GAP",
                    f"{label}: {len(short)} pair(s) of sessions closer than {gap_needed} day(s)",
                    key,
                    tuple(sorted({d for pair in short for d in pair})),
                )
            )

    for (therapy, day), count in sorted(therapy_day_count.items()):
        if count > rules.max_sessions_per_therapy_per_day:
            keys = [
                k
                for k, r in requests.items()
                if r.therapy == therapy and day in schedule.get(k, ())
            ]
            violations.append(
                Conflict(
                    "SAME_THERAPY_DAILY_LIMIT",
                    f"{therapy}: {count} sessions on {day:%d/%m/%Y}, at most "
                    f"{rules.max_sessions_per_therapy_per_day} allowed",
                    keys[0] if keys else None,
                    {"day": day, "count": count},
                )
            )
    for day, therapists in sorted(therapists_by_day.items()):
        if len(therapists) > rules.max_therapists_per_day:
            violations.append(
                Conflict(
                    "THERAPIST_DAILY_LIMIT",
                    f"{day:%d/%m/%Y}: {len(therapists)} different therapists, at most "
                    f"{rules.max_therapists_per_day} allowed per day",
                    None,
                    {"day": day, "therapists": sorted(therapists)},
                )
            )
    shared = sorted(
        d
        for d, t in therapies_by_day.items()
        if len(t) > 1 and any(d in schedule.get(k, ()) for k in requests)
    )
    if shared:
        warnings.append(
            ScheduleWarning(
                "SHARED_DAY",
                f"{len(shared)} day(s) with more than one therapy",
                None,
                tuple(shared),
            )
        )
    return violations, warnings
