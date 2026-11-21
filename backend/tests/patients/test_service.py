"""Patient service (M5.2): create, update, AMKA correction, lifecycle, warnings, audit,
``updated_at`` and database-rule mapping."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.models import AuditLog, Patient
from app.services import patients as svc
from tests.db import factories as f
from tests.db.test_periods import issue, setup_period
from tests.patients.amka import synthetic_amka
from tests.patients.conftest import person

NOW = datetime(2026, 3, 2, 10, 15, 30, 123456)
LATER = NOW + timedelta(hours=2)


def audit(session):
    return [
        (r.action, r.entity_id, json.loads(r.details) if r.details else None)
        for r in session.scalars(
            select(AuditLog).where(AuditLog.entity == "patients").order_by(AuditLog.id)
        )
    ]


def make(session, n=1, now=NOW, **kw):
    return svc.create_patient(session, person(n, **kw), now).item


def code_of(call) -> str:
    with pytest.raises(svc.PatientError) as info:
        call()
    return info.value.code


# ---------------------------------------------------------------- create
def test_create_normalises_and_stamps(session):
    row = make(session, first_name="  Γιώργος   Ανδρέας ", notes="  σημείωση\n\tδεύτερη  ")
    assert row.first_name == "Γιώργος Ανδρέας"
    assert row.notes == "σημείωση\n\tδεύτερη"
    assert row.active is True
    assert row.created_at == row.updated_at == NOW.replace(microsecond=0)
    assert audit(session) == [("patient.created", row.id, None)]


def test_create_requires_every_identity_field(session):
    for missing in ("first_name", "last_name", "father_name", "amka"):
        body = person(1)
        del body[missing]
        with pytest.raises(svc.PatientError) as info:
            svc.create_patient(session, body, NOW)
        assert info.value.code == "invalid_value" and missing in info.value.message


def test_create_refuses_unknown_fields_and_invalid_values(session):
    assert code_of(lambda: svc.create_patient(session, {**person(1), "x": 1}, NOW)) == (
        "invalid_field"
    )
    assert code_of(lambda: make(session, first_name="Γιώρ\tγος")) == "invalid_value"
    assert code_of(lambda: make(session, amka=synthetic_amka(1)[:-1])) == "invalid_value"
    assert code_of(lambda: make(session, notes="α\rβ")) == "invalid_value"
    assert session.scalar(select(Patient.id)) is None


@pytest.mark.parametrize("first_active", [True, False])
def test_amka_is_unique_across_active_and_inactive_patients(session, first_active):
    first = make(session, 1)
    if not first_active:
        svc.set_patient_active(session, first.id, False, NOW)
    with pytest.raises(svc.PatientError) as info:
        make(session, 1, first_name="Άλλος")
    assert info.value.code == "duplicate_amka"
    assert info.value.details == {"patient_id": first.id, "active": first_active}
    assert synthetic_amka(1) not in info.value.message


def test_a_duplicate_that_slips_past_the_check_is_mapped_by_the_database(session, monkeypatch):
    make(session, 1)
    monkeypatch.setattr(svc, "_check_amka_free", lambda *a, **k: None)
    with pytest.raises(svc.PatientError) as info:
        make(session, 1)
    assert info.value.code == "duplicate_amka" and info.value.details == {}


def test_same_names_give_a_possible_duplicate_warning(session):
    a = make(session, 1, first_name="Άννα", last_name="Οικονόμου", father_name="Σταύρος")
    saved = svc.create_patient(
        session,
        person(2, first_name="ΑΝΝΑ", last_name="οικονομου", father_name="ΣΤΑΥΡΟΣ"),
        NOW,
    )
    assert [(w.code, w.ids) for w in saved.warnings] == [("possible_duplicate", (a.id,))]
    other = svc.create_patient(
        session,
        person(3, first_name="Άννα", last_name="Οικονόμου", father_name="Πέτρος"),
        NOW,
    )
    assert other.warnings == []  # a different father's name is a different person


# ---------------------------------------------------------------- update
def test_update_changes_only_given_fields_and_stamps(session):
    row = make(session)
    saved = svc.update_patient(session, row.id, {"last_name": "Νέο  Επώνυμο"}, LATER)
    assert saved.item.last_name == "Νέο Επώνυμο" and saved.item.first_name == "Γεώργιος"
    assert saved.item.updated_at == LATER.replace(microsecond=0)
    assert saved.item.created_at == NOW.replace(microsecond=0)
    assert audit(session)[-1] == ("patient.updated", row.id, {"fields": ["last_name"]})


def test_a_no_op_update_writes_nothing(session):
    row = make(session, notes="σημείωση")
    before = audit(session)
    saved = svc.update_patient(
        session, row.id, {"first_name": " Γεώργιος ", "notes": "σημείωση  "}, LATER
    )
    assert saved.item.updated_at == NOW.replace(microsecond=0)
    assert audit(session) == before


def test_update_can_clear_notes_but_not_names(session):
    row = make(session, notes="κάτι")
    assert svc.update_patient(session, row.id, {"notes": "   "}, LATER).item.notes is None
    for name in ("first_name", "last_name", "father_name"):
        assert code_of(lambda n=name: svc.update_patient(session, row.id, {n: None}, LATER)) == (
            "invalid_value"
        )
        assert code_of(lambda n=name: svc.update_patient(session, row.id, {n: " "}, LATER)) == (
            "invalid_value"
        )


def test_update_refuses_the_amka_and_unknown_ids(session):
    row = make(session)
    assert code_of(
        lambda: svc.update_patient(session, row.id, {"amka": synthetic_amka(2)}, LATER)
    ) == ("invalid_field")
    assert code_of(lambda: svc.update_patient(session, 999, {"notes": "x"}, LATER)) == ("not_found")
    assert session.get(Patient, row.id).amka == synthetic_amka(1)


def test_update_warns_about_a_new_duplicate(session):
    a = make(session, 1, first_name="Άννα")
    b = make(session, 2, first_name="Μαρία")
    saved = svc.update_patient(session, b.id, {"first_name": "άννα"}, LATER)
    assert [(w.code, w.ids) for w in saved.warnings] == [("possible_duplicate", (a.id,))]


# ---------------------------------------------------------------- AMKA correction
def test_correct_amka_without_issued_documents(session):
    row = make(session, 1)
    saved = svc.correct_amka(session, row.id, f" {synthetic_amka(2)} ", LATER)
    assert saved.item.amka == synthetic_amka(2) and saved.warnings == []
    assert saved.item.updated_at == LATER.replace(microsecond=0)
    assert audit(session)[-1] == ("patient.amka_corrected", row.id, {"issued_documents": 0})


def test_correcting_to_the_same_amka_changes_nothing(session):
    row = make(session, 1)
    before = audit(session)
    svc.correct_amka(session, row.id, synthetic_amka(1), LATER)
    assert audit(session) == before and row.updated_at == NOW.replace(microsecond=0)


def test_correct_amka_refuses_invalid_and_taken_values(session):
    a = make(session, 1)
    b = make(session, 2)
    assert code_of(lambda: svc.correct_amka(session, b.id, "12", LATER)) == "invalid_value"
    with pytest.raises(svc.PatientError) as info:
        svc.correct_amka(session, b.id, synthetic_amka(1), LATER)
    assert info.value.code == "duplicate_amka" and info.value.details["patient_id"] == a.id
    assert code_of(lambda: svc.correct_amka(session, 999, synthetic_amka(5), LATER)) == (
        "not_found"
    )


def test_correcting_the_amka_of_a_patient_with_issued_documents_needs_confirmation(session):
    pat, _, _, thr, per, refs = setup_period(session)
    f.period_therapy(session, per, refs["speech"], thr)
    issue(session, per)
    with pytest.raises(svc.PatientError) as info:
        svc.correct_amka(session, pat.id, synthetic_amka(9), LATER)
    assert info.value.code == "amka_change_needs_confirmation"
    assert info.value.details == {"issued_documents": 1}
    assert session.get(Patient, pat.id).amka != synthetic_amka(9)

    saved = svc.correct_amka(
        session, pat.id, synthetic_amka(9), LATER, acknowledge_issued_documents=True
    )
    assert saved.item.amka == synthetic_amka(9)
    assert [(w.code, w.count) for w in saved.warnings] == [
        ("issued_documents_show_previous_details", 1)
    ]
    assert audit(session)[-1] == ("patient.amka_corrected", pat.id, {"issued_documents": 1})


def test_issued_documents_are_untouched_by_patient_edits(session):
    pat, _, _, thr, per, refs = setup_period(session)
    f.period_therapy(session, per, refs["speech"], thr)
    issue(session, per)
    before = session.execute(text("SELECT * FROM generated_documents")).all()
    svc.update_patient(session, pat.id, {"first_name": "Αλλαγμένο"}, LATER)
    svc.correct_amka(session, pat.id, synthetic_amka(8), LATER, acknowledge_issued_documents=True)
    assert session.execute(text("SELECT * FROM generated_documents")).all() == before


# ---------------------------------------------------------------- lifecycle
def test_deactivate_and_reactivate(session):
    row = make(session)
    saved = svc.set_patient_active(session, row.id, False, LATER)
    assert saved.item.active is False and saved.warnings == []
    assert saved.item.updated_at == LATER.replace(microsecond=0)
    later2 = LATER + timedelta(hours=1)
    assert svc.set_patient_active(session, row.id, True, later2).item.active is True
    assert [a for a, _, _ in audit(session)] == [
        "patient.created",
        "patient.deactivated",
        "patient.activated",
    ]


def test_repeating_activation_or_deactivation_changes_nothing(session):
    row = make(session)
    svc.set_patient_active(session, row.id, True, LATER)
    assert row.updated_at == NOW.replace(microsecond=0)
    svc.set_patient_active(session, row.id, False, LATER)
    before = audit(session)
    svc.set_patient_active(session, row.id, False, LATER + timedelta(days=1))
    assert audit(session) == before and row.updated_at == LATER.replace(microsecond=0)


def test_deactivation_reports_open_work_and_changes_nothing_else(session):
    pat, rx, _, thr, per, _ = setup_period(session)
    f.prescription(session, pat, date(2026, 4, 1), date(2026, 9, 30), status="draft")
    tt = f.therapy_type(session, "occupational", "Εργοθεραπεία:")
    _assignment(session, pat, tt, thr)
    saved = svc.set_patient_active(session, pat.id, False, LATER)
    assert {(w.code, w.count) for w in saved.warnings} == {
        ("open_assignments", 1),
        ("draft_prescriptions", 1),
        ("unissued_periods", 1),
    }
    # nothing was closed or changed automatically
    assert session.execute(text("SELECT valid_to FROM therapist_assignments")).all() == [(None,)]
    assert session.execute(text("SELECT status FROM periods")).all() == [("draft",)]


def _assignment(session, pat, tt, thr):
    from app.models import TherapistAssignment

    return f.add(
        session,
        TherapistAssignment(
            patient_id=pat.id,
            therapy_type_id=tt.id,
            therapist_id=thr.id,
            valid_from=date(2026, 1, 1),
        ),
    )


def test_inactive_patients_can_still_be_edited_and_corrected(session):
    row = make(session, 1)
    svc.set_patient_active(session, row.id, False, NOW)
    assert svc.update_patient(session, row.id, {"notes": "νέα"}, LATER).item.notes == "νέα"
    assert svc.correct_amka(session, row.id, synthetic_amka(3), LATER).item.amka == (
        synthetic_amka(3)
    )


def test_usage_counts(session):
    pat, _, _, thr, per, refs = setup_period(session)
    f.period_therapy(session, per, refs["speech"], thr)
    issue(session, per)
    usage = svc.patient_usage(session, pat.id)
    assert usage == svc.Usage(
        prescriptions=1,
        draft_prescriptions=0,
        open_assignments=0,
        unissued_periods=0,
        issued_documents=1,
    )
    assert code_of(lambda: svc.patient_usage(session, 999)) == "not_found"


# ---------------------------------------------------------------- database guard
def test_patients_cannot_be_deleted_even_without_references(session):
    row = make(session)
    session.flush()
    with pytest.raises(IntegrityError, match="never deleted"):
        session.execute(text("DELETE FROM patients WHERE id = :id"), {"id": row.id})


# ---------------------------------------------------------------- review fixes (M5.2)
def _rows(session):
    patients = session.execute(text("SELECT * FROM patients ORDER BY id")).all()
    audits = session.execute(text("SELECT * FROM audit_log ORDER BY id")).all()
    return patients, audits


@pytest.mark.parametrize("field", ["first_name", "last_name", "father_name", "notes"])
def test_lone_surrogates_are_refused_and_write_nothing(session, field):
    row = make(session, 1)
    before = _rows(session)
    with pytest.raises(svc.PatientError) as info:
        make(session, 2, **{field: "Γιώρ\ud800γος"})
    assert info.value.code == "invalid_value" and "not allowed" in info.value.message
    with pytest.raises(svc.PatientError):
        svc.update_patient(session, row.id, {field: "x\udfff"}, LATER)
    session.flush()
    assert _rows(session) == before


@pytest.mark.parametrize("name", ["-", "1", "\u0301", "\ue000", "..."])
def test_names_without_a_letter_are_refused_and_write_nothing(session, name):
    row = make(session, 1)
    before = _rows(session)
    assert code_of(lambda: make(session, 2, first_name=name)) == "invalid_value"
    assert code_of(lambda: svc.update_patient(session, row.id, {"last_name": name}, LATER)) == (
        "invalid_value"
    )
    assert _rows(session) == before


def test_the_unknown_field_error_never_names_the_fields(session):
    key = synthetic_amka(77)
    row = make(session, 1)
    for call in (
        lambda: svc.create_patient(session, {**person(2), key: 1}, NOW),
        lambda: svc.update_patient(session, row.id, {"Κρυφόόνομα": 1}, LATER),
    ):
        with pytest.raises(svc.PatientError) as info:
            call()
        assert info.value.code == "invalid_field"
        assert info.value.message == "unknown fields are not allowed"
        assert info.value.details == {}
