"""Prescription → prescribed therapy → monthly referral periods."""

from __future__ import annotations

from datetime import date

from sqlalchemy import select, text

from app.models import Prescription, ReferralPeriod
from tests.db import factories as f
from tests.db.conftest import rejected

D = date


def example_prescription(session):
    """Shaped like the real example: validity 16/09/2025–15/03/2026, speech 8/month and
    child psychotherapy 6/month, six monthly referrals each (16th to 15th)."""
    pat = f.patient(session)
    speech = f.therapy_type(session, "speech", "Λογοθεραπεία:", eopyy_code="530000010")
    psycho = f.therapy_type(session, "psychotherapy", "Ψυχοθεραπεία:", eopyy_code="530000032")
    rx = f.prescription(
        session,
        pat,
        D(2025, 9, 16),
        D(2026, 3, 15),
        prescription_no="000000000000000",
        decision_no="ΕΑ 00000",
        issued_on=D(2025, 9, 23),
        decision_date=D(2025, 9, 22),
    )
    items = {}
    for tt, qty in ((speech, 8), (psycho, 6)):
        it = f.item(
            session,
            rx,
            tt,
            qty,
            eopyy_code=tt.eopyy_code,
            printed_total_quantity=qty,
            printed_duration_months=1,
        )
        for seq, (start, end) in enumerate(f.monthly_windows(D(2025, 9, 16), 6), 1):
            f.referral(session, it, seq, start, end, qty)
        items[tt.code] = it
    return pat, rx, items


def test_prescription_items_and_referrals_are_linked(session):
    _, rx, items = example_prescription(session)
    session.commit()
    loaded = session.get(Prescription, rx.id)
    assert {i.monthly_quantity for i in loaded.items} == {8, 6}
    speech = items["speech"]
    windows = [
        (r.valid_from, r.valid_to, r.authorized_quantity)
        for r in sorted(speech.referral_periods, key=lambda r: r.sequence_no)
    ]
    assert windows[0] == (D(2025, 9, 16), D(2025, 10, 15), 8)
    assert windows[-1] == (D(2026, 2, 16), D(2026, 3, 15), 8)
    assert len(windows) == 6


def test_therapies_of_one_prescription_can_have_independent_windows(session):
    """Speech: 16th–15th for 6 months. Occupational therapy: calendar months, only 3 months,
    starting later, with a different quantity in its last month. All in one prescription."""
    pat = f.patient(session)
    speech = f.therapy_type(session, "speech", "Λογοθεραπεία:")
    ot = f.therapy_type(session, "occupational", "Εργοθεραπεία:")
    rx = f.prescription(session, pat, D(2025, 9, 16), D(2026, 3, 15))
    it_speech = f.item(session, rx, speech, 8)
    for seq, (start, end) in enumerate(f.monthly_windows(D(2025, 9, 16), 6), 1):
        f.referral(session, it_speech, seq, start, end, 8)
    it_ot = f.item(session, rx, ot, 4, printed_duration_months=3)
    f.referral(session, it_ot, 1, D(2025, 11, 1), D(2025, 11, 30), 4)
    f.referral(session, it_ot, 2, D(2025, 12, 1), D(2025, 12, 31), 4)
    f.referral(session, it_ot, 3, D(2026, 1, 1), D(2026, 1, 31), 2)
    session.commit()
    rows = session.execute(
        select(ReferralPeriod.prescription_item_id, ReferralPeriod.valid_from)
    ).all()
    assert len(rows) == 9


def test_referral_periods_of_one_therapy_cannot_overlap(session):
    _, _, items = example_prescription(session)
    with rejected(session, "must not overlap"):
        f.referral(session, items["speech"], 7, D(2025, 10, 1), D(2025, 10, 20), 8)


def test_overlap_check_also_applies_to_updates(session):
    _, _, items = example_prescription(session)
    session.commit()
    first, second = sorted(items["speech"].referral_periods, key=lambda r: r.sequence_no)[:2]
    with rejected(session, "must not overlap"):
        session.execute(
            text("UPDATE referral_periods SET valid_to = '2025-10-20' WHERE id = :id"),
            {"id": first.id},
        )
    # a non-overlapping change of its own window is accepted
    session.execute(
        text("UPDATE referral_periods SET valid_to = '2025-10-14' WHERE id = :id"), {"id": first.id}
    )
    assert second.valid_from == D(2025, 10, 16)


def test_same_window_is_fine_for_different_therapies(session):
    """Overlap is only forbidden within one therapy (example: both start 16/09)."""
    _, _, items = example_prescription(session)
    assert (
        items["speech"].referral_periods[0].valid_from
        == items["psychotherapy"].referral_periods[0].valid_from
    )


def test_referral_must_lie_inside_prescription_validity(session):
    pat = f.patient(session)
    speech = f.therapy_type(session, "speech", "Λογοθεραπεία:")
    rx = f.prescription(session, pat, D(2025, 9, 16), D(2026, 3, 15))
    it = f.item(session, rx, speech, 8)
    with rejected(session, "outside the prescription validity"):
        f.referral(session, it, 1, D(2026, 3, 16), D(2026, 4, 15), 8)
    pat = f.patient(session)
    speech = f.therapy_type(session, "speech", "Λογοθεραπεία:")
    rx = f.prescription(session, pat, D(2025, 9, 16), D(2026, 3, 15))
    it = f.item(session, rx, speech, 8)
    with rejected(session, "outside the prescription validity"):
        f.referral(session, it, 1, D(2025, 9, 1), D(2025, 9, 30), 8)


def test_prescription_validity_cannot_shrink_below_its_referrals(session):
    _, rx, _ = example_prescription(session)
    with rejected(session, "must cover all its referral periods"):
        session.execute(
            text("UPDATE prescriptions SET valid_to = '2026-02-28' WHERE id = :id"), {"id": rx.id}
        )


def test_invalid_validity_dates(session):
    pat = f.patient(session)
    with rejected(session, "valid_to_after_valid_from"):
        f.prescription(session, pat, D(2026, 3, 15), D(2025, 9, 16))


def test_invalid_prescription_status(session):
    pat = f.patient(session)
    with rejected(session, "status_allowed"):
        f.prescription(session, pat, D(2025, 9, 16), D(2026, 3, 15), status="approved")


def test_layout_is_required(session):
    pat = f.patient(session)
    with rejected(session, "NOT NULL constraint failed: prescriptions.layout"):
        session.execute(
            text(
                "INSERT INTO prescriptions (patient_id, valid_from, valid_to) "
                "VALUES (:p, '2025-09-16', '2026-03-15')"
            ),
            {"p": pat.id},
        )


def test_sequence_number_unique_per_therapy(session):
    pat = f.patient(session)
    speech = f.therapy_type(session, "speech", "Λογοθεραπεία:")
    rx = f.prescription(session, pat, D(2025, 9, 16), D(2026, 3, 15))
    it = f.item(session, rx, speech, 8)
    f.referral(session, it, 1, D(2025, 9, 16), D(2025, 10, 15), 8)
    with rejected(
        session,
        "UNIQUE constraint failed: referral_periods.prescription_item_id, "
        "referral_periods.sequence_no",
    ):
        f.referral(session, it, 1, D(2025, 10, 16), D(2025, 11, 15), 8)
