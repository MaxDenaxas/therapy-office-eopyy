"""Scheduling services on the database, and the database guards of migration 0002."""

from __future__ import annotations

import json
from datetime import date

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.models import AuditLog, CenterSettings, HolidayOverride, ScheduledSession
from app.scheduling import SchedulingError
from app.services.scheduling import (
    ScheduleIncomplete,
    ScheduleInvalid,
    ScheduleLocked,
    approve_schedule,
    propose_schedule,
    reopen_schedule,
    replace_dates,
    schedule_status,
)
from tests.db import factories as f

D = date
OCT = (D(2025, 10, 16), D(2025, 11, 15))  # 21 working days (28/10 is a holiday)


def setup(
    session,
    spec=(("speech", "Λογοθεραπεία:", 8, 0), ("psychotherapy", "Ψυχοθεραπεία:", 6, 1)),
    window=OCT,
    amka="11111111111",
):
    """spec: (code, label, authorized quantity, therapist index)."""
    pat = f.patient(session, amka=amka)
    therapists = [f.therapist(session, f"Θεραπευτής {i}") for i in range(3)]
    rx = f.prescription(session, pat, D(2025, 9, 16), D(2026, 3, 15))
    period = f.period(session, pat, *window)
    pts = []
    for code, label, qty, t in spec:
        tt = f.therapy_type(session, f"{code}-{amka}", label)
        ref = f.referral(session, f.item(session, rx, tt, qty), 1, *window, qty)
        pts.append(f.period_therapy(session, period, ref, therapists[t]))
    session.commit()
    return pat, period, pts, therapists


def stored(session, period_therapy_id):
    return session.scalars(
        select(ScheduledSession)
        .where(ScheduledSession.period_therapy_id == period_therapy_id)
        .order_by(ScheduledSession.session_date)
    ).all()


# ---------------------------------------------------------------- propose
def test_propose_stores_exactly_the_authorized_quantity(session):
    _, period, pts, _ = setup(session)
    outcome = propose_schedule(session, period.id)
    session.commit()
    status = schedule_status(session, period.id)
    assert status.valid and status.complete
    assert [len(stored(session, pt.id)) for pt in pts] == [8, 6]
    assert all(s.origin == "proposed" for pt in pts for s in stored(session, pt.id))
    assert outcome.status.complete
    assert D(2025, 10, 28) not in {s.session_date for pt in pts for s in stored(session, pt.id)}


def test_propose_audits_without_personal_data(session):
    _, period, _, _ = setup(session)
    propose_schedule(session, period.id)
    session.commit()
    entry = session.scalars(select(AuditLog).where(AuditLog.action == "schedule.proposed")).one()
    details = json.loads(entry.details)
    assert entry.entity_id == period.id and details["created"] == 14
    assert "ΓΕΩΡΓΙΟΣ" not in entry.details and "11111111111" not in entry.details


def test_propose_again_replaces_proposals_and_keeps_manual_dates(session):
    _, period, pts, _ = setup(session)
    propose_schedule(session, period.id)
    manual = [D(2025, 10, 16), D(2025, 10, 20)]
    replace_dates(session, period.id, pts[0].id, manual)
    session.commit()
    propose_schedule(session, period.id)
    session.commit()
    rows = stored(session, pts[0].id)
    assert len(rows) == 8 and len({r.session_date for r in rows}) == 8  # no duplicates
    assert {r.session_date for r in rows if r.origin == "manual"} == set(manual)
    assert len(stored(session, pts[1].id)) == 6


def test_propose_uses_center_settings_and_closures(session):
    _, period, pts, _ = setup(session)
    session.add(HolidayOverride(day=D(2025, 10, 20), kind="closed", name="Τοπική εορτή"))
    session.get(CenterSettings, 1).max_therapists_per_day = 1
    session.commit()
    propose_schedule(session, period.id)
    session.commit()
    speech = {s.session_date for s in stored(session, pts[0].id)}
    psycho = {s.session_date for s in stored(session, pts[1].id)}
    assert D(2025, 10, 20) not in speech | psycho
    assert not speech & psycho  # one therapist per day


def test_other_periods_of_the_patient_are_respected(session):
    pat, period, pts, therapists = setup(session)
    # a second, overlapping document period of the same patient (different window, P-22)
    tt = f.therapy_type(session, "occupational", "Εργοθεραπεία:")
    rx2 = f.prescription(session, pat, D(2025, 11, 1), D(2026, 1, 31))
    ref = f.referral(session, f.item(session, rx2, tt, 4), 1, D(2025, 11, 1), D(2025, 11, 30), 4)
    other = f.period(session, pat, D(2025, 11, 1), D(2025, 11, 30))
    ot = f.period_therapy(session, other, ref, therapists[2])
    session.commit()
    busy_days = [D(2025, 11, 3), D(2025, 11, 4), D(2025, 11, 5), D(2025, 11, 6)]
    replace_dates(session, other.id, ot.id, busy_days)
    session.commit()
    propose_schedule(session, period.id)
    session.commit()
    for day in busy_days:
        here = [s for pt in pts for s in stored(session, pt.id) if s.session_date == day]
        assert len(here) <= 1  # at most one more therapist joins therapist 3 that day
    assert [s.session_date for s in stored(session, ot.id)] == busy_days  # never moved


def test_failed_proposal_changes_nothing(session):
    _, period, pts, _ = setup(session, spec=(("speech", "Λογοθεραπεία:", 22, 0),))
    with pytest.raises(SchedulingError) as info:
        propose_schedule(session, period.id)
    session.rollback()
    assert info.value.conflicts[0].code == "INSUFFICIENT_DAYS"
    assert stored(session, pts[0].id) == []


# ---------------------------------------------------------------- manual edits
def test_manual_edit_is_validated(session):
    _, period, pts, _ = setup(session)
    propose_schedule(session, period.id)
    session.commit()
    cases = [
        ([D(2025, 10, 18)], "NON_WORKING_DAY"),  # Saturday
        ([D(2025, 10, 28)], "NON_WORKING_DAY"),  # Ochi Day
        ([D(2025, 11, 17)], "OUTSIDE_WINDOW"),
        ([D(2025, 10, 16), D(2025, 10, 16)], "SAME_THERAPY_DAILY_LIMIT"),  # duplicate
        ([D(2025, 10, d) for d in (16, 17, 20, 21, 22, 23, 24, 27, 29)], "EXCEEDS_QUANTITY"),
    ]
    for dates, code in cases:
        with pytest.raises(ScheduleInvalid) as info:
            replace_dates(session, period.id, pts[0].id, dates)
        session.rollback()
        assert code in [v.code for v in info.value.violations]
    assert len(stored(session, pts[0].id)) == 8  # untouched


def test_manual_edit_respects_the_therapist_limit(session):
    _, period, pts, _ = setup(
        session,
        spec=(
            ("speech", "Λογοθεραπεία:", 2, 0),
            ("psychotherapy", "Ψυχοθεραπεία:", 2, 1),
            ("occupational", "Εργοθεραπεία:", 2, 2),
        ),
    )
    replace_dates(session, period.id, pts[0].id, [D(2025, 10, 16), D(2025, 10, 20)])
    replace_dates(session, period.id, pts[1].id, [D(2025, 10, 16), D(2025, 10, 21)])
    with pytest.raises(ScheduleInvalid) as info:
        replace_dates(session, period.id, pts[2].id, [D(2025, 10, 16), D(2025, 10, 22)])
    assert "THERAPIST_DAILY_LIMIT" in [v.code for v in info.value.violations]


def test_fewer_dates_are_kept_but_reported_incomplete(session):
    _, period, pts, _ = setup(session)
    propose_schedule(session, period.id)
    status = replace_dates(session, period.id, pts[0].id, [D(2025, 10, 16), D(2025, 10, 20)])
    session.commit()
    speech = next(t for t in status.therapies if t.period_therapy_id == pts[0].id)
    assert (speech.planned, speech.authorized, speech.complete) == (2, 8, False)
    assert status.valid and not status.complete


# ---------------------------------------------------------------- approve / reopen
def test_approve_complete_schedule_then_locked(session):
    _, period, pts, _ = setup(session)
    propose_schedule(session, period.id)
    approve_schedule(session, period.id)
    session.commit()
    assert schedule_status(session, period.id).status == "schedule_approved"
    with pytest.raises(ScheduleLocked):
        propose_schedule(session, period.id)
    with pytest.raises(ScheduleLocked):
        replace_dates(session, period.id, pts[0].id, [D(2025, 10, 16)])
    first = stored(session, pts[0].id)[0]
    with pytest.raises(IntegrityError, match="schedule is approved"):  # database guard too
        session.execute(text("DELETE FROM scheduled_sessions WHERE id = :id"), {"id": first.id})
    session.rollback()
    reopen_schedule(session, period.id)
    session.commit()
    assert schedule_status(session, period.id).status == "draft"
    with pytest.raises(ScheduleLocked):
        reopen_schedule(session, period.id)


def test_incomplete_schedule_needs_explicit_approval(session):
    _, period, pts, _ = setup(session)
    propose_schedule(session, period.id)
    replace_dates(session, period.id, pts[0].id, [D(2025, 10, 16), D(2025, 10, 20)])
    session.commit()
    with pytest.raises(ScheduleIncomplete, match="2/8"):
        approve_schedule(session, period.id)
    session.rollback()
    approve_schedule(session, period.id, allow_incomplete=True)
    session.commit()
    entry = session.scalars(select(AuditLog).where(AuditLog.action == "schedule.approved")).one()
    assert json.loads(entry.details)["incomplete"] == [
        {"period_therapy_id": pts[0].id, "planned": 2, "authorized": 8}
    ]


def test_therapy_without_dates_can_never_be_approved(session):
    _, period, pts, _ = setup(session)
    propose_schedule(session, period.id)
    replace_dates(session, period.id, pts[1].id, [])
    session.commit()
    with pytest.raises(ScheduleIncomplete, match="without any planned date"):
        approve_schedule(session, period.id, allow_incomplete=True)


def test_approval_rechecks_todays_calendar(session):
    _, period, pts, _ = setup(session)
    propose_schedule(session, period.id)
    session.commit()
    planned = stored(session, pts[0].id)[0].session_date
    session.add(HolidayOverride(day=planned, kind="closed", name="Έκτακτο κλείσιμο"))
    session.commit()
    with pytest.raises(ScheduleInvalid) as info:
        approve_schedule(session, period.id)
    assert "NON_WORKING_DAY" in [v.code for v in info.value.violations]


def test_unknown_period_and_therapy(session):
    _, period, _, _ = setup(session)
    with pytest.raises(LookupError):
        propose_schedule(session, 999)
    with pytest.raises(LookupError):
        replace_dates(session, period.id, 999, [])


# ---------------------------------------------------------------- database guards (0002)
def insert(session, pt, day):
    session.execute(
        text(
            "INSERT INTO scheduled_sessions (period_therapy_id, session_date, origin) "
            "VALUES (:pt, :d, 'manual')"
        ),
        {"pt": pt.id, "d": day.isoformat()},
    )


@pytest.mark.parametrize(
    "day, message",
    [
        (D(2025, 10, 18), "Saturday or Sunday"),
        (D(2025, 10, 19), "Saturday or Sunday"),
    ],
)
def test_database_refuses_weekends(session, day, message):
    _, _, pts, _ = setup(session)
    with pytest.raises(IntegrityError, match=message):
        insert(session, pts[0], day)


def test_database_refuses_duplicates_per_setting(session):
    _, _, pts, _ = setup(session)
    insert(session, pts[0], D(2025, 10, 16))
    with pytest.raises(IntegrityError, match="more sessions of this therapy"):
        insert(session, pts[0], D(2025, 10, 16))
    session.rollback()
    session.get(CenterSettings, 1).max_sessions_per_therapy_per_day = 2
    session.commit()
    insert(session, pts[0], D(2025, 10, 16))
    insert(session, pts[0], D(2025, 10, 16))  # now allowed


def test_database_refuses_a_third_therapist(session):
    _, _, pts, _ = setup(
        session,
        spec=(("speech", "Λ:", 2, 0), ("psychotherapy", "Ψ:", 2, 1), ("occupational", "Ε:", 2, 2)),
    )
    insert(session, pts[0], D(2025, 10, 16))
    insert(session, pts[1], D(2025, 10, 16))
    with pytest.raises(IntegrityError, match="more different therapists"):
        insert(session, pts[2], D(2025, 10, 16))


def test_database_counts_the_same_therapist_once(session):
    _, _, pts, _ = setup(
        session,
        spec=(
            ("speech", "Λ:", 2, 0),
            ("special_education", "Ε:", 2, 0),
            ("psychotherapy", "Ψ:", 2, 1),
        ),
    )
    for pt in pts:
        insert(session, pt, D(2025, 10, 16))  # therapists 1, 1, 2: allowed
    session.commit()


def test_database_update_is_checked_too(session):
    _, _, pts, _ = setup(session)
    insert(session, pts[0], D(2025, 10, 16))
    insert(session, pts[0], D(2025, 10, 17))
    session.commit()  # so the rollbacks below keep these rows
    with pytest.raises(IntegrityError, match="more sessions of this therapy"):
        session.execute(
            text(
                "UPDATE scheduled_sessions SET session_date = '2025-10-16' "
                "WHERE session_date = '2025-10-17'"
            )
        )
    session.rollback()
    with pytest.raises(IntegrityError, match="Saturday or Sunday"):
        session.execute(
            text(
                "UPDATE scheduled_sessions SET session_date = '2025-10-18' "
                "WHERE session_date = '2025-10-17'"
            )
        )


def test_migration_0002_round_trip(tmp_path):
    from app import migrate
    from app.db import make_engine

    path = tmp_path / "rt.db"
    migrate.upgrade(path)
    migrate.downgrade(path, "0001")
    assert migrate.current_revision(path) == "0001"
    engine = make_engine(path)
    with engine.connect() as conn:
        sql = conn.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE name = 'scheduled_sessions_check_insert'"
        ).scalar()
    engine.dispose()
    assert "Saturday" not in sql  # the 0001 definition is back
    migrate.upgrade(path)
    assert migrate.check(path) == []
