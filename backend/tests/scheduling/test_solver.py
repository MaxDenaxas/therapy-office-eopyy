"""Scheduling engine: hard rules never violated, soft preferences applied when feasible."""

from __future__ import annotations

import random
from datetime import date, timedelta
from itertools import pairwise

import pytest

from app.scheduling import (
    CLOSED,
    OPEN,
    BusySession,
    Rules,
    SchedulingError,
    TherapyRequest,
    check_schedule,
    propose,
    working_days,
)

OCT = (date(2025, 10, 16), date(2025, 11, 15))  # 21 working days (28/10 is Ochi Day)
RULES = Rules()


def req(key, therapy, therapist, quantity, window=OCT, fixed=()):
    return TherapyRequest(key, therapy, therapist, window[0], window[1], quantity, tuple(fixed))


def assert_valid(requests, proposal, rules=RULES, busy=(), overrides=None):
    violations, _ = check_schedule(requests, proposal.dates, rules, busy=busy, overrides=overrides)
    assert violations == []
    for r in requests:
        assert len(proposal.dates[r.key]) == r.quantity
        assert set(r.fixed_dates) <= set(proposal.dates[r.key])


def codes(error: SchedulingError) -> list[str]:
    return [c.code for c in error.conflicts]


# ---------------------------------------------------------------- hard rules
def test_exact_quantity_on_working_days_inside_the_window():
    requests = [req(1, "speech", 10, 8)]
    p = propose(requests, RULES)
    assert_valid(requests, p)
    assert all(d.weekday() < 5 and OCT[0] <= d <= OCT[1] for d in p.dates[1])
    assert date(2025, 10, 28) not in p.dates[1]


def test_easter_holidays_are_avoided():
    window = (date(2026, 4, 6), date(2026, 4, 17))  # 8 working days: Good Fri + Easter Mon off
    requests = [req(1, "speech", 10, 8, window)]
    p = propose(requests, RULES)
    assert_valid(requests, p)
    assert not {date(2026, 4, 10), date(2026, 4, 13)} & set(p.dates[1])


def test_center_closures_and_open_days_are_respected():
    window = (date(2025, 10, 27), date(2025, 10, 31))
    overrides = {date(2025, 10, 29): CLOSED, date(2025, 10, 28): OPEN}
    requests = [req(1, "speech", 10, 4, window)]
    p = propose(requests, RULES, overrides=overrides)
    assert p.dates[1] == (
        date(2025, 10, 27),
        date(2025, 10, 28),
        date(2025, 10, 30),
        date(2025, 10, 31),
    )
    assert_valid(requests, p, overrides=overrides)


def test_never_more_distinct_therapists_per_day_than_allowed():
    requests = [
        req(1, "speech", 10, 12),
        req(2, "psychotherapy", 11, 12),
        req(3, "occupational", 12, 12),
    ]
    p = propose(requests, RULES)
    assert_valid(requests, p)
    by_day = {}
    for r in requests:
        for d in p.dates[r.key]:
            by_day.setdefault(d, set()).add(r.therapist_id)
    assert max(len(t) for t in by_day.values()) <= 2


def test_same_therapist_for_two_therapies_counts_once():
    window = (date(2025, 11, 3), date(2025, 11, 7))  # 5 working days
    shared = [
        req(1, "speech", 10, 5, window),
        req(2, "special_education", 10, 5, window),
        req(3, "occupational", 11, 5, window),
    ]
    p = propose(shared, RULES)  # every day: therapists 10 and 11 only
    assert_valid(shared, p)
    distinct = [
        req(1, "speech", 10, 5, window),
        req(2, "special_education", 12, 5, window),
        req(3, "occupational", 11, 5, window),
    ]
    with pytest.raises(SchedulingError) as info:
        propose(distinct, RULES)
    assert codes(info.value) == ["THERAPIST_LIMIT"]
    assert "at most 2 different therapist" in info.value.conflicts[0].message


def test_configured_therapist_limit_is_used():
    window = (date(2025, 11, 3), date(2025, 11, 7))
    requests = [req(k, f"t{k}", 10 + k, 5, window) for k in (1, 2, 3)]
    assert_valid(
        requests,
        propose(requests, Rules(max_therapists_per_day=3)),
        Rules(max_therapists_per_day=3),
    )
    with pytest.raises(SchedulingError):
        propose(requests, Rules(max_therapists_per_day=2))


def test_one_session_per_therapy_per_day_and_configurable():
    window = (date(2025, 11, 3), date(2025, 11, 6))  # 4 working days
    requests = [req(1, "speech", 10, 8, window)]
    with pytest.raises(SchedulingError) as info:
        propose(requests, RULES)
    assert codes(info.value) == ["INSUFFICIENT_DAYS"]
    two = Rules(max_sessions_per_therapy_per_day=2)
    p = propose(requests, two)
    assert_valid(requests, p, two)
    assert all(p.dates[1].count(d) == 2 for d in set(p.dates[1]))


def test_no_duplicates_with_the_default_limit():
    requests = [req(1, "speech", 10, 21)]  # every working day once
    p = propose(requests, RULES)
    assert len(set(p.dates[1])) == 21 == len(p.dates[1])


def test_existing_sessions_of_other_periods_are_respected():
    day = date(2025, 10, 20)
    busy = [
        BusySession(day, 20, "occupational"),
        BusySession(day, 21, "psychotherapy"),
        BusySession(date(2025, 10, 21), 30, "speech"),
    ]
    requests = [req(1, "speech", 10, 19)]
    p = propose(requests, RULES, busy=busy)
    assert day not in p.dates[1]  # two other therapists already that day
    assert date(2025, 10, 21) not in p.dates[1]  # speech already planned that day
    assert_valid(requests, p, busy=busy)


def test_days_already_over_the_limit_are_never_used():
    day = date(2025, 10, 20)  # older data: three therapists, one of them ours
    busy = [
        BusySession(day, 10, "special_education"),
        BusySession(day, 20, "occupational"),
        BusySession(day, 21, "psychotherapy"),
    ]
    requests = [req(1, "speech", 10, 21 - 1)]
    p = propose(requests, RULES, busy=busy)
    assert day not in p.dates[1]


def test_fixed_dates_are_kept_and_validated():
    fixed = (date(2025, 10, 16), date(2025, 10, 17))  # consecutive on purpose
    requests = [req(1, "speech", 10, 6, fixed=fixed)]
    p = propose(requests, RULES)
    assert set(fixed) <= set(p.dates[1]) and len(p.dates[1]) == 6
    assert_valid(requests, p)
    with pytest.raises(SchedulingError) as info:
        propose([req(1, "speech", 10, 3, fixed=(date(2025, 10, 18),))], RULES)  # a Saturday
    assert codes(info.value) == ["FIXED_DATES_INVALID"]
    with pytest.raises(SchedulingError) as info:
        propose([req(1, "speech", 10, 1, fixed=fixed)], RULES)
    assert codes(info.value) == ["EXCEEDS_QUANTITY"]


def test_boundary_dates():
    single = (date(2025, 11, 3), date(2025, 11, 3))
    assert propose([req(1, "speech", 10, 1, single)], RULES).dates[1] == (date(2025, 11, 3),)
    tight = (date(2025, 11, 3), date(2025, 11, 7))
    p = propose([req(1, "speech", 10, 5, tight)], RULES)
    assert p.dates[1][0] == tight[0] and p.dates[1][-1] == tight[1]


def test_weekend_only_window():
    with pytest.raises(SchedulingError) as info:
        propose([req(1, "speech", 10, 1, (date(2025, 11, 1), date(2025, 11, 2)))], RULES)
    assert codes(info.value) == ["INSUFFICIENT_DAYS"]
    assert info.value.conflicts[0].details["working_days"] == 0


def test_insufficient_days_explains_holidays():
    window = (date(2025, 12, 22), date(2026, 1, 2))
    with pytest.raises(SchedulingError) as info:
        propose([req(1, "speech", 10, 8, window)], RULES)
    conflict = info.value.conflicts[0]
    assert conflict.code == "INSUFFICIENT_DAYS" and conflict.therapy_key == 1
    assert {"2025-12-25", "2025-12-26", "2026-01-01"} <= set(conflict.details["excluded"])
    assert "holiday/closure" in conflict.message


def test_all_conflicts_are_reported_together():
    window = (date(2025, 11, 3), date(2025, 11, 4))
    with pytest.raises(SchedulingError) as info:
        propose([req(1, "speech", 10, 5, window), req(2, "psychotherapy", 11, 5, window)], RULES)
    assert codes(info.value) == ["INSUFFICIENT_DAYS", "INSUFFICIENT_DAYS"]


def test_different_referral_windows():
    a = req(1, "speech", 10, 8, (date(2025, 10, 16), date(2025, 11, 15)))
    b = req(2, "occupational", 11, 4, (date(2025, 11, 1), date(2025, 11, 30)))
    p = propose([a, b], RULES)
    assert_valid([a, b], p)
    assert all(date(2025, 11, 1) <= d <= date(2025, 11, 30) for d in p.dates[2])


# ---------------------------------------------------------------- soft preferences
def test_gap_preference_applied_when_feasible():
    p = propose([req(1, "speech", 10, 10)], RULES)  # 10 of 21 working days
    days = p.dates[1]
    assert all((b - a).days >= 2 for a, b in pairwise(days))
    assert not [w for w in p.warnings if w.code == "SHORT_GAP"]


def test_gap_preference_relaxed_only_when_necessary_and_reported():
    window = (date(2025, 11, 3), date(2025, 11, 7))
    p = propose([req(1, "speech", 10, 5, window)], RULES)  # every day: unavoidable
    assert [w.code for w in p.warnings] == ["SHORT_GAP"]


def test_even_distribution():
    window = (date(2025, 11, 3), date(2025, 11, 28))  # four full weeks
    p = propose([req(1, "speech", 10, 4, window)], RULES)
    weeks = sorted({d.isocalendar().week for d in p.dates[1]})
    assert len(weeks) == 4  # one session per week
    gaps = [(b - a).days for a, b in pairwise(p.dates[1])]
    assert max(gaps) - min(gaps) <= 3


def test_therapies_are_spread_over_different_days_when_possible():
    requests = [
        req(1, "speech", 10, 8),
        req(2, "psychotherapy", 11, 6),
        req(3, "occupational", 12, 6),
    ]
    p = propose(requests, RULES)  # 20 sessions, 21 working days
    days = [d for r in requests for d in p.dates[r.key]]
    assert len(days) == len(set(days))
    assert not [w for w in p.warnings if w.code == "SHARED_DAY"]


def test_sharing_reported_when_unavoidable():
    requests = [req(1, "speech", 10, 12), req(2, "psychotherapy", 11, 12)]  # 24 > 21 days
    p = propose(requests, RULES)
    assert_valid(requests, p)
    shared = [w for w in p.warnings if w.code == "SHARED_DAY"]
    assert shared and len(shared[0].days) >= 3  # at least 24 - 21 days must be shared
    # the gap preference ranks above avoiding shared days, so it still holds
    assert not [w for w in p.warnings if w.code == "SHORT_GAP"]


# ---------------------------------------------------------------- determinism / input errors
def test_identical_input_gives_identical_output():
    requests = [
        req(1, "speech", 10, 8),
        req(2, "psychotherapy", 11, 6),
        req(3, "occupational", 12, 8),
    ]
    first = propose(requests, RULES)
    assert all(propose(requests, RULES) == first for _ in range(3))
    assert propose(list(reversed(requests)), RULES).dates == first.dates


@pytest.mark.parametrize(
    "requests, code",
    [
        ([], "INVALID_INPUT"),
        ([req(1, "speech", 10, 0)], "INVALID_INPUT"),
        ([req(1, "speech", 10, 3, (OCT[1], OCT[0]))], "INVALID_INPUT"),
        ([req(1, "speech", 10, 3), req(1, "psychotherapy", 11, 3)], "INVALID_INPUT"),
    ],
)
def test_invalid_input(requests, code):
    with pytest.raises(SchedulingError) as info:
        propose(requests, RULES)
    assert codes(info.value)[0] == code


def test_invalid_rules():
    with pytest.raises(ValueError):
        Rules(max_therapists_per_day=0)
    with pytest.raises(ValueError):
        Rules(prefer_gap_days=-1)


def test_flow_fallback_still_returns_a_valid_schedule(monkeypatch):
    import app.scheduling.solver as solver

    monkeypatch.setattr(solver, "_place", lambda *a, **k: None)  # every ordering 'fails'
    requests = [req(1, "speech", 10, 8), req(2, "psychotherapy", 11, 6)]
    p = propose(requests, RULES)
    assert_valid(requests, p)
    assert p.warnings[0].code == "SPACING_NOT_OPTIMISED"


# ---------------------------------------------------------------- stress: hard rules hold
ALLOWED_ERRORS = {"INSUFFICIENT_DAYS", "THERAPIST_LIMIT", "NO_SCHEDULE_FOUND"}


@pytest.mark.parametrize("seed", range(150))
def test_random_instances_never_break_hard_rules(seed):
    rng = random.Random(seed)
    start = date(2025, 1, 1) + timedelta(days=rng.randrange(0, 900))
    rules = Rules(
        max_therapists_per_day=rng.choice([1, 2, 2, 3]),
        max_sessions_per_therapy_per_day=rng.choice([1, 1, 1, 2]),
        prefer_gap_days=rng.choice([0, 1, 1, 2]),
    )
    requests = []
    for key in range(1, rng.randint(1, 4) + 1):
        offset = rng.randrange(0, 10)
        length = rng.choice([3, 7, 14, 30, 31])
        window = (start + timedelta(days=offset), start + timedelta(days=offset + length - 1))
        requests.append(
            req(key, f"therapy{key}", rng.choice([10, 11, 12, 13]), rng.randint(1, 12), window)
        )
    busy = [
        BusySession(
            start + timedelta(days=rng.randrange(0, 40)),
            rng.choice([20, 21]),
            rng.choice(["other", "therapy1"]),
        )
        for _ in range(rng.randint(0, 4))
    ]
    overrides = {
        start + timedelta(days=rng.randrange(0, 40)): CLOSED for _ in range(rng.randint(0, 2))
    }
    # existing data may itself be over the limit; such days are simply unavailable
    try:
        p = propose(requests, rules, busy=busy, overrides=overrides)
    except SchedulingError as e:
        assert set(codes(e)) <= ALLOWED_ERRORS, e.conflicts
        if "INSUFFICIENT_DAYS" in codes(e):
            c = e.conflicts[0]
            assert c.details["needed"] > c.details["available"]
        return
    for r in requests:
        assert len(p.dates[r.key]) == r.quantity
        assert all(
            d in working_days(r.window_start, r.window_end, overrides) for d in p.dates[r.key]
        )
    new_busy_violations, _ = check_schedule(
        requests, p.dates, rules, busy=busy, overrides=overrides
    )
    # only pre-existing overloads in 'busy' may be reported, never days the engine chose
    chosen = {d for ds in p.dates.values() for d in ds}
    assert all(v.details.get("day") not in chosen for v in new_busy_violations)
