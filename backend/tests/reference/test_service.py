"""Reference data services and the migration-0004 database guards (M5.1)."""

from __future__ import annotations

import json
from datetime import date

import pytest
from sqlalchemy import select, text

from app.models import (
    AuditLog,
    DocumentTemplate,
    Therapist,
    TherapistAssignment,
    TherapyType,
)
from app.services import reference as svc
from app.services.reference import ReferenceDataError
from app.services.templates import (
    NoMatchingTemplate,
    deactivate_template,
    match_template,
)
from tests.db import factories as f
from tests.db.conftest import rejected
from tests.reference.conftest import activate_center_template

D = date


def audit_rows(session):
    return session.execute(
        select(AuditLog.action, AuditLog.entity, AuditLog.entity_id, AuditLog.details).order_by(
            AuditLog.id
        )
    ).all()


def type_data(**kw):
    return {
        "code": "speech2",
        "name_el": "Λογοθεραπεία Β",
        "template_label": "Λογοθεραπεία Β:",
        **kw,
    }


# ================================================================ therapy types
def test_create_therapy_type_with_defaults(session):
    saved = svc.create_therapy_type(session, type_data())
    row = saved.item
    assert row.id and row.active is True and row.supports_home_visit is False
    assert row.eopyy_code is None and row.eopyy_description is None
    assert saved.warnings == []


def test_create_therapy_type_full_and_normalised(session):
    row = svc.create_therapy_type(
        session,
        type_data(
            code="  occ  ",
            eopyy_code=" 530000010 ",
            eopyy_description="  περιγραφή ",
            supports_home_visit=True,
        ),
    ).item
    assert (row.code, row.eopyy_code, row.eopyy_description) == ("occ", "530000010", "περιγραφή")
    assert row.supports_home_visit is True


@pytest.mark.parametrize(
    "data, match",
    [
        ({"name_el": "  "}, "name_el must not be empty"),
        ({"template_label": ""}, "template_label must not be empty"),
        ({"code": " "}, "code must not be empty"),
        ({"bogus": 1}, "unknown field"),
    ],
)
def test_create_therapy_type_validation(session, data, match):
    with pytest.raises(ReferenceDataError, match=match):
        svc.create_therapy_type(session, type_data(**data))


def test_create_therapy_type_requires_the_mandatory_fields(session):
    with pytest.raises(ReferenceDataError, match="missing: code, name_el"):
        svc.create_therapy_type(session, {"template_label": "Χ:"})


def test_duplicate_code_and_eopyy_code_are_conflicts(session):
    svc.create_therapy_type(session, type_data(eopyy_code="530000010"))
    with pytest.raises(ReferenceDataError) as info:
        svc.create_therapy_type(session, type_data(name_el="άλλο"))
    assert info.value.code == "duplicate_code"
    with pytest.raises(ReferenceDataError) as info:
        svc.create_therapy_type(session, type_data(code="other", eopyy_code="530000010"))
    assert info.value.code == "duplicate_eopyy_code"
    # several types without any EOPYY code are fine
    svc.create_therapy_type(session, type_data(code="a1"))
    svc.create_therapy_type(session, type_data(code="a2"))


def test_conflict_messages_do_not_contain_the_value(session):
    svc.create_therapy_type(session, type_data(code="secretcode", eopyy_code="987654321"))
    for data in (type_data(code="secretcode"), type_data(code="x", eopyy_code="987654321")):
        with pytest.raises(ReferenceDataError) as info:
            svc.create_therapy_type(session, data)
        assert "secretcode" not in info.value.message and "987654321" not in info.value.message


def test_same_name_is_allowed_with_a_warning(session):
    first = svc.create_therapy_type(session, type_data(code="a1")).item
    second = svc.create_therapy_type(session, type_data(code="a2", name_el="ΛΟΓΟΘΕΡΑΠΕΙΑ  Β"))
    assert [(w.code, w.ids) for w in second.warnings] == [("duplicate_name", (first.id,))]


def test_update_therapy_type_changes_only_given_fields_and_audits_field_names(session):
    row = svc.create_therapy_type(session, type_data(eopyy_code="530000010")).item
    saved = svc.update_therapy_type(
        session, row.id, {"name_el": "Νέο όνομα", "eopyy_code": None, "supports_home_visit": True}
    )
    assert saved.item.name_el == "Νέο όνομα"
    assert saved.item.eopyy_code is None and saved.item.supports_home_visit is True
    assert saved.item.template_label == "Λογοθεραπεία Β:"  # untouched
    action, entity, entity_id, details = audit_rows(session)[-1]
    assert (action, entity, entity_id) == ("therapy_type.updated", "therapy_types", row.id)
    assert json.loads(details) == {"fields": ["eopyy_code", "name_el", "supports_home_visit"]}
    assert "Νέο όνομα" not in details


def test_update_with_unchanged_values_audits_nothing(session):
    row = svc.create_therapy_type(session, type_data()).item
    before = len(audit_rows(session))
    svc.update_therapy_type(session, row.id, {"name_el": row.name_el, "eopyy_code": None})
    assert len(audit_rows(session)) == before


def test_update_cannot_blank_required_fields_or_duplicate_codes(session):
    a = svc.create_therapy_type(session, type_data(code="a1", eopyy_code="111")).item
    b = svc.create_therapy_type(session, type_data(code="b1", eopyy_code="222")).item
    with pytest.raises(ReferenceDataError, match="name_el must not be empty"):
        svc.update_therapy_type(session, a.id, {"name_el": " "})
    with pytest.raises(ReferenceDataError) as info:
        svc.update_therapy_type(session, b.id, {"code": "a1"})
    assert info.value.code == "duplicate_code"
    with pytest.raises(ReferenceDataError) as info:
        svc.update_therapy_type(session, b.id, {"eopyy_code": "111"})
    assert info.value.code == "duplicate_eopyy_code"
    svc.update_therapy_type(session, a.id, {"eopyy_code": "111"})  # its own value: no conflict


def test_unknown_ids_are_not_found(session):
    for call in (
        lambda: svc.get_therapy_type(session, 999),
        lambda: svc.update_therapy_type(session, 999, {"name_el": "x"}),
        lambda: svc.set_therapy_type_active(session, 999, False),
        lambda: svc.get_therapist(session, 999),
        lambda: svc.update_therapist(session, 999, {"specialty": "x"}),
        lambda: svc.set_therapist_active(session, 999, False),
    ):
        with pytest.raises(ReferenceDataError) as info:
            call()
        assert info.value.code == "not_found"


def test_list_therapy_types_filters_and_orders(session):
    a = svc.create_therapy_type(session, type_data(code="a1", name_el="Β")).item
    b = svc.create_therapy_type(session, type_data(code="b1", name_el="Α")).item
    svc.set_therapy_type_active(session, a.id, False)
    assert [t.id for t in svc.list_therapy_types(session)] == [b.id, a.id]
    assert [t.id for t in svc.list_therapy_types(session, active=True)] == [b.id]
    assert [t.id for t in svc.list_therapy_types(session, active=False)] == [a.id]


def test_activate_and_deactivate_therapy_type_are_idempotent_and_audited(session):
    row = svc.create_therapy_type(session, type_data()).item
    svc.set_therapy_type_active(session, row.id, False)
    svc.set_therapy_type_active(session, row.id, False)  # no second audit row
    assert row.active is False
    svc.set_therapy_type_active(session, row.id, True)
    actions = [a for a, *_ in audit_rows(session)]
    assert actions == ["therapy_type.created", "therapy_type.deactivated", "therapy_type.activated"]


# ---------------------------------------------------------------- code immutability
def test_code_can_change_while_nothing_uses_the_type(session):
    row = svc.create_therapy_type(session, type_data()).item
    assert svc.update_therapy_type(session, row.id, {"code": "renamed"}).item.code == "renamed"


def _use_in_prescription_item(session, tt):
    pat = f.patient(session)
    rx = f.prescription(session, pat, D(2025, 9, 16), D(2026, 3, 15))
    f.item(session, rx, tt, 8)


def _use_in_assignment(session, tt):
    pat, th = f.patient(session), f.therapist(session)
    f.add(
        session,
        TherapistAssignment(
            patient_id=pat.id, therapy_type_id=tt.id, therapist_id=th.id, valid_from=D(2025, 1, 1)
        ),
    )


def _use_in_template(session, tt):
    from app.services.templates import register_template

    th = f.therapist(session)
    register_template(session, "Πρότυπο", [(tt.id, th.id)])


@pytest.mark.parametrize(
    "use, table",
    [
        (_use_in_prescription_item, "prescription_items"),
        (_use_in_assignment, "therapist_assignments"),
        (_use_in_template, "template_configuration_items"),
    ],
)
def test_code_is_frozen_once_a_record_uses_the_type(session, use, table):
    tt = f.therapy_type(session, "frozen", "Παγωμένο:")
    use(session, tt)
    session.flush()
    with pytest.raises(ReferenceDataError) as info:
        svc.update_therapy_type(session, tt.id, {"code": "changed"})
    assert info.value.code == "code_locked" and table in info.value.message
    assert info.value.details["referenced_by"] == [table]
    # other fields remain editable
    svc.update_therapy_type(session, tt.id, {"name_el": "Άλλο όνομα"})
    # and the same code "changed" to itself is not a change
    svc.update_therapy_type(session, tt.id, {"code": "frozen"})


def test_code_is_frozen_once_a_period_therapy_uses_the_type(session):
    from tests.db.test_periods import setup_period

    pat, rx, items, thr, per, refs = setup_period(session)
    f.period_therapy(session, per, refs["speech"], thr)
    session.flush()
    tt_id = items["speech"].therapy_type_id
    with pytest.raises(ReferenceDataError) as info:
        svc.update_therapy_type(session, tt_id, {"code": "changed"})
    assert info.value.code == "code_locked"
    assert "period_therapies" in info.value.details["referenced_by"]


@pytest.mark.parametrize(
    "use",
    [_use_in_prescription_item, _use_in_assignment, _use_in_template],
    ids=lambda u: u.__name__,
)
def test_the_database_also_refuses_a_code_change_when_the_service_is_bypassed(session, use):
    tt = f.therapy_type(session, "frozen", "Παγωμένο:")
    use(session, tt)
    session.flush()
    with rejected(session, "code cannot change once it is used"):
        session.execute(text("UPDATE therapy_types SET code = 'x' WHERE id = :i"), {"i": tt.id})


def test_the_database_allows_a_code_change_while_unused(session):
    tt = f.therapy_type(session, "free", "Ελεύθερο:")
    session.flush()
    session.execute(text("UPDATE therapy_types SET code = 'free2' WHERE id = :i"), {"i": tt.id})


# ================================================================ therapists
def therapist_data(**kw):
    return {"full_name": "Θεραπεύτρια Δοκιμής Α", **kw}


def test_create_therapist_minimal_and_full(session):
    minimal = svc.create_therapist(session, therapist_data()).item
    assert minimal.active is True and minimal.specialty is None and minimal.license_no is None
    full = svc.create_therapist(
        session,
        therapist_data(
            full_name="  Θεραπευτής Δοκιμής Β ",
            specialty="Λογοθεραπεία",
            license_no=" TEST-1 ",
            license_authority="Δοκιμαστική Αρχή",
            license_date=D(2020, 1, 31),
        ),
    ).item
    assert full.full_name == "Θεραπευτής Δοκιμής Β" and full.license_no == "TEST-1"
    assert full.license_date == D(2020, 1, 31)


def test_create_therapist_validation(session):
    with pytest.raises(ReferenceDataError, match="full_name must not be empty"):
        svc.create_therapist(session, {"full_name": "   "})
    with pytest.raises(ReferenceDataError, match="missing: full_name"):
        svc.create_therapist(session, {"specialty": "x"})
    with pytest.raises(ReferenceDataError, match="unknown field"):
        svc.create_therapist(session, therapist_data(active=False))  # status is not creatable


def test_duplicate_names_and_licence_numbers_are_warnings_not_errors(session):
    first = svc.create_therapist(
        session, therapist_data(full_name="Μαρία Παπά", license_no="TEST-7")
    ).item
    second = svc.create_therapist(
        session, therapist_data(full_name="ΜΑΡΙΑ  ΠΑΠΑ", license_no=" test-7 ")
    )
    assert second.item.id != first.id  # both exist
    assert [(w.code, w.ids) for w in second.warnings] == [
        ("duplicate_full_name", (first.id,)),
        ("duplicate_license_no", (first.id,)),
    ]
    unrelated = svc.create_therapist(
        session, therapist_data(full_name="Άλλος", license_no="TEST-8")
    )
    assert unrelated.warnings == []


def test_licence_number_warning_ignores_missing_numbers(session):
    svc.create_therapist(session, therapist_data(full_name="Α"))
    assert svc.create_therapist(session, therapist_data(full_name="Β")).warnings == []


def test_update_therapist_audits_field_names_only(session):
    row = svc.create_therapist(session, therapist_data(license_no="TEST-1")).item
    saved = svc.update_therapist(
        session, row.id, {"specialty": "Εργοθεραπεία", "license_no": "TEST-2", "license_date": None}
    )
    assert saved.item.specialty == "Εργοθεραπεία" and saved.item.license_no == "TEST-2"
    action, entity, entity_id, details = audit_rows(session)[-1]
    assert (action, entity, entity_id) == ("therapist.updated", "therapists", row.id)
    assert json.loads(details) == {"fields": ["license_no", "specialty"]}
    assert "TEST-2" not in details and "Εργοθεραπεία" not in details


def test_update_therapist_cannot_blank_the_name(session):
    row = svc.create_therapist(session, therapist_data()).item
    with pytest.raises(ReferenceDataError, match="full_name must not be empty"):
        svc.update_therapist(session, row.id, {"full_name": ""})


def test_audit_entries_never_contain_personal_values(session):
    row = svc.create_therapist(
        session, therapist_data(full_name="Μυστική Ονομασία", license_no="TEST-SECRET")
    ).item
    svc.update_therapist(session, row.id, {"full_name": "Άλλο Όνομα"})
    svc.set_therapist_active(session, row.id, False)
    dump = json.dumps([tuple(r) for r in audit_rows(session)], ensure_ascii=False)
    assert "Μυστική" not in dump and "TEST-SECRET" not in dump and "Άλλο" not in dump


def test_list_therapists_filters_and_orders(session):
    a = svc.create_therapist(session, therapist_data(full_name="Β")).item
    b = svc.create_therapist(session, therapist_data(full_name="Α")).item
    svc.set_therapist_active(session, a.id, False)
    assert [t.id for t in svc.list_therapists(session)] == [b.id, a.id]
    assert [t.id for t in svc.list_therapists(session, active=True)] == [b.id]
    assert [t.id for t in svc.list_therapists(session, active=False)] == [a.id]


def test_therapists_are_never_deleted_by_the_service():
    assert not [n for n in dir(svc) if "delete" in n.lower() or "remove" in n.lower()]


# ================================================================ active-template protection
@pytest.fixture()
def center(session, ref, store, upload):
    """Three types and three therapists, one active template using all of them, plus an
    unrelated type and therapist the template does not use."""
    template_id = activate_center_template(session, store, upload, ref)
    spare_type = f.therapy_type(session, "spare", "Άλλη:")
    spare_therapist = f.therapist(session, "Ανεξάρτητη Θεραπεύτρια")
    session.commit()
    return type(
        "Center",
        (),
        {
            "template_id": template_id,
            "ref": ref,
            "spare_type": spare_type,
            "spare_therapist": spare_therapist,
            "store": store,
        },
    )


def test_therapy_type_used_by_an_active_template_cannot_be_deactivated(session, center):
    tt = center.ref.types["speech"]
    with pytest.raises(ReferenceDataError) as info:
        svc.set_therapy_type_active(session, tt.id, False)
    err = info.value
    assert err.code == "in_use_by_active_template"
    assert "deactivate it first" in err.message
    assert [t["id"] for t in err.details["templates"]] == [center.template_id]
    session.rollback()
    assert session.get(TherapyType, tt.id).active is True


def test_therapist_used_by_an_active_template_cannot_be_deactivated(session, center):
    th = center.ref.therapists["occupational"]
    with pytest.raises(ReferenceDataError) as info:
        svc.set_therapist_active(session, th.id, False)
    assert info.value.code == "in_use_by_active_template"
    assert info.value.details["templates"][0]["id"] == center.template_id
    assert "deactivate" in info.value.message and "corrected template version" in info.value.message


@pytest.mark.parametrize(
    "change",
    [
        {"full_name": "Άλλο Όνομα"},
        {"license_no": "TEST-999"},
        {"license_authority": "Άλλη Αρχή"},
        {"license_date": D(2001, 1, 1)},
        {"license_no": None},
    ],
    ids=lambda c: next(iter(c)),
)
def test_identity_fields_are_frozen_while_an_active_template_uses_the_therapist(
    session, center, change
):
    th = center.ref.therapists["speech"]
    before = (th.full_name, th.license_no, th.license_authority, th.license_date)
    with pytest.raises(ReferenceDataError) as info:
        svc.update_therapist(session, th.id, change)
    assert info.value.code == "identity_locked"
    assert "deactivate it first" in info.value.message
    session.rollback()
    th = session.get(Therapist, th.id)
    assert (th.full_name, th.license_no, th.license_authority, th.license_date) == before


def test_other_therapist_fields_stay_editable_under_an_active_template(session, center):
    th = center.ref.therapists["speech"]
    saved = svc.update_therapist(session, th.id, {"specialty": "Ειδικότητα"})
    assert saved.item.specialty == "Ειδικότητα"
    # re-sending an identity field with its current value is not a change
    svc.update_therapist(session, th.id, {"full_name": th.full_name, "specialty": "Ειδ."})


def test_unrelated_records_are_not_blocked(session, center):
    svc.set_therapy_type_active(session, center.spare_type.id, False)
    svc.set_therapist_active(session, center.spare_therapist.id, False)
    svc.update_therapist(session, center.spare_therapist.id, {"full_name": "Νέο Όνομα"})
    svc.update_therapy_type(session, center.spare_type.id, {"name_el": "Νέο"})


def test_the_workflow_deactivate_template_change_then_reactivate(session, center):
    th = center.ref.therapists["speech"]
    with pytest.raises(ReferenceDataError):
        svc.update_therapist(session, th.id, {"license_no": "TEST-NEW"})
    session.rollback()
    deactivate_template(session, center.template_id)
    saved = svc.update_therapist(session, th.id, {"license_no": "TEST-NEW"})
    assert saved.item.license_no == "TEST-NEW"
    svc.set_therapist_active(session, th.id, False)
    svc.set_therapist_active(session, th.id, True)
    svc.set_therapy_type_active(session, center.ref.types["speech"].id, False)
    svc.set_therapy_type_active(session, center.ref.types["speech"].id, True)
    # the old template can no longer be activated with the old printed licence number
    from app.services.templates import TemplateValidationFailed, activate_template

    with pytest.raises(TemplateValidationFailed, match="licence number"):
        activate_template(session, center.store, center.template_id)


def test_the_error_names_every_active_template_that_uses_the_record(session, center, upload):
    from app.services.templates import activate_template, add_version, register_template
    from fixtures.eopyy_builder import DEFAULT_BLOCKS, build_eopyy_docx
    from fixtures.eopyy_maps import production_field_map

    only = DEFAULT_BLOCKS[:1]
    built = build_eopyy_docx(upload.path.parent / "single.docx", only)
    tpl = register_template(
        session,
        "Μόνο λογοθεραπεία",
        [(center.ref.types["speech"].id, center.ref.therapists["speech"].id)],
    ).template
    add_version(session, center.store, tpl.id, built.path, production_field_map(built))
    activate_template(session, center.store, tpl.id)
    with pytest.raises(ReferenceDataError) as info:
        svc.set_therapist_active(session, center.ref.therapists["speech"].id, False)
    names = [t["name"] for t in info.value.details["templates"]]
    assert names == sorted(names) and len(names) == 2
    assert "them" in info.value.message


# ---- the database backstop (the service pre-checks are bypassed with raw SQL)
def test_the_database_refuses_deactivation_under_an_active_template(session, center):
    with rejected(session, "used by an active template cannot be deactivated"):
        session.execute(
            text("UPDATE therapy_types SET active = 0 WHERE id = :i"),
            {"i": center.ref.types["speech"].id},
        )
    with rejected(session, "used by an active template cannot be deactivated"):
        session.execute(
            text("UPDATE therapists SET active = 0 WHERE id = :i"),
            {"i": center.ref.therapists["speech"].id},
        )


@pytest.mark.parametrize("column", ["full_name", "license_no", "license_authority", "license_date"])
def test_the_database_refuses_identity_changes_under_an_active_template(session, center, column):
    value = "'2001-01-01'" if column == "license_date" else "'changed'"
    with rejected(session, "cannot change identity details"):
        session.execute(
            text(f"UPDATE therapists SET {column} = {value} WHERE id = :i"),
            {"i": center.ref.therapists["speech"].id},
        )


def test_the_database_allows_the_same_changes_once_the_template_is_inactive(session, center):
    deactivate_template(session, center.template_id)
    tid = center.ref.therapists["speech"].id
    session.execute(text("UPDATE therapists SET full_name = 'changed' WHERE id = :i"), {"i": tid})
    session.execute(text("UPDATE therapists SET active = 0 WHERE id = :i"), {"i": tid})
    session.execute(
        text("UPDATE therapy_types SET active = 0 WHERE id = :i"),
        {"i": center.ref.types["speech"].id},
    )


def test_the_database_allows_reactivation_and_non_identity_edits_under_an_active_template(
    session, center
):
    tid = center.ref.therapists["speech"].id
    session.execute(text("UPDATE therapists SET specialty = 'x' WHERE id = :i"), {"i": tid})
    session.execute(text("UPDATE therapists SET active = 1 WHERE id = :i"), {"i": tid})  # no-op
    session.execute(
        text("UPDATE therapy_types SET template_label = 'Χ:' WHERE id = :i"),
        {"i": center.ref.types["speech"].id},
    )


def test_a_database_rule_that_slips_past_the_service_becomes_a_safe_conflict(
    session, center, monkeypatch
):
    monkeypatch.setattr(svc, "_refuse_while_in_active_template", lambda *a, **k: None)
    with pytest.raises(ReferenceDataError) as info:
        svc.set_therapist_active(session, center.ref.therapists["speech"].id, False)
    assert info.value.code == "in_use_by_active_template"
    session.rollback()  # as the request does after an error
    with pytest.raises(ReferenceDataError) as info:
        svc.update_therapist(session, center.ref.therapists["speech"].id, {"full_name": "Νέο"})
    assert info.value.code == "identity_locked"
    assert "Νέο" not in info.value.message


def test_an_unexpected_database_rule_is_a_generic_constraint_violation(session):
    row = svc.create_therapy_type(session, type_data()).item
    with pytest.raises(ReferenceDataError) as info:
        svc.update_therapy_type(session, row.id, {"eopyy_code": "53A"})  # CHECK digits only
    assert info.value.code == "constraint_violation"
    assert "53A" not in info.value.message


# ================================================================ template matching
def test_matching_refuses_an_inactive_therapist_even_if_the_template_is_active(session, center):
    pairs = [(center.ref.types[c].id, center.ref.therapists[c].id) for c in center.ref.types]
    assert match_template(session, center.store, pairs).template_id == center.template_id
    # force the impossible state (the guard trigger would normally prevent it)
    session.execute(text("DROP TRIGGER therapists_keep_active_templates"))
    session.execute(
        text("UPDATE therapists SET active = 0 WHERE id = :i"),
        {"i": center.ref.therapists["occupational"].id},
    )
    session.expire_all()  # raw SQL bypassed the ORM's cache
    assert session.get(DocumentTemplate, center.template_id).active is True
    with pytest.raises(NoMatchingTemplate, match="is inactive") as info:
        match_template(session, center.store, pairs)
    assert center.ref.therapists["occupational"].full_name not in str(info.value)  # by id only


def test_matching_refuses_an_inactive_therapy_type_even_if_the_template_is_active(session, center):
    pairs = [(center.ref.types[c].id, center.ref.therapists[c].id) for c in center.ref.types]
    session.execute(text("DROP TRIGGER therapy_types_keep_active_templates"))
    session.execute(
        text("UPDATE therapy_types SET active = 0 WHERE id = :i"),
        {"i": center.ref.types["speech"].id},
    )
    session.expire_all()
    with pytest.raises(NoMatchingTemplate, match="therapy .* is inactive"):
        match_template(session, center.store, pairs)


def test_matching_still_works_after_a_normal_edit_cycle(session, center):
    th = center.ref.therapists["speech"]
    svc.update_therapist(session, th.id, {"specialty": "νέα ειδικότητα"})
    pairs = [(center.ref.types[c].id, center.ref.therapists[c].id) for c in center.ref.types]
    assert match_template(session, center.store, pairs).template_id == center.template_id


# ================================================================ audit follow-ups
def test_the_period_therapies_branch_of_the_code_trigger_fires_in_the_database(session, db_path):
    """Raw SQL on a separate connection, so no service pre-check can intercept the change.

    A period therapy normally implies a prescription item of the same type, which would also
    trip the trigger. To prove the ``period_therapies`` branch by itself, the item is removed on
    the raw connection (foreign keys off, rolled back afterwards): only the period therapy is
    left referencing the type."""
    import sqlite3

    from tests.db.test_periods import setup_period

    pat, rx, items, thr, per, refs = setup_period(session)
    f.period_therapy(session, per, refs["speech"], thr)
    type_id = items["speech"].therapy_type_id
    session.commit()

    con = sqlite3.connect(db_path, isolation_level=None)
    try:
        con.execute("PRAGMA foreign_keys=OFF")
        con.execute("BEGIN")
        con.execute("DELETE FROM prescription_items WHERE therapy_type_id = ?", (type_id,))
        for table in ("template_configuration_items", "therapist_assignments"):
            assert con.execute(
                f"SELECT count(*) FROM {table} WHERE therapy_type_id = ?", (type_id,)
            ).fetchone() == (0,)
        assert con.execute(
            "SELECT count(*) FROM period_therapies WHERE therapy_type_id = ?", (type_id,)
        ).fetchone() == (1,)  # the only remaining reference
        with pytest.raises(sqlite3.IntegrityError, match="code cannot change once it is used"):
            con.execute("UPDATE therapy_types SET code = 'renamed' WHERE id = ?", (type_id,))
        # a change of any other column is still fine
        con.execute("UPDATE therapy_types SET name_el = 'Νέο' WHERE id = ?", (type_id,))
        con.execute("ROLLBACK")
    finally:
        con.close()
    session.expire_all()
    assert session.get(TherapyType, type_id).code == "speech"


@pytest.mark.parametrize("record", ["therapist", "therapy type"])
def test_a_raw_reactivation_of_a_template_with_an_inactive_record_is_allowed_but_never_matched(
    session, center, record
):
    """Documented defence in depth (no fifth trigger, by decision): the database lets a template
    be switched back on while one of its therapists or therapy types is inactive, because the
    template triggers only look at the template's own version. The authoritative paths refuse it:
    template activation and template matching."""
    from app.services.templates import TemplateValidationFailed, activate_template

    pairs = [(center.ref.types[c].id, center.ref.therapists[c].id) for c in center.ref.types]
    deactivate_template(session, center.template_id)
    if record == "therapist":
        svc.set_therapist_active(session, center.ref.therapists["speech"].id, False)
    else:
        svc.set_therapy_type_active(session, center.ref.types["speech"].id, False)
    session.commit()  # the setup must survive the refused activation below

    # 1. the service-level activation refuses
    with pytest.raises(TemplateValidationFailed, match="is inactive"):
        activate_template(session, center.store, center.template_id)
    assert session.get(DocumentTemplate, center.template_id).active is False

    # 2. the database itself currently permits the raw write
    session.execute(
        text("UPDATE document_templates SET active = 1 WHERE id = :i"), {"i": center.template_id}
    )
    session.flush()
    session.expire_all()
    assert session.get(DocumentTemplate, center.template_id).active is True

    # 3. but matching refuses to use that template
    with pytest.raises(NoMatchingTemplate, match="is inactive"):
        match_template(session, center.store, pairs)


@pytest.fixture()
def booked(session, center):
    """A patient with the three therapies of the active template, both as a document period
    (``period_therapies``) and as current assignments, on exactly the template's therapists."""
    pat = f.patient(session, amka="22222222222")
    rx = f.prescription(session, pat, D(2025, 9, 16), D(2026, 3, 15))
    window = (D(2025, 10, 16), D(2025, 11, 15))
    per = f.period(session, pat, *window)
    for code, th in center.ref.therapists.items():
        tt = center.ref.types[code]
        item = f.item(session, rx, tt, 8)
        referral = f.referral(session, item, 1, *window, 8)
        f.period_therapy(session, per, referral, th)
        f.add(
            session,
            TherapistAssignment(
                patient_id=pat.id,
                therapy_type_id=tt.id,
                therapist_id=th.id,
                valid_from=D(2025, 1, 1),
            ),
        )
    session.commit()
    return type("Booked", (), {"patient": pat, "period": per})


def _force_inactive(session, center, record):
    """The state the guards normally prevent: an *active* template whose record is inactive."""
    if record == "therapist":
        session.execute(text("DROP TRIGGER therapists_keep_active_templates"))
        session.execute(
            text("UPDATE therapists SET active = 0 WHERE id = :i"),
            {"i": center.ref.therapists["occupational"].id},
        )
    else:
        session.execute(text("DROP TRIGGER therapy_types_keep_active_templates"))
        session.execute(
            text("UPDATE therapy_types SET active = 0 WHERE id = :i"),
            {"i": center.ref.types["occupational"].id},
        )
    session.expire_all()
    assert session.get(DocumentTemplate, center.template_id).active is True


@pytest.mark.parametrize("record", ["therapist", "therapy type"])
def test_match_for_period_refuses_an_inactive_record(session, center, booked, record):
    from app.services.templates import match_for_period

    assert (
        match_for_period(session, center.store, booked.period.id).template_id == center.template_id
    )  # control: it matches while everything is active
    _force_inactive(session, center, record)
    with pytest.raises(NoMatchingTemplate, match="is inactive"):
        match_for_period(session, center.store, booked.period.id)


@pytest.mark.parametrize("record", ["therapist", "therapy type"])
def test_match_for_patient_refuses_an_inactive_record(session, center, booked, record):
    from app.services.templates import match_for_patient

    assert (
        match_for_patient(session, center.store, booked.patient.id).template_id
        == center.template_id
    )
    _force_inactive(session, center, record)
    with pytest.raises(NoMatchingTemplate, match="is inactive"):
        match_for_patient(session, center.store, booked.patient.id)


# ---------------------------------------------------------------- M5.2: semantics pinned
def test_m51_duplicate_name_semantics_are_unchanged_by_the_patient_fold(session):
    """M5.2 added ``app.text.therapy_fold`` for patients (hyphens and punctuation become word
    separators). The reference-data fold must keep its M5.1 behaviour: case, accents and spacing
    are ignored, but a hyphen is part of the name."""
    assert svc._fold("Άννα  ΜΑΡΊΑ ") == svc._fold("άννα μαρία")
    assert svc._fold("Άννα-Μαρία") != svc._fold("Άννα Μαρία")
    first = svc.create_therapist(session, {"full_name": "Άννα-Μαρία Δοκιμής"}).item
    hyphen_vs_space = svc.create_therapist(session, {"full_name": "Άννα Μαρία Δοκιμής"})
    assert hyphen_vs_space.warnings == []
    same = svc.create_therapist(session, {"full_name": "ΑΝΝΑ-ΜΑΡΙΑ  δοκιμής"})
    assert [(w.code, w.ids) for w in same.warnings] == [("duplicate_full_name", (first.id,))]


def test_m51_audit_entries_are_unchanged_by_the_shared_helper(session):
    row = svc.create_therapy_type(
        session, {"code": "pin", "name_el": "Α", "template_label": "Α:"}
    ).item
    svc.update_therapy_type(session, row.id, {"name_el": "Β"})
    entries = session.execute(
        text("SELECT action, entity, entity_id, details FROM audit_log ORDER BY id")
    ).all()
    assert [tuple(e) for e in entries] == [
        ("therapy_type.created", "therapy_types", row.id, None),
        ("therapy_type.updated", "therapy_types", row.id, json.dumps({"fields": ["name_el"]})),
    ]
