"""Template registration, versions, validation, activation (M4)."""

from __future__ import annotations

import json
import os
import stat

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.db import session_scope
from app.documents import TemplateFileError, sha256_file
from app.models import AuditLog, DocumentTemplate, TemplateConfigurationItem, TemplateVersion
from app.services.templates import (
    DuplicateTemplateFile,
    InvalidConfiguration,
    InvalidFieldMap,
    TemplateConflict,
    TemplateValidationFailed,
    activate_template,
    add_version,
    configuration_key,
    deactivate_template,
    register_template,
)
from fixtures import eopyy_builder as fx
from fixtures.eopyy_maps import production_field_map
from tests.templates.conftest import pairs_for


def new_template(
    session, ref, name="Λογο+Εργο+Φυσικο", codes=("speech", "occupational", "physiotherapy")
):
    return register_template(session, name, pairs_for(ref, codes)).template


def versions(session, template):
    return session.scalars(
        select(TemplateVersion)
        .where(TemplateVersion.template_id == template.id)
        .order_by(TemplateVersion.version_no)
    ).all()


# ---------------------------------------------------------------- registration
def test_register_creates_an_inactive_template_with_its_configuration(session, ref):
    template = new_template(session, ref)
    session.commit()
    assert not template.active and template.current_version_id is None
    items = session.scalars(select(TemplateConfigurationItem)).all()
    assert sorted((i.therapy_type_id, i.therapist_id) for i in items) == sorted(pairs_for(ref))
    assert template.configuration_key == configuration_key(pairs_for(ref))


def test_configuration_key_is_canonical():
    assert (
        configuration_key([(3, 9), (1, 7)])
        == configuration_key([(1, 7), (3, 9), (1, 7)])
        == "1:7,3:9"
    )
    with pytest.raises(InvalidConfiguration, match="at least one"):
        configuration_key([])
    with pytest.raises(InvalidConfiguration, match="only once"):
        configuration_key([(1, 7), (1, 8)])


@pytest.mark.parametrize(
    "name, pairs, match",
    [
        ("  ", None, "needs a name"),
        ("Χ", [(999, 1)], "therapy type #999"),
        ("Χ", [(1, 999)], "therapist #999"),
    ],
)
def test_invalid_registrations(session, ref, name, pairs, match):
    with pytest.raises(InvalidConfiguration, match=match):
        register_template(session, name, pairs if pairs is not None else pairs_for(ref))


def test_duplicate_names_are_refused_and_same_combination_is_reported(session, ref):
    new_template(session, ref, name="Α")
    with pytest.raises(InvalidConfiguration, match="already exists"):
        new_template(session, ref, name="Α")
    session.rollback()
    new_template(session, ref, name="Α")
    second = register_template(session, "Β", pairs_for(ref))
    assert any("same combination" in w for w in second.warnings)


# ---------------------------------------------------------------- versions
def test_add_valid_version_stores_an_immutable_copy(session, store, ref, upload):
    template = new_template(session, ref)
    before = upload.path.read_bytes()
    result = add_version(session, store, template.id, upload.path, production_field_map(upload))
    session.commit()
    v = result.version
    assert result.valid and v.version_no == 1 and v.validation_status == "valid"
    assert v.sha256 == sha256_file(upload.path) and v.storage_path == f"templates/{v.sha256}.docx"
    stored = store.verify(v.storage_path, v.sha256)
    assert not os.access(stored, os.W_OK)
    assert upload.path.read_bytes() == before  # the upload is only read
    assert v.original_filename == upload.path.name
    assert json.loads(v.field_map)["template_sha256"] == v.sha256
    assert json.loads(v.validation_report) == {"errors": [], "warnings": []}


def test_invalid_docx_is_rejected_without_traces(session, store, ref, tmp_path):
    template = new_template(session, ref)
    bad = tmp_path / "bad.docx"
    bad.write_bytes(b"PK\x03\x04 broken")
    fmap = production_field_map(fx.build_eopyy_docx(tmp_path / "x.docx"))
    with pytest.raises(TemplateFileError):
        add_version(session, store, template.id, bad, fmap)
    assert versions(session, template) == []
    assert list((store.root / "templates").iterdir()) == []


def test_field_map_must_be_valid_and_for_this_file(session, store, ref, upload, tmp_path):
    template = new_template(session, ref)
    other = fx.build_eopyy_docx(tmp_path / "other.docx", sample_dates=True)
    with pytest.raises(InvalidFieldMap, match="different file"):
        add_version(session, store, template.id, upload.path, production_field_map(other))
    with pytest.raises(InvalidFieldMap, match="not valid"):
        add_version(session, store, template.id, upload.path, {"template_sha256": "x"})
    raw = production_field_map(upload).model_dump(mode="json")
    del raw["ada"]["mode"]  # D-25: mode must be stated
    with pytest.raises(InvalidFieldMap, match="mode"):
        add_version(session, store, template.id, upload.path, raw)
    assert versions(session, template) == []


def test_field_map_accepted_as_json_text(session, store, ref, upload):
    template = new_template(session, ref)
    result = add_version(
        session, store, template.id, upload.path, production_field_map(upload).model_dump_json()
    )
    assert result.valid


def test_the_same_file_cannot_be_used_twice(session, store, ref, upload):
    first = new_template(session, ref, name="Α")
    add_version(session, store, first.id, upload.path, production_field_map(upload))
    with pytest.raises(DuplicateTemplateFile, match="identical to version 1"):
        add_version(session, store, first.id, upload.path, production_field_map(upload))
    second = new_template(session, ref, name="Β")
    with pytest.raises(DuplicateTemplateFile, match="already registered for template"):
        add_version(session, store, second.id, upload.path, production_field_map(upload))


def test_sections_must_match_the_configuration(session, store, ref, upload):
    template = new_template(session, ref, codes=("speech", "occupational"))
    result = add_version(session, store, template.id, upload.path, production_field_map(upload))
    assert not result.valid and result.version.validation_status == "invalid"
    assert any("'physiotherapy', which is not configured" in e for e in result.errors)
    with pytest.raises(TemplateValidationFailed, match="not configured"):
        activate_template(session, store, template.id, result.version.id)


def test_licence_numbers_are_cross_checked(session, store, ref, upload):
    ref.therapists["occupational"].license_no = "TEST-999/99"
    session.commit()
    template = new_template(session, ref)
    result = add_version(session, store, template.id, upload.path, production_field_map(upload))
    assert not result.valid
    assert any(
        "prints licence number 'TEST-102/10'" in e and "TEST-999/99" in e for e in result.errors
    )


def test_missing_details_on_record_block_the_version(session, store, ref, upload):
    """D-27: a printed detail that cannot be checked against the record is an error."""
    ref.therapists["speech"].license_no = None
    ref.therapists["occupational"].license_authority = None
    session.commit()
    template = new_template(session, ref)
    result = add_version(session, store, template.id, upload.path, production_field_map(upload))
    assert not result.valid
    assert any("prints the licence number" in e and "none on record" in e for e in result.errors)
    assert any("prints the issuing authority" in e for e in result.errors)


def test_static_and_dynamic_center_fields(session, store, ref, upload, tmp_path):
    template = new_template(session, ref)
    static = add_version(
        session, store, template.id, upload.path, production_field_map(upload, center_mode="static")
    )
    assert static.valid
    # dynamic fields with the center settings not filled in: valid, but warned
    from app.models import CenterSettings

    settings = session.get(CenterSettings, 1)
    settings.ada = None
    session.flush()
    other = fx.build_eopyy_docx(tmp_path / "o.docx", sample_dates=True)
    dynamic = add_version(session, store, template.id, other.path, production_field_map(other))
    assert dynamic.valid
    assert any("ada is dynamic" in w and "center settings" in w for w in dynamic.warnings)


def test_blank_static_field_makes_the_version_invalid(session, store, ref, tmp_path):
    from docx import Document
    from docx.oxml.ns import qn

    built = fx.build_eopyy_docx(tmp_path / "t.docx")
    doc = Document(str(built.path))
    doc.paragraphs[built.layout.ada_paragraph].runs[0]._r.find(qn("w:t")).text = "ΑΔΑ: "
    doc.save(str(built.path))
    template = new_template(session, ref)
    result = add_version(
        session, store, template.id, built.path, production_field_map(built, center_mode="static")
    )
    assert not result.valid and any("static but is blank" in e for e in result.errors)


# ---------------------------------------------------------------- activation
def test_activate_and_deactivate(session, store, ref, upload):
    template = new_template(session, ref)
    v1 = add_version(session, store, template.id, upload.path, production_field_map(upload))
    result = activate_template(session, store, template.id)
    session.commit()
    assert template.active and template.current_version_id == v1.version.id
    assert result.version.id == v1.version.id
    deactivate_template(session, template.id)
    session.commit()
    assert not template.active and template.current_version_id == v1.version.id  # kept
    actions = session.scalars(
        select(AuditLog.action).where(AuditLog.entity == "document_templates")
    ).all()
    assert actions == [
        "template.registered",
        "template.version_added",
        "template.activated",
        "template.deactivated",
    ]


def test_activation_needs_a_valid_version(session, store, ref):
    template = new_template(session, ref)
    with pytest.raises(TemplateValidationFailed, match="no valid version"):
        activate_template(session, store, template.id)


def test_conflicting_active_templates_are_refused(session, store, ref, upload, tmp_path):
    first = new_template(session, ref, name="Α")
    add_version(session, store, first.id, upload.path, production_field_map(upload))
    activate_template(session, store, first.id)
    second = new_template(session, ref, name="Β")
    other = fx.build_eopyy_docx(tmp_path / "b.docx", sample_dates=True)
    add_version(session, store, second.id, other.path, production_field_map(other))
    with pytest.raises(TemplateConflict, match="'Α' is already active.*deactivate it first"):
        activate_template(session, store, second.id)
    deactivate_template(session, first.id)
    activate_template(session, store, second.id)  # replacement by another template
    session.commit()
    assert not first.active and second.active


def test_activation_rechecks_the_stored_file(session, store, ref, upload):
    template = new_template(session, ref)
    v = add_version(session, store, template.id, upload.path, production_field_map(upload))
    session.commit()
    stored = store.resolve(v.version.storage_path)
    stored.chmod(stat.S_IWRITE | stat.S_IREAD)
    stored.write_bytes(stored.read_bytes() + b"\x00")
    with pytest.raises(TemplateValidationFailed, match="altered"):
        activate_template(session, store, template.id)
    assert not template.active


def test_activation_rechecks_current_data(session, store, ref, upload):
    template = new_template(session, ref)
    add_version(session, store, template.id, upload.path, production_field_map(upload))
    ref.therapists["physiotherapy"].active = False
    session.commit()
    with pytest.raises(TemplateValidationFailed, match="is inactive"):
        activate_template(session, store, template.id)


def test_explicit_invalid_version_cannot_be_activated(session, store, ref, upload):
    template = new_template(session, ref, codes=("speech",))
    bad = add_version(session, store, template.id, upload.path, production_field_map(upload))
    with pytest.raises(TemplateValidationFailed) as info:
        activate_template(session, store, template.id, bad.version.id)
    assert info.value.errors and "not configured" in " ".join(info.value.errors)


# ---------------------------------------------------------------- versions over time
def test_new_versions_never_change_old_ones(session, store, ref, upload, tmp_path):
    template = new_template(session, ref)
    v1 = add_version(session, store, template.id, upload.path, production_field_map(upload))
    activate_template(session, store, template.id)
    session.commit()
    snapshot = {c.name: getattr(v1.version, c.name) for c in TemplateVersion.__table__.columns}

    revised = fx.build_eopyy_docx(tmp_path / "v2.docx", sample_dates=True)
    v2 = add_version(session, store, template.id, revised.path, production_field_map(revised))
    session.commit()
    assert v2.version.version_no == 2 and template.current_version_id == v1.version.id
    activate_template(session, store, template.id, v2.version.id)  # switch while active
    session.commit()
    assert template.active and template.current_version_id == v2.version.id

    session.expire_all()
    again = session.get(TemplateVersion, v1.version.id)
    assert {c.name: getattr(again, c.name) for c in TemplateVersion.__table__.columns} == snapshot
    store.verify(again.storage_path, again.sha256)  # old file still there, unchanged
    with pytest.raises(IntegrityError, match="template version cannot be modified"):
        session.execute(
            text("UPDATE template_versions SET storage_path = 'x' WHERE id = :id"),
            {"id": v1.version.id},
        )
    session.rollback()
    activate_template(session, store, template.id, v1.version.id)  # rolling back is allowed
    session.commit()
    assert template.current_version_id == v1.version.id


def test_configuration_is_frozen_once_a_version_exists(session, store, ref, upload):
    template = new_template(session, ref)
    add_version(session, store, template.id, upload.path, production_field_map(upload))
    session.commit()
    with pytest.raises(IntegrityError, match="configuration is fixed"):
        session.execute(
            text("DELETE FROM template_configuration_items WHERE template_id = :t"),
            {"t": template.id},
        )


# ---------------------------------------------------------------- transactions / privacy
def test_failure_rolls_back_the_database(engine, session, store, ref, upload):
    from sqlalchemy.orm import sessionmaker

    pairs = pairs_for(ref)
    session.close()  # release the fixture's connection: every transaction takes the write lock
    factory = sessionmaker(engine, expire_on_commit=False)
    with pytest.raises(RuntimeError, match="simulated"), session_scope(factory) as s:
        template = register_template(s, "Προσωρινό", pairs).template
        add_version(s, store, template.id, upload.path, production_field_map(upload))
        raise RuntimeError("simulated failure after the version was written")
    with session_scope(factory) as s:
        assert s.scalars(select(DocumentTemplate)).all() == []
        assert s.scalars(select(TemplateVersion)).all() == []
        # the stored file is content-addressed: a retry reuses it
        template = register_template(s, "Προσωρινό", pairs).template
        assert add_version(s, store, template.id, upload.path, production_field_map(upload)).valid


def test_audit_entries_hold_ids_only(session, store, ref, upload):
    template = new_template(session, ref)
    add_version(session, store, template.id, upload.path, production_field_map(upload))
    session.commit()
    details = " ".join(session.scalars(select(AuditLog.details)).all())
    assert "Θεραπευτής" not in details and "ΠΛΑΣΤΟ" not in details
    assert upload.path.name not in details
