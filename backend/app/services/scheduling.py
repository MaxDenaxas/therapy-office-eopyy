"""Scheduling use cases on the database (M3). The caller owns the transaction: run
these inside ``app.db.session_scope`` so a failure rolls everything back.

  propose_schedule   replace earlier *proposed* dates of a draft period with a new proposal;
                     the administrator's *manual* dates are kept as they are
  replace_dates      the administrator's edit of one therapy's dates (stored as manual)
  schedule_status    planned vs authorized per therapy, rule violations, soft warnings
  approve_schedule   draft -> schedule_approved (rules re-checked against today's calendar)
  reopen_schedule    schedule_approved -> draft

A schedule is a plan (D-08). Fewer planned dates than authorized is allowed while
editing but reported as incomplete; approving it needs ``allow_incomplete=True``, which
is audited. Whether an incomplete schedule may ever be *issued* is Q-03 (decided in M7).
A therapy with zero dates can never be approved (D-24).
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import (
    AuditLog,
    CenterSettings,
    HolidayOverride,
    Period,
    PeriodTherapy,
    ReferralPeriod,
    ScheduledSession,
    TherapyType,
)
from app.scheduling import (
    BusySession,
    Conflict,
    Proposal,
    Rules,
    ScheduleWarning,
    SchedulingError,
    TherapyRequest,
    check_schedule,
    propose,
)

DRAFT, APPROVED = "draft", "schedule_approved"


class ScheduleLocked(Exception):
    """The period's schedule is approved or issued; reopen it before changing dates."""


class ScheduleInvalid(Exception):
    """The dates break a hard rule. ``violations`` lists each one."""

    def __init__(self, violations: list[Conflict]):
        self.violations = list(violations)
        super().__init__("; ".join(v.message for v in self.violations))


class ScheduleIncomplete(Exception):
    """Fewer planned dates than authorized for some therapy (``short`` lists them)."""

    def __init__(self, short: list[TherapyStatus], message: str):
        self.short = list(short)
        super().__init__(message)


@dataclass(frozen=True)
class TherapyStatus:
    period_therapy_id: int
    therapy: str
    therapist_id: int
    authorized: int
    planned: int
    dates: tuple[date, ...]

    @property
    def complete(self) -> bool:
        return self.planned == self.authorized


@dataclass(frozen=True)
class ScheduleStatus:
    period_id: int
    status: str
    therapies: tuple[TherapyStatus, ...]
    violations: tuple[Conflict, ...]
    warnings: tuple[ScheduleWarning, ...]

    @property
    def complete(self) -> bool:
        """Every therapy has exactly its authorized quantity. Not the same as 'may be issued'."""
        return all(t.complete for t in self.therapies)

    @property
    def valid(self) -> bool:
        return not self.violations


@dataclass(frozen=True)
class ProposalOutcome:
    proposal: Proposal
    status: ScheduleStatus


# ---------------------------------------------------------------- loading
def load_rules(session: Session) -> Rules:
    settings = session.get(CenterSettings, 1)
    if settings is None:
        raise RuntimeError("center settings row is missing; run the database migrations")
    return Rules(
        max_therapists_per_day=settings.max_therapists_per_day,
        max_sessions_per_therapy_per_day=settings.max_sessions_per_therapy_per_day,
        prefer_gap_days=settings.prefer_gap_days,
    )


def load_overrides(session: Session, start: date, end: date) -> dict[date, str]:
    rows = session.scalars(
        select(HolidayOverride).where(HolidayOverride.day.between(start, end))
    ).all()
    return {r.day: r.kind for r in rows}


def _period(session: Session, period_id: int) -> Period:
    period = session.get(Period, period_id)
    if period is None:
        raise LookupError(f"document period {period_id} does not exist")
    return period


def _therapies(session: Session, period: Period) -> list[tuple[PeriodTherapy, ReferralPeriod, str]]:
    rows = session.execute(
        select(PeriodTherapy, ReferralPeriod, TherapyType.code)
        .join(ReferralPeriod, ReferralPeriod.id == PeriodTherapy.referral_period_id)
        .join(TherapyType, TherapyType.id == PeriodTherapy.therapy_type_id)
        .where(PeriodTherapy.period_id == period.id)
        .order_by(PeriodTherapy.id)
    ).all()
    if not rows:
        raise SchedulingError([Conflict("INVALID_INPUT", "the document period has no therapies")])
    return [(pt, rp, code) for pt, rp, code in rows]


def _sessions(session: Session, period: Period) -> dict[int, list[ScheduledSession]]:
    rows = session.scalars(
        select(ScheduledSession)
        .join(PeriodTherapy, PeriodTherapy.id == ScheduledSession.period_therapy_id)
        .where(PeriodTherapy.period_id == period.id)
        .order_by(ScheduledSession.session_date, ScheduledSession.id)
    ).all()
    by_therapy: dict[int, list[ScheduledSession]] = defaultdict(list)
    for s in rows:
        by_therapy[s.period_therapy_id].append(s)
    return by_therapy


def _busy(session: Session, period: Period) -> list[BusySession]:
    """The patient's sessions in *other* document periods within this window (any status):
    respected as they are, never moved."""
    rows = session.execute(
        select(ScheduledSession.session_date, PeriodTherapy.therapist_id, TherapyType.code)
        .join(PeriodTherapy, PeriodTherapy.id == ScheduledSession.period_therapy_id)
        .join(Period, Period.id == PeriodTherapy.period_id)
        .join(TherapyType, TherapyType.id == PeriodTherapy.therapy_type_id)
        .where(
            Period.patient_id == period.patient_id,
            Period.id != period.id,
            ScheduledSession.session_date.between(period.start_date, period.end_date),
        )
    ).all()
    return [BusySession(day, therapist, code) for day, therapist, code in rows]


def _request(
    pt: PeriodTherapy, rp: ReferralPeriod, code: str, period: Period, fixed: Iterable[date] = ()
) -> TherapyRequest:
    return TherapyRequest(
        key=pt.id,
        therapy=code,
        therapist_id=pt.therapist_id,
        window_start=period.start_date,
        window_end=period.end_date,  # = referral window (P-22)
        quantity=rp.authorized_quantity,
        fixed_dates=tuple(sorted(fixed)),
    )


def _audit(session: Session, action: str, period: Period, **details) -> None:
    session.add(
        AuditLog(
            action=action,
            entity="periods",
            entity_id=period.id,
            details=json.dumps(details, sort_keys=True, default=str),
        )
    )


def _require_draft(period: Period) -> None:
    if period.status != DRAFT:
        raise ScheduleLocked(
            f"document period {period.id} is {period.status}; reopen it before changing dates"
        )


# ---------------------------------------------------------------- use cases
def propose_schedule(session: Session, period_id: int) -> ProposalOutcome:
    period = _period(session, period_id)
    _require_draft(period)
    therapies = _therapies(session, period)
    existing = _sessions(session, period)
    requests = [
        _request(
            pt,
            rp,
            code,
            period,
            fixed=[s.session_date for s in existing.get(pt.id, []) if s.origin == "manual"],
        )
        for pt, rp, code in therapies
    ]
    rules = load_rules(session)
    overrides = load_overrides(session, period.start_date, period.end_date)
    proposal = propose(requests, rules, busy=_busy(session, period), overrides=overrides)

    keys = [pt.id for pt, _, _ in therapies]
    session.execute(
        delete(ScheduledSession).where(
            ScheduledSession.period_therapy_id.in_(keys), ScheduledSession.origin == "proposed"
        )
    )
    created = 0
    for request in requests:
        new_days = list(proposal.dates[request.key])
        for kept in request.fixed_dates:
            new_days.remove(kept)
        for day in new_days:
            session.add(
                ScheduledSession(period_therapy_id=request.key, session_date=day, origin="proposed")
            )
            created += 1
    session.flush()
    _audit(
        session,
        "schedule.proposed",
        period,
        created=created,
        kept_manual=sum(len(r.fixed_dates) for r in requests),
        warnings=[w.code for w in proposal.warnings],
    )
    session.flush()
    return ProposalOutcome(proposal, schedule_status(session, period_id))


def replace_dates(
    session: Session, period_id: int, period_therapy_id: int, dates: Iterable[date]
) -> ScheduleStatus:
    """Store the administrator's dates for one therapy (origin 'manual'). Any hard-rule
    violation is refused; fewer dates than authorized are accepted and reported."""
    period = _period(session, period_id)
    _require_draft(period)
    dates = list(dates)
    therapies = _therapies(session, period)
    if period_therapy_id not in {pt.id for pt, _, _ in therapies}:
        raise LookupError(f"therapy {period_therapy_id} is not part of period {period_id}")
    existing = _sessions(session, period)
    requests = [_request(pt, rp, code, period) for pt, rp, code in therapies]
    schedule = {pt.id: [s.session_date for s in existing.get(pt.id, [])] for pt, _, _ in therapies}
    schedule[period_therapy_id] = dates
    violations, _ = check_schedule(
        requests,
        schedule,
        load_rules(session),
        busy=_busy(session, period),
        overrides=load_overrides(session, period.start_date, period.end_date),
    )
    if violations:
        raise ScheduleInvalid(violations)
    session.execute(
        delete(ScheduledSession).where(ScheduledSession.period_therapy_id == period_therapy_id)
    )
    session.flush()
    for day in sorted(dates):
        session.add(
            ScheduledSession(period_therapy_id=period_therapy_id, session_date=day, origin="manual")
        )
    session.flush()
    _audit(
        session, "schedule.edited", period, period_therapy_id=period_therapy_id, dates=len(dates)
    )
    session.flush()
    return schedule_status(session, period_id)


def schedule_status(session: Session, period_id: int) -> ScheduleStatus:
    period = _period(session, period_id)
    therapies = _therapies(session, period)
    existing = _sessions(session, period)
    requests = [_request(pt, rp, code, period) for pt, rp, code in therapies]
    schedule = {pt.id: [s.session_date for s in existing.get(pt.id, [])] for pt, _, _ in therapies}
    violations, warnings = check_schedule(
        requests,
        schedule,
        load_rules(session),
        busy=_busy(session, period),
        overrides=load_overrides(session, period.start_date, period.end_date),
    )
    rows = tuple(
        TherapyStatus(
            pt.id,
            code,
            pt.therapist_id,
            rp.authorized_quantity,
            len(schedule[pt.id]),
            tuple(sorted(schedule[pt.id])),
        )
        for pt, rp, code in therapies
    )
    return ScheduleStatus(period.id, period.status, rows, tuple(violations), tuple(warnings))


def approve_schedule(
    session: Session, period_id: int, *, allow_incomplete: bool = False
) -> ScheduleStatus:
    period = _period(session, period_id)
    _require_draft(period)
    status = schedule_status(session, period_id)  # re-checked against today's calendar
    if status.violations:
        raise ScheduleInvalid(list(status.violations))
    empty = [t for t in status.therapies if t.planned == 0]
    if empty:  # D-24: never, not even with allow_incomplete
        raise ScheduleIncomplete(
            empty,
            "therapies without any planned date: "
            + ", ".join(f"{t.therapy} (#{t.period_therapy_id})" for t in empty),
        )
    short = [t for t in status.therapies if not t.complete]
    if short and not allow_incomplete:
        raise ScheduleIncomplete(
            short,
            "fewer planned dates than authorized for: "
            + ", ".join(
                f"{t.therapy} (#{t.period_therapy_id}) {t.planned}/{t.authorized}" for t in short
            )
            + "; approve with allow_incomplete=True only if this is intended (see Q-03)",
        )
    period.status = APPROVED
    session.flush()
    _audit(
        session,
        "schedule.approved",
        period,
        incomplete=[
            {
                "period_therapy_id": t.period_therapy_id,
                "planned": t.planned,
                "authorized": t.authorized,
            }
            for t in short
        ],
    )
    session.flush()
    return schedule_status(session, period_id)


def reopen_schedule(session: Session, period_id: int) -> ScheduleStatus:
    period = _period(session, period_id)
    if period.status != APPROVED:
        raise ScheduleLocked(
            f"document period {period_id} is {period.status}; only an approved "
            "schedule can be reopened here (issued periods: see corrections, M7)"
        )
    period.status = DRAFT
    session.flush()
    _audit(session, "schedule.reopened", period)
    session.flush()
    return schedule_status(session, period_id)
