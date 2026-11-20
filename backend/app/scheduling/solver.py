"""Propose planned dates for the therapies of one patient.

Hard rules are never part of the cost: a day that would break one is simply not a
candidate, and the result is re-checked by ``rules.check_schedule`` before it is
returned. Soft preferences only rank the valid candidates:

  cost = W_GAP    x (days missing to the preferred gap between sessions of a therapy)
       + W_SHARED x (other therapies of the patient already on that day)
       + W_SPREAD x (distance from evenly spaced target positions)^2

Per therapy, an exact dynamic programme picks exactly the authorized quantity of
slots (keeping fixed dates) with the lowest cost. Therapies interact only through
the daily limits, so several placement orders are tried and the cheapest complete
result wins; ties go to the first order (deterministic). If no order works, a
max-flow check either proves the daily therapist limit makes it impossible
(THERAPIST_LIMIT) or supplies a valid schedule without spacing optimisation.
"""

from __future__ import annotations

import itertools
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

from .calendar import days_between, non_working_days
from .flow import FlowGraph
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

W_GAP = 1000.0
W_SHARED = 40.0
W_SPREAD = 1.0
EXHAUSTIVE_ORDERS_UP_TO = 4  # all orderings for up to 4 therapies (24); fewer beyond that


@dataclass
class _Occupancy:
    therapists: dict[date, set[int]] = field(default_factory=lambda: defaultdict(set))
    therapy_count: Counter = field(default_factory=Counter)  # (therapy, day) -> sessions
    therapies: dict[date, Counter] = field(default_factory=lambda: defaultdict(Counter))

    def add(self, day: date, therapist_id: int, therapy: str) -> None:
        self.therapists[day].add(therapist_id)
        self.therapy_count[(therapy, day)] += 1
        self.therapies[day][therapy] += 1

    def copy(self) -> _Occupancy:
        new = _Occupancy()
        for d, s in self.therapists.items():
            new.therapists[d] = set(s)
        new.therapy_count = Counter(self.therapy_count)
        for d, c in self.therapies.items():
            new.therapies[d] = Counter(c)
        return new

    def free_slots(self, day: date, req: TherapyRequest, rules: Rules) -> int:
        """How many more sessions of ``req`` the day can take without breaking a hard rule."""
        present = self.therapists.get(day, set())
        if len(present) > rules.max_therapists_per_day:
            return 0  # already over the limit (older data): never add to such a day
        if req.therapist_id not in present and len(present) >= rules.max_therapists_per_day:
            return 0
        return max(
            0, rules.max_sessions_per_therapy_per_day - self.therapy_count[(req.therapy, day)]
        )

    def other_therapies(self, day: date, therapy: str) -> int:
        counts = self.therapies.get(day)
        return 0 if not counts else sum(1 for t, n in counts.items() if t != therapy and n > 0)


# ---------------------------------------------------------------- input checks
def _validate_input(requests: Sequence[TherapyRequest]) -> None:
    conflicts = []
    keys = [r.key for r in requests]
    for key in sorted({k for k in keys if keys.count(k) > 1}):
        conflicts.append(Conflict("INVALID_INPUT", f"therapy key {key} given more than once", key))
    for r in requests:
        label = f"{r.therapy} (#{r.key})"
        if r.window_end < r.window_start:
            conflicts.append(
                Conflict("INVALID_INPUT", f"{label}: window ends before it starts", r.key)
            )
        if r.quantity < 1:
            conflicts.append(
                Conflict(
                    "INVALID_INPUT",
                    f"{label}: authorized quantity must be at least 1",
                    r.key,
                    {"quantity": r.quantity},
                )
            )
        if len(r.fixed_dates) > max(r.quantity, 0):
            conflicts.append(
                Conflict(
                    "EXCEEDS_QUANTITY",
                    f"{label}: {len(r.fixed_dates)} kept dates but only {r.quantity} authorized",
                    r.key,
                    {"kept": len(r.fixed_dates), "authorized": r.quantity},
                )
            )
    if conflicts:
        raise SchedulingError(conflicts)


# ---------------------------------------------------------------- one therapy: exact DP
def _place(
    req: TherapyRequest, occ: _Occupancy, rules: Rules, working: list[date]
) -> tuple[list[date], float] | None:
    """Choose the new dates for ``req`` (fixed dates are kept). None if impossible."""
    candidates: list[tuple[date, bool]] = [(d, True) for d in req.fixed_dates]
    for day in working:
        candidates += [(day, False)] * occ.free_slots(day, req, rules)
    candidates.sort(key=lambda c: (c[0], not c[1]))
    n, q = len(candidates), req.quantity
    if n < q:
        return None
    span = (req.window_end - req.window_start).days + 1
    gap_needed = rules.prefer_gap_days + 1
    offsets = [(d - req.window_start).days for d, _ in candidates]
    shared = [occ.other_therapies(d, req.therapy) for d, _ in candidates]
    mand_before = [0]
    for _, mandatory in candidates:
        mand_before.append(mand_before[-1] + mandatory)
    total_mandatory = mand_before[-1]

    def pick_cost(j: int, c: int) -> float:
        target = (c + 0.5) * span / q - 0.5
        return W_SPREAD * (offsets[j] - target) ** 2 + W_SHARED * shared[j]

    inf = float("inf")
    cost = [[inf] * n for _ in range(q)]
    parent = [[-1] * n for _ in range(q)]
    for j in range(n):
        if mand_before[j] == 0:  # no mandatory candidate skipped before the first pick
            cost[0][j] = pick_cost(j, 0)
    for c in range(1, q):
        for j in range(c, n):
            best, arg = inf, -1
            for k in range(c - 1, j):
                prev = cost[c - 1][k]
                if prev == inf or mand_before[j] - mand_before[k + 1] != 0:
                    continue  # would skip a mandatory candidate between k and j
                gap = (candidates[j][0] - candidates[k][0]).days
                value = prev + W_GAP * max(0, gap_needed - gap)
                if value < best:
                    best, arg = value, k
            if arg >= 0:
                cost[c][j] = best + pick_cost(j, c)
                parent[c][j] = arg
    ends = [
        j for j in range(n) if cost[q - 1][j] < inf and total_mandatory - mand_before[j + 1] == 0
    ]
    if not ends:
        return None
    j = min(ends, key=lambda e: (cost[q - 1][e], e))
    total = cost[q - 1][j]
    picks = []
    for c in range(q - 1, -1, -1):
        picks.append(j)
        j = parent[c][j]
    new_days = [candidates[p][0] for p in sorted(picks) if not candidates[p][1]]
    return new_days, total


# ---------------------------------------------------------------- orders
def _orders(requests: list[TherapyRequest], slack: Mapping[int, int]) -> list[list[TherapyRequest]]:
    base = sorted(requests, key=lambda r: (slack[r.key], r.key))
    if len(base) <= EXHAUSTIVE_ORDERS_UP_TO:
        orders = [base] + [list(p) for p in itertools.permutations(base) if list(p) != base]
    else:
        orders = [base] + [base[i:] + base[:i] for i in range(1, len(base))] + [base[::-1]]
    return orders


# ---------------------------------------------------------------- public entry point
def propose(
    requests: Iterable[TherapyRequest],
    rules: Rules,
    *,
    busy: Iterable[BusySession] = (),
    overrides: Mapping[date, str] | None = None,
) -> Proposal:
    requests = list(requests)
    busy = list(busy)
    if not requests:
        raise SchedulingError([Conflict("INVALID_INPUT", "nothing to schedule")])
    _validate_input(requests)

    kept = {r.key: list(r.fixed_dates) for r in requests}
    kept_days = {d for r in requests for d in r.fixed_dates}
    violations, _ = check_schedule(requests, kept, rules, busy=busy, overrides=overrides)
    # overloads that already exist only in other periods are not the kept dates' fault:
    # such days are simply unavailable below
    violations = [v for v in violations if "day" not in v.details or v.details["day"] in kept_days]
    if violations:
        raise SchedulingError(
            [
                Conflict(
                    "FIXED_DATES_INVALID",
                    f"kept dates break a rule: {v.message}",
                    v.therapy_key,
                    {"violation": v.code, **v.details},
                )
                for v in violations
            ]
        )

    base = _Occupancy()
    for b in busy:
        base.add(b.day, b.therapist_id, b.therapy)
    for r in requests:
        for d in r.fixed_dates:
            base.add(d, r.therapist_id, r.therapy)

    working: dict[int, list[date]] = {}
    slack: dict[int, int] = {}
    conflicts = []
    for r in requests:
        closed = non_working_days(r.window_start, r.window_end, overrides)
        working[r.key] = [d for d in days_between(r.window_start, r.window_end) if d not in closed]
        need = r.quantity - len(r.fixed_dates)
        capacity = sum(base.free_slots(d, r, rules) for d in working[r.key])
        slack[r.key] = capacity - need
        if need > capacity:
            conflicts.append(_insufficient(r, need, capacity, working[r.key], closed, base, rules))
    if conflicts:
        raise SchedulingError(conflicts)

    best: tuple[float, dict[int, list[date]]] | None = None
    for order in _orders(requests, slack):
        occ, placed, total = base.copy(), {}, 0.0
        for r in order:
            result = _place(r, occ, rules, working[r.key])
            if result is None:
                break
            new_days, cost = result
            placed[r.key], total = new_days, total + cost
            for d in new_days:
                occ.add(d, r.therapist_id, r.therapy)
        else:
            if best is None or total < best[0] - 1e-9:
                best = (total, placed)

    extra_warnings: list[ScheduleWarning] = []
    if best is None:
        placed = _flow_fallback(requests, base, rules, working)
        extra_warnings.append(
            ScheduleWarning(
                "SPACING_NOT_OPTIMISED",
                "a valid schedule exists only with an unusual arrangement; "
                "spacing preferences were "
                "not optimised — review the dates",
            )
        )
        best = (float("nan"), placed)

    dates = {r.key: tuple(sorted([*r.fixed_dates, *best[1][r.key]])) for r in requests}
    violations, warnings = check_schedule(requests, dates, rules, busy=busy, overrides=overrides)
    planned_days = {d for ds in dates.values() for d in ds}
    # judge the proposal by the days it uses; overloads elsewhere pre-date it (other periods)
    violations = [
        v for v in violations if "day" not in v.details or v.details["day"] in planned_days
    ]
    if violations:  # never return an invalid schedule
        raise RuntimeError(f"internal error: proposal breaks hard rules: {violations}")
    return Proposal(dates=dates, warnings=tuple(extra_warnings + warnings), cost=best[0])


def _insufficient(
    r: TherapyRequest,
    need: int,
    capacity: int,
    working: list[date],
    closed: Mapping[date, str],
    occ: _Occupancy,
    rules: Rules,
) -> Conflict:
    blocked = [d for d in working if occ.free_slots(d, r, rules) == 0]
    holidays = {d: why for d, why in closed.items() if why != "weekend"}
    reason = (
        f"{r.therapy} (#{r.key}): needs {need} more session(s) between "
        f"{r.window_start:%d/%m/%Y} and {r.window_end:%d/%m/%Y}, but only {capacity} "
        f"slot(s) are available ({len(working)} working day(s)"
    )
    if holidays:
        reason += f"; {len(holidays)} holiday/closure day(s) excluded"
    if blocked:
        reason += f"; {len(blocked)} day(s) already full"
    return Conflict(
        "INSUFFICIENT_DAYS",
        reason + ")",
        r.key,
        {
            "needed": need,
            "available": capacity,
            "working_days": len(working),
            "excluded": {d.isoformat(): why for d, why in sorted(holidays.items())},
            "blocked_days": [d.isoformat() for d in blocked],
        },
    )


def _flow_fallback(
    requests: list[TherapyRequest], occ: _Occupancy, rules: Rules, working: Mapping[int, list[date]]
) -> dict[int, list[date]]:
    """Exact feasibility for one session per therapy per day; otherwise a clear error.
    Each therapy is counted as its own therapist (stricter than the rule), so a flow
    solution is always valid; when every therapy has its own therapist this is exact."""
    if rules.max_sessions_per_therapy_per_day != 1:
        raise SchedulingError(
            [
                Conflict(
                    "NO_SCHEDULE_FOUND",
                    "no schedule satisfying every hard rule was found; "
                    "reduce the sessions or check "
                    "the windows and existing dates",
                )
            ]
        )
    graph = FlowGraph()
    needs = {r.key: r.quantity - len(r.fixed_dates) for r in requests}
    day_nodes: set[date] = set()  # each day's capacity edge must be added exactly once
    for r in requests:
        graph.add("S", ("req", r.key), needs[r.key])
        for d in working[r.key]:
            if occ.free_slots(d, r, rules) == 0:
                continue
            if r.therapist_id in occ.therapists.get(d, set()):
                graph.add(("req", r.key), ("present", d, r.therapist_id), 1)
                graph.add(("present", d, r.therapist_id), "T", 10**6)
            else:
                graph.add(("req", r.key), ("day", d), 1)
                if d not in day_nodes:
                    day_nodes.add(d)
                    free = rules.max_therapists_per_day - len(occ.therapists.get(d, ()))
                    graph.add(("day", d), "T", free)
    value, flow = graph.max_flow("S", "T")
    if value == sum(needs.values()):
        placed = {r.key: [] for r in requests}
        for r in requests:
            for node, amount in flow.get(("req", r.key), {}).items():
                if amount:
                    placed[r.key].append(node[1])
        return placed
    reachable = graph.reachable_from("S")
    short = [r for r in requests if ("req", r.key) in reachable]
    therapists = {r.therapist_id for r in requests}
    exact = len(therapists) == len(requests)
    days = sorted({d for r in short for d in working[r.key]})
    slots = sum(max(0, rules.max_therapists_per_day - len(occ.therapists.get(d, ()))) for d in days)
    names = ", ".join(f"{r.therapy} (#{r.key})" for r in short)
    message = (
        f"with at most {rules.max_therapists_per_day} different therapist(s) per day, "
        f"{names} cannot all be planned: they need {sum(needs[r.key] for r in short)} "
        f"session day(s) but only {slots} therapist slot(s) are free"
        + (f" between {days[0]:%d/%m/%Y} and {days[-1]:%d/%m/%Y}" if days else "")
    )
    return_code = "THERAPIST_LIMIT" if exact else "NO_SCHEDULE_FOUND"
    raise SchedulingError(
        [
            Conflict(
                return_code,
                message,
                None,
                {
                    "therapies": [r.key for r in short],
                    "needed": sum(needs[r.key] for r in short),
                    "free_slots": slots,
                },
            )
        ]
    )
