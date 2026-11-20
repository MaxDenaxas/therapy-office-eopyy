"""Foreign keys, unique constraints and rejection of invalid values."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import inspect, text

from app.models import AdminAccount, Base, CenterSettings, HolidayOverride, Patient
from tests.db import factories as f
from tests.db.conftest import rejected

D = date


# ---------------------------------------------------------------- foreign keys
def test_foreign_keys_are_enforced(session):
    with rejected(session, "FOREIGN KEY constraint failed"):
        f.prescription(session, Patient(id=999), D(2025, 9, 16), D(2026, 3, 15))


def test_referenced_rows_cannot_be_deleted(session):
    pat = f.patient(session)
    f.prescription(session, pat, D(2025, 9, 16), D(2026, 3, 15))
    session.commit()
    with rejected(session, "FOREIGN KEY constraint failed"):
        session.execute(text("DELETE FROM patients WHERE id = :id"), {"id": pat.id})


def test_every_foreign_key_restricts_delete():
    """RESTRICT is the documented default: no cascade deletes anywhere."""
    for table in Base.metadata.tables.values():
        for fk in table.foreign_keys:
            assert fk.ondelete is None, f"{table.name}.{fk.parent.name} cascades"


# ---------------------------------------------------------------- unique constraints
@pytest.mark.parametrize(
    "build, match",
    [
        (
            lambda s: [f.patient(s, amka="11111111111"), f.patient(s, amka="11111111111")],
            "UNIQUE constraint failed: patients.amka",
        ),
        (
            lambda s: [
                f.therapy_type(s, "speech", "Λογοθεραπεία:"),
                f.therapy_type(s, "speech", "Λογοθεραπεία:"),
            ],
            "UNIQUE constraint failed: therapy_types.code",
        ),
        (
            lambda s: [
                f.therapy_type(s, "a", "Α:", eopyy_code="530000010"),
                f.therapy_type(s, "b", "Β:", eopyy_code="530000010"),
            ],
            "UNIQUE constraint failed: therapy_types.eopyy_code",
        ),
        (
            lambda s: [
                s.add(HolidayOverride(day=D(2025, 12, 31), kind="closed", name="α")),
                s.flush(),
                s.add(HolidayOverride(day=D(2025, 12, 31), kind="open", name="β")),
                s.flush(),
            ],
            "UNIQUE constraint failed: holiday_overrides.day",
        ),
    ],
)
def test_unique_constraints(session, build, match):
    with rejected(session, match):
        build(session)


def test_prescription_number_unique_but_optional(session):
    pat = f.patient(session)
    f.prescription(session, pat, D(2025, 9, 16), D(2026, 3, 15))
    f.prescription(session, pat, D(2026, 3, 16), D(2026, 9, 15))  # two NULL numbers are fine
    f.prescription(session, pat, D(2026, 9, 16), D(2027, 3, 15), prescription_no="000000000000000")
    with rejected(session, "UNIQUE constraint failed: prescriptions.prescription_no"):
        f.prescription(
            session, pat, D(2027, 3, 16), D(2027, 9, 15), prescription_no="000000000000000"
        )


def test_one_prescription_item_per_therapy_type(session):
    pat = f.patient(session)
    speech = f.therapy_type(session, "speech", "Λογοθεραπεία:")
    rx = f.prescription(session, pat, D(2025, 9, 16), D(2026, 3, 15))
    f.item(session, rx, speech, 8)
    with rejected(session, "UNIQUE constraint failed: prescription_items"):
        f.item(session, rx, speech, 6)


def test_only_one_open_therapist_assignment_per_therapy(session):
    from app.models import TherapistAssignment as TA

    pat = f.patient(session)
    speech = f.therapy_type(session, "speech", "Λογοθεραπεία:")
    t1, t2 = f.therapist(session, "Α"), f.therapist(session, "Β")
    f.add(
        session,
        TA(
            patient_id=pat.id,
            therapy_type_id=speech.id,
            therapist_id=t1.id,
            valid_from=D(2025, 1, 1),
            valid_to=D(2025, 9, 15),
        ),
    )
    f.add(
        session,
        TA(
            patient_id=pat.id,
            therapy_type_id=speech.id,
            therapist_id=t2.id,
            valid_from=D(2025, 9, 16),
        ),
    )  # history + one open row: fine
    with rejected(session, "UNIQUE constraint failed: therapist_assignments"):
        f.add(
            session,
            TA(
                patient_id=pat.id,
                therapy_type_id=speech.id,
                therapist_id=t1.id,
                valid_from=D(2025, 10, 1),
            ),
        )


# ---------------------------------------------------------------- invalid values
@pytest.mark.parametrize(
    "sql, match",
    [
        (
            "INSERT INTO patients (first_name, last_name, father_name, amka) "
            "VALUES ('Α', 'Β', 'Γ', '1234567890')",
            "amka_format",
        ),  # 10 digits
        (
            "INSERT INTO patients (first_name, last_name, father_name, amka) "
            "VALUES ('Α', 'Β', 'Γ', '1234567890A')",
            "amka_format",
        ),  # letter
        (
            "INSERT INTO patients (first_name, last_name, father_name, amka) "
            "VALUES ('  ', 'Β', 'Γ', '11111111111')",
            "first_name_not_blank",
        ),
        (
            "INSERT INTO patients (first_name, last_name, father_name, amka, active) "
            "VALUES ('Α', 'Β', 'Γ', '11111111111', 2)",
            "active_bool",
        ),
        (
            "INSERT INTO holiday_overrides (day, kind, name) VALUES ('2025-02-30', 'closed', 'x')",
            "day_iso_date",
        ),  # not a real date
        (
            "INSERT INTO holiday_overrides (day, kind, name) VALUES ('16/09/2025', 'closed', 'x')",
            "day_iso_date",
        ),  # not ISO
        (
            "INSERT INTO holiday_overrides (day, kind, name) VALUES ('2025-12-31', 'maybe', 'x')",
            "kind_allowed",
        ),
        (
            "INSERT INTO therapy_types (code, name_el, template_label, eopyy_code) "
            "VALUES ('x', 'x', 'x:', '53A')",
            "eopyy_code_digits",
        ),
        (
            "INSERT INTO audit_log (action, entity, details) VALUES ('a', 'b', '{not json')",
            "details_json",
        ),
        ("UPDATE center_settings SET max_therapists_per_day = 0", "max_therapists_range"),
        ("UPDATE center_settings SET max_sessions_per_therapy_per_day = 9", "max_sessions_range"),
        ("UPDATE center_settings SET ada = ''", "ada_not_blank"),
    ],
)
def test_invalid_values_are_rejected(session, sql, match):
    with rejected(session, match):
        session.execute(text(sql))


def test_single_admin_account(session):
    f.add(session, AdminAccount(id=1, username="admin", password_hash="argon2id$placeholder"))
    with rejected(session, "UNIQUE constraint failed: admin_account.id"):
        f.add(session, AdminAccount(id=1, username="second", password_hash="x"))
    with rejected(session, "single_row"):
        f.add(session, AdminAccount(id=2, username="second", password_hash="x"))


def test_single_settings_row_cannot_be_duplicated_or_deleted(session):
    with rejected(session, "single_row"):
        f.add(session, CenterSettings(id=2))
    with rejected(session, "center settings cannot be deleted"):
        session.execute(text("DELETE FROM center_settings"))


def test_no_session_count_without_a_prescription_value():
    """The only quantity columns are the two prescription values; both are required,
    have no default, and nothing else in the schema can hold a session count."""
    quantity_columns = {
        (t.name, c.name)
        for t in Base.metadata.tables.values()
        for c in t.columns
        if "quantity" in c.name or "count" in c.name or "sessions" in c.name
    }
    assert quantity_columns == {
        ("prescription_items", "monthly_quantity"),
        ("prescription_items", "printed_total_quantity"),  # as printed, never used for counts
        ("referral_periods", "authorized_quantity"),
        ("admin_account", "failed_login_count"),  # not a session count
        ("center_settings", "max_sessions_per_therapy_per_day"),  # a scheduling limit
    }
    for table, column in [
        ("prescription_items", "monthly_quantity"),
        ("referral_periods", "authorized_quantity"),
    ]:
        col = Base.metadata.tables[table].c[column]
        assert col.nullable is False and col.server_default is None and col.default is None


def _rx(session):
    pat = f.patient(session)
    speech = f.therapy_type(session, "speech", "Λογοθεραπεία:")
    return f.prescription(session, pat, D(2025, 9, 16), D(2026, 3, 15)), speech


def test_monthly_quantity_is_required(session):
    rx, speech = _rx(session)
    with rejected(session, "NOT NULL constraint failed: prescription_items.monthly_quantity"):
        session.execute(
            text(
                "INSERT INTO prescription_items (prescription_id, therapy_type_id) VALUES (:p, :t)"
            ),
            {"p": rx.id, "t": speech.id},
        )


@pytest.mark.parametrize("bad", [0, -8])
def test_monthly_quantity_must_be_positive(session, bad):
    rx, speech = _rx(session)
    with rejected(session, "monthly_quantity_positive"):
        f.item(session, rx, speech, bad)


def test_authorized_quantity_is_required_and_positive(session):
    rx, speech = _rx(session)
    it = f.item(session, rx, speech, 8)
    session.commit()
    with rejected(session, "NOT NULL constraint failed: referral_periods.authorized_quantity"):
        session.execute(
            text(
                "INSERT INTO referral_periods (prescription_item_id, sequence_no, "
                "valid_from, valid_to) VALUES (:i, 1, '2025-09-16', '2025-10-15')"
            ),
            {"i": it.id},
        )
    with rejected(session, "authorized_quantity_positive"):
        f.referral(session, it, 1, D(2025, 9, 16), D(2025, 10, 15), 0)


def test_inspector_sees_partial_unique_indexes(engine):
    insp = inspect(engine)
    idx = {i["name"]: i for i in insp.get_indexes("document_templates")}
    assert idx["uq_document_templates_one_active_per_configuration"]["unique"]
    idx = {i["name"]: i for i in insp.get_indexes("therapist_assignments")}
    assert idx["uq_therapist_assignments_one_open"]["unique"]
