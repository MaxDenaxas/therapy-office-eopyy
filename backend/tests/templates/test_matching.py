"""Exact template matching (M4): one active valid template or an explicit error."""

from __future__ import annotations

import stat
from datetime import date

import pytest
from sqlalchemy import select, text

from app.models import DocumentTemplate, GeneratedDocument, TherapistAssignment
from app.services.templates import (
    AmbiguousTemplates,
    NoMatchingTemplate,
    activate_template,
    add_version,
    deactivate_template,
    match_for_patient,
    match_for_period,
    match_template,
    register_template,
)
from fixtures import eopyy_builder as fx
from fixtures.eopyy_maps import production_field_map
from tests.db import factories as f
from tests.templates.conftest import pairs_for

D = date
OCT_START, OCT_END = D(2025, 10, 16), D(2025, 11, 15)
SPEECH_ONLY = (fx.TherapyBlock("speech", "Λογοθεραπεία:", "T1", "TEST-101/10"),)


def active_template(
    session,
    store,
    ref,
    tmp_path,
    name,
    codes,
    blocks=fx.DEFAULT_BLOCKS,
    therapist_override=None,
    **build,
):
    built = fx.build_eopyy_docx(tmp_path / f"{name}.docx", blocks, **build)
    pairs = pairs_for(ref, codes)
    if therapist_override:
        pairs = [(t, therapist_override.get(t, th)) for t, th in pairs]
    template = register_template(session, name, pairs).template
    add_version(session, store, template.id, built.path, production_field_map(built))
    activate_template(session, store, template.id)
    session.commit()
    return template


# ---------------------------------------------------------------- exact match
def test_exact_match_returns_the_verified_current_version(session, store, ref, tmp_path):
    template = active_template(
        session, store, ref, tmp_path, "Όλα", ("speech", "occupational", "physiotherapy")
    )
    match = match_template(session, store, list(reversed(pairs_for(ref))))  # order irrelevant
    assert match.template_id == template.id and match.version_id == template.current_version_id
    assert match.path.is_file() and match.field_map.template_sha256 == match.sha256
    assert {b.therapy for b in match.field_map.blocks} == {
        "speech",
        "occupational",
        "physiotherapy",
    }


def test_unsupported_pairs_never_match(session, store, ref, tmp_path):
    template = active_template(
        session,
        store,
        ref,
        tmp_path,
        "Λογο+Εργο",
        ("speech", "occupational"),
        fx.DEFAULT_BLOCKS[:2],
    )
    # exact matching (D-28): the full combination matches ...
    match = match_template(session, store, pairs_for(ref, ("speech", "occupational")))
    assert match.template_id == template.id
    assert match.selected_therapies == ("occupational", "speech")
    # ... a subset or a superset never does
    with pytest.raises(NoMatchingTemplate, match="no active template for exactly"):
        match_template(session, store, pairs_for(ref, ("speech",)))
    with pytest.raises(NoMatchingTemplate, match="no active template for exactly"):
        match_template(session, store, pairs_for(ref))  # adds physiotherapy
    other = f.therapist(session, "Άλλη Θεραπεύτρια")
    pairs = pairs_for(ref, ("speech", "occupational"))
    pairs[0] = (pairs[0][0], other.id)  # same therapies, different therapist
    with pytest.raises(NoMatchingTemplate, match="Άλλη Θεραπεύτρια"):
        match_template(session, store, pairs)


def test_inactive_templates_are_never_used(session, store, ref, tmp_path):
    template = active_template(session, store, ref, tmp_path, "Λογο", ("speech",), SPEECH_ONLY)
    deactivate_template(session, template.id)
    session.commit()
    with pytest.raises(NoMatchingTemplate):
        match_template(session, store, pairs_for(ref, ("speech",)))


def test_template_without_a_valid_version_is_never_used(session, store, ref):
    register_template(session, "Κενό", pairs_for(ref, ("speech",)))
    session.commit()
    with pytest.raises(NoMatchingTemplate):
        match_template(session, store, pairs_for(ref, ("speech",)))


def test_ambiguity_is_an_explicit_error(session, store, ref, tmp_path):
    """The database already prevents two active templates per combination; if that ever
    failed (here: the index is dropped), matching refuses instead of picking one."""
    first = active_template(session, store, ref, tmp_path, "Α", ("speech",), SPEECH_ONLY)
    session.execute(text("DROP INDEX uq_document_templates_one_active_per_configuration"))
    built = fx.build_eopyy_docx(tmp_path / "b.docx", SPEECH_ONLY, sample_dates=True)
    second = register_template(session, "Β", pairs_for(ref, ("speech",))).template
    version = add_version(session, store, second.id, built.path, production_field_map(built))
    session.execute(
        text("UPDATE document_templates SET current_version_id = :v, active = 1 WHERE id = :t"),
        {"v": version.version.id, "t": second.id},
    )
    session.commit()
    with pytest.raises(AmbiguousTemplates, match="Α, Β"):
        match_template(session, store, pairs_for(ref, ("speech",)))
    assert first.active


def test_altered_template_file_is_never_used(session, store, ref, tmp_path):
    template = active_template(session, store, ref, tmp_path, "Λογο", ("speech",), SPEECH_ONLY)
    match = match_template(session, store, pairs_for(ref, ("speech",)))
    match.path.chmod(stat.S_IWRITE | stat.S_IREAD)
    match.path.write_bytes(b"tampered")
    with pytest.raises(NoMatchingTemplate, match="altered"):
        match_template(session, store, pairs_for(ref, ("speech",)))
    assert template.active  # nothing changed in the database


def test_invalid_input(session, store, ref):
    from app.services.templates import InvalidConfiguration

    with pytest.raises(InvalidConfiguration):
        match_template(session, store, [])
    with pytest.raises(LookupError):
        match_for_period(session, store, 999)


# ---------------------------------------------------------------- periods, patients, history
def speech_period(
    session,
    ref,
    therapist,
    start=OCT_START,
    end=OCT_END,
    amka="11111111111",
    patient=None,
):
    patient = patient or f.patient(session, amka=amka)
    rx = f.prescription(session, patient, D(2025, 9, 16), D(2026, 3, 15))
    ref_period = f.referral(session, f.item(session, rx, ref.types["speech"], 8), 1, start, end, 8)
    period = f.period(session, patient, start, end)
    f.period_therapy(session, period, ref_period, therapist)
    session.commit()
    return patient, period


def test_period_matching_uses_the_period_snapshot(session, store, ref, tmp_path):
    template = active_template(session, store, ref, tmp_path, "Λογο", ("speech",), SPEECH_ONLY)
    _, period = speech_period(session, ref, ref.therapists["speech"])
    assert match_for_period(session, store, period.id).template_id == template.id


def test_therapist_reassignment_uses_the_new_template_and_keeps_history(
    session, store, ref, tmp_path
):
    old_t = ref.therapists["speech"]
    old = active_template(session, store, ref, tmp_path, "Λογο Τ1", ("speech",), SPEECH_ONLY)
    patient, october = speech_period(session, ref, old_t)
    session.add(
        TherapistAssignment(
            patient_id=patient.id,
            therapy_type_id=ref.types["speech"].id,
            therapist_id=old_t.id,
            valid_from=D(2025, 9, 16),
        )
    )
    session.commit()
    first_match = match_for_period(session, store, october.id)
    f.document(session, october, _version(session, first_match.version_id))
    session.commit()

    # reassignment: a new therapist with her own template
    new_t = f.therapist(
        session,
        "Θεραπευτής T9",
        license_no="TEST-201/10",
        license_authority="Νομαρχία Δοκιμής",
        license_date=D(2010, 1, 1),
    )
    new_blocks = (fx.TherapyBlock("speech", "Λογοθεραπεία:", "T9", "TEST-201/10"),)
    built = fx.build_eopyy_docx(tmp_path / "new.docx", new_blocks)
    new = register_template(session, "Λογο Τ9", [(ref.types["speech"].id, new_t.id)]).template
    add_version(session, store, new.id, built.path, production_field_map(built))
    activate_template(session, store, new.id)
    assignment = session.scalars(select(TherapistAssignment)).one()
    assignment.valid_to = D(2025, 11, 15)
    session.add(
        TherapistAssignment(
            patient_id=patient.id,
            therapy_type_id=ref.types["speech"].id,
            therapist_id=new_t.id,
            valid_from=D(2025, 11, 16),
        )
    )
    session.commit()

    assert match_for_patient(session, store, patient.id).template_id == new.id
    _, november = speech_period(
        session, ref, new_t, D(2025, 11, 16), D(2025, 12, 15), patient=patient
    )
    assert match_for_period(session, store, november.id).template_id == new.id
    assert match_for_period(session, store, october.id).template_id == old.id  # its snapshot
    issued = session.scalars(select(GeneratedDocument)).one()
    assert issued.template_version_id == first_match.version_id  # history untouched


def _version(session, version_id):
    from app.models import TemplateVersion

    return session.get(TemplateVersion, version_id)


def test_patient_without_assignments(session, store, ref):
    patient = f.patient(session)
    session.commit()
    with pytest.raises(NoMatchingTemplate, match="no current therapist assignments"):
        match_for_patient(session, store, patient.id)


def test_active_flag_is_the_only_switch(session, store, ref, tmp_path):
    """Deactivating and re-activating keeps the same version; no hidden fallback."""
    template = active_template(session, store, ref, tmp_path, "Λογο", ("speech",), SPEECH_ONLY)
    version_id = template.current_version_id
    deactivate_template(session, template.id)
    activate_template(session, store, template.id)
    session.commit()
    assert match_template(session, store, pairs_for(ref, ("speech",))).version_id == version_id
    assert session.scalars(select(DocumentTemplate)).one().active
