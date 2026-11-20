"""Document periods, their therapies (one referral each) and planned dates."""

from __future__ import annotations

from datetime import date

from sqlalchemy import text

from tests.db import factories as f
from tests.db.conftest import rejected
from tests.db.test_prescriptions import example_prescription

D = date
OCT = (D(2025, 10, 16), D(2025, 11, 15))  # second monthly window of the example


def setup_period(session, window=OCT):
    pat, rx, items = example_prescription(session)
    thr = f.therapist(session)
    per = f.period(session, pat, *window)
    refs = {
        code: next(r for r in it.referral_periods if r.valid_from == window[0])
        for code, it in items.items()
    }
    return pat, rx, items, thr, per, refs


def issue(session, per, pt=None):
    tpl = f.template(session, "Πρότυπο", "1:1")
    tv = f.version(session, tpl)
    f.document(session, per, tv)
    session.execute(text("UPDATE periods SET status = 'issued' WHERE id = :id"), {"id": per.id})
    return tv


# ---------------------------------------------------------------- linking a referral period
def test_period_therapy_reads_quantity_from_its_referral(session):
    _, _, _, thr, per, refs = setup_period(session)
    pt = f.period_therapy(session, per, refs["speech"], thr)
    qty = session.execute(
        text(
            "SELECT r.authorized_quantity FROM period_therapies pt "
            "JOIN referral_periods r ON r.id = pt.referral_period_id WHERE pt.id = :id"
        ),
        {"id": pt.id},
    ).scalar()
    assert qty == 8


def test_a_referral_period_can_be_documented_only_once(session, engine):
    """Guarded twice: UNIQUE(referral_period_id) and UNIQUE(period_id, therapy_type_id). With
    window equality, a second use could only come from the same period, so either fires."""
    _, _, _, thr, per, refs = setup_period(session)
    f.period_therapy(session, per, refs["speech"], thr)
    with rejected(session, "UNIQUE constraint failed: period_therapies"):
        f.period_therapy(session, per, refs["speech"], thr)
    from sqlalchemy import inspect

    uniques = inspect(engine).get_unique_constraints("period_therapies")
    assert {
        "column_names": ["referral_period_id"],
        "name": "uq_period_therapies_referral_period_id",
    } in uniques


def test_duplicate_document_period_is_rejected(session):
    pat, *_ = setup_period(session)
    with rejected(session, "UNIQUE constraint failed: periods.patient_id"):
        f.period(session, pat, *OCT)


def test_referral_window_must_equal_period_window(session):
    pat, _, items, thr, _, _ = setup_period(session)
    calendar_month = f.period(session, pat, D(2025, 11, 1), D(2025, 11, 30))
    ref = items["speech"].referral_periods[1]  # 16/10–15/11
    with rejected(session, "referral window must equal the document period window"):
        f.period_therapy(session, calendar_month, ref, thr)


def test_therapies_with_different_windows_go_to_separate_overlapping_periods(session):
    """Independent windows: speech 16th–15th and OT calendar months in one prescription.
    Each gets its own document period, and these periods overlap."""
    pat = f.patient(session)
    speech = f.therapy_type(session, "speech", "Λογοθεραπεία:")
    ot = f.therapy_type(session, "occupational", "Εργοθεραπεία:")
    rx = f.prescription(session, pat, D(2025, 9, 16), D(2026, 3, 15))
    it_s = f.item(session, rx, speech, 8)
    it_o = f.item(session, rx, ot, 4)
    ref_s = f.referral(session, it_s, 1, D(2025, 10, 16), D(2025, 11, 15), 8)
    ref_o = f.referral(session, it_o, 1, D(2025, 11, 1), D(2025, 11, 30), 4)
    thr = f.therapist(session)
    p1 = f.period(session, pat, D(2025, 10, 16), D(2025, 11, 15))
    p2 = f.period(session, pat, D(2025, 11, 1), D(2025, 11, 30))
    f.period_therapy(session, p1, ref_s, thr)
    f.period_therapy(session, p2, ref_o, thr)
    session.commit()  # accepted


def test_source_must_match_patient_therapy_and_be_confirmed(session):
    _, _, items, thr, per, refs = setup_period(session)
    with rejected(session, "different therapy type"):
        f.period_therapy(
            session,
            per,
            refs["speech"],
            thr,
            therapy_type_id=items["psychotherapy"].therapy_type_id,
        )

    _, _, _, thr, per, refs = setup_period(session)
    other_patient = f.patient(session, amka="22222222222")
    foreign_period = f.period(session, other_patient, *OCT)
    with rejected(session, "belongs to another patient"):
        f.period_therapy(session, foreign_period, refs["speech"], thr)

    pat = f.patient(session, amka="33333333333")
    speech = f.therapy_type(session, "speech", "Λογοθεραπεία:")
    draft = f.prescription(session, pat, D(2025, 9, 16), D(2026, 3, 15), status="draft")
    ref = f.referral(session, f.item(session, draft, speech, 8), 1, *OCT, 8)
    thr = f.therapist(session)
    per = f.period(session, pat, *OCT)
    with rejected(session, "not confirmed"):
        f.period_therapy(session, per, ref, thr)


def test_prescription_data_is_read_only_once_used(session):
    _, rx, items, thr, per, refs = setup_period(session)
    f.period_therapy(session, per, refs["speech"], thr)
    session.commit()
    for sql, match in [
        (
            "UPDATE referral_periods SET authorized_quantity = 9 WHERE id = :ref",
            "referral period is used",
        ),
        (
            "UPDATE prescription_items SET monthly_quantity = 9 WHERE id = :item",
            "prescribed therapy is used",
        ),
        ("UPDATE prescriptions SET status = 'draft' WHERE id = :rx", "prescription is used"),
        (
            "UPDATE prescriptions SET valid_from = '2025-09-01' WHERE id = :rx",
            "prescription is used",
        ),
    ]:
        with rejected(session, match):
            session.execute(
                text(sql), {"ref": refs["speech"].id, "item": items["speech"].id, "rx": rx.id}
            )
    # unused referral periods of the same prescription can still be corrected
    unused = refs["psychotherapy"]
    session.execute(
        text("UPDATE referral_periods SET referral_no = '000000000000001' WHERE id = :id"),
        {"id": unused.id},
    )
    # notes are not locked
    session.execute(
        text("UPDATE prescriptions SET notes = 'σημείωση' WHERE id = :id"), {"id": rx.id}
    )


def test_period_window_fixed_once_therapies_attached(session):
    _, _, _, thr, per, refs = setup_period(session)
    f.period_therapy(session, per, refs["speech"], thr)
    with rejected(session, "window is fixed"):
        session.execute(
            text("UPDATE periods SET end_date = '2025-11-14' WHERE id = :id"), {"id": per.id}
        )


def test_period_therapy_links_cannot_be_rewired(session):
    _, _, _, thr, per, refs = setup_period(session)
    pt = f.period_therapy(session, per, refs["speech"], thr)
    with rejected(session, "links cannot change"):
        session.execute(
            text("UPDATE period_therapies SET referral_period_id = :r WHERE id = :id"),
            {"r": refs["psychotherapy"].id, "id": pt.id},
        )


def test_home_visit_is_optional_boolean(session):
    _, _, _, thr, per, refs = setup_period(session)
    pt = f.period_therapy(session, per, refs["speech"], thr)
    assert pt.home_visit is None
    session.execute(
        text("UPDATE period_therapies SET home_visit = 1 WHERE id = :id"), {"id": pt.id}
    )
    with rejected(session, "home_visit_bool"):
        session.execute(
            text("UPDATE period_therapies SET home_visit = 2 WHERE id = :id"), {"id": pt.id}
        )


# ---------------------------------------------------------------- planned dates
def test_planned_dates_must_lie_inside_the_period(session):
    _, _, _, thr, per, refs = setup_period(session)
    pt = f.period_therapy(session, per, refs["speech"], thr)
    f.session_date(session, pt, D(2025, 10, 16))
    f.session_date(session, pt, D(2025, 11, 14), origin="manual")
    with rejected(session, "outside the document period"):
        f.session_date(session, pt, D(2025, 11, 16))

    _, _, _, thr, per, refs = setup_period(session)
    pt = f.period_therapy(session, per, refs["speech"], thr)
    sd = f.session_date(session, pt, D(2025, 10, 20))
    with rejected(session, "outside the document period"):
        session.execute(
            text("UPDATE scheduled_sessions SET session_date = '2025-10-15' WHERE id = :id"),
            {"id": sd.id},
        )


def test_planned_date_origin_and_format(session):
    _, _, _, thr, per, refs = setup_period(session)
    pt = f.period_therapy(session, per, refs["speech"], thr)
    with rejected(session, "origin_allowed"):
        f.session_date(session, pt, D(2025, 10, 20), origin="performed")


# ---------------------------------------------------------------- issuing and locks
def test_period_can_only_become_issued_through_a_document(session):
    pat, _, _, thr, per, refs = setup_period(session)
    with rejected(session, "only become issued through a generated document"):
        session.execute(text("UPDATE periods SET status = 'issued' WHERE id = :id"), {"id": per.id})
    with rejected(session, "only become issued through a generated document"):
        f.period(session, f.patient(session, amka="44444444444"), *OCT, status="issued")


def test_issued_period_is_frozen_and_can_be_reopened(session):
    _, _, _, thr, per, refs = setup_period(session)
    pt = f.period_therapy(session, per, refs["speech"], thr, home_visit=True)
    sd = f.session_date(session, pt, D(2025, 10, 20))
    tv = issue(session, per)
    session.commit()

    for sql in [
        "UPDATE period_therapies SET home_visit = 0 WHERE id = :pt",
        "DELETE FROM period_therapies WHERE id = :pt",
        "UPDATE scheduled_sessions SET session_date = '2025-10-21' WHERE id = :sd",
        "DELETE FROM scheduled_sessions WHERE id = :sd",
        "INSERT INTO scheduled_sessions (period_therapy_id, session_date, origin) "
        "VALUES (:pt, '2025-10-22', 'manual')",
        "INSERT INTO period_therapies (period_id, therapy_type_id, therapist_id, "
        "referral_period_id) VALUES (:per, :tt, :thr, :ref)",
    ]:
        with rejected(session, "issued; reopen it"):
            session.execute(
                text(sql),
                {
                    "pt": pt.id,
                    "sd": sd.id,
                    "per": per.id,
                    "thr": thr.id,
                    "tt": refs["psychotherapy"].item.therapy_type_id,
                    "ref": refs["psychotherapy"].id,
                },
            )

    # a correction needs the period reopened first
    with rejected(session, "reopen it before issuing a correction"):
        f.document(session, per, tv, version_no=2)

    session.execute(text("UPDATE periods SET status = 'draft' WHERE id = :id"), {"id": per.id})
    session.execute(
        text("UPDATE period_therapies SET home_visit = 0 WHERE id = :id"), {"id": pt.id}
    )
    f.document(session, per, tv, version_no=2)
    session.execute(text("UPDATE periods SET status = 'issued' WHERE id = :id"), {"id": per.id})
    session.commit()
    versions = session.execute(
        text(
            "SELECT version, reason FROM generated_documents WHERE period_id = :id ORDER BY version"
        ),
        {"id": per.id},
    ).all()
    assert versions == [(1, "initial"), (2, "correction")]
