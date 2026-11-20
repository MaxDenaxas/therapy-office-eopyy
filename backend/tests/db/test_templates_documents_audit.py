"""Template versions, issued documents and the audit log: append-only behaviour."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from app.models import AuditLog
from tests.db import factories as f
from tests.db.conftest import rejected
from tests.db.test_periods import setup_period


def therapy_setup(session):
    speech = f.therapy_type(session, "speech", "Λογοθεραπεία:")
    thr = f.therapist(session)
    return speech, thr


# ---------------------------------------------------------------- template versions
def test_template_versions_cannot_be_modified_or_deleted(session):
    tpl = f.template(session, "Λογοθεραπεία Τ1", "1:1")
    tv = f.version(session, tpl)
    session.commit()
    with rejected(session, "template version cannot be modified"):
        session.execute(
            text("UPDATE template_versions SET storage_path = 'x' WHERE id = :id"), {"id": tv.id}
        )
    with rejected(session, "template version cannot be modified"):
        session.execute(
            text("UPDATE template_versions SET validation_status = 'valid' WHERE id = :id"),
            {"id": tv.id},
        )
    with rejected(session, "template version cannot be deleted"):
        session.execute(text("DELETE FROM template_versions WHERE id = :id"), {"id": tv.id})


def test_new_version_keeps_the_old_one(session):
    tpl = f.template(session, "Λογοθεραπεία Τ1", "1:1")
    v1, v2 = f.version(session, tpl, 1), f.version(session, tpl, 2)
    session.execute(
        text("UPDATE document_templates SET current_version_id = :v WHERE id = :t"),
        {"v": v1.id, "t": tpl.id},
    )
    session.execute(
        text("UPDATE document_templates SET current_version_id = :v WHERE id = :t"),
        {"v": v2.id, "t": tpl.id},
    )
    session.commit()
    assert session.execute(text("SELECT count(*) FROM template_versions")).scalar() == 2
    with rejected(session, "UNIQUE constraint failed: template_versions.template_id"):
        f.version(session, tpl, 2)


def test_version_fields_are_validated(session):
    tpl = f.template(session, "Τ", "1:1")
    for column, value, match in [
        ("sha256", "XYZ", "sha256_sha256"),
        ("sha256", "A" * 64, "sha256_sha256"),  # upper case is not canonical
        ("field_map", "{broken", "field_map_json"),
        ("validation_status", "pending", "validation_status_allowed"),
        ("size_bytes", 0, "size_positive"),
    ]:
        values = {
            "template_id": tpl.id,
            "version_no": 1,
            "original_filename": "a.docx",
            "sha256": f.sha("a"),
            "storage_path": "templates/a.docx",
            "size_bytes": 10,
            "field_map": "{}",
            "field_map_schema_version": 1,
            "validation_status": "valid",
        }
        values[column] = value
        cols = ", ".join(values)
        params = ", ".join(f":{k}" for k in values)
        with rejected(session, match):
            session.execute(
                text(f"INSERT INTO template_versions ({cols}) VALUES ({params})"), values
            )
        tpl = f.template(session, "Τ", "1:1")


def test_current_version_must_be_a_valid_version_of_the_same_template(session):
    a = f.template(session, "Α", "1:1")
    b = f.template(session, "Β", "2:2")
    va = f.version(session, a)
    invalid_b = f.version(session, b, status="invalid")
    session.commit()
    for tpl, ver in [(b, va), (b, invalid_b)]:
        with rejected(session, "current version must be a valid version of this template"):
            session.execute(
                text("UPDATE document_templates SET current_version_id = :v WHERE id = :t"),
                {"v": ver.id, "t": tpl.id},
            )


def test_active_template_needs_a_current_version(session):
    tpl = f.template(session, "Α", "1:1")
    with rejected(session, "only be active with a valid current version"):
        session.execute(
            text("UPDATE document_templates SET active = 1 WHERE id = :id"), {"id": tpl.id}
        )


def test_only_one_active_template_per_configuration(session):
    a = f.template(session, "Α", "1:1,4:2")
    b = f.template(session, "Β", "1:1,4:2")
    for tpl in (a, b):
        v = f.version(session, tpl)
        session.execute(
            text("UPDATE document_templates SET current_version_id = :v WHERE id = :t"),
            {"v": v.id, "t": tpl.id},
        )
    session.execute(text("UPDATE document_templates SET active = 1 WHERE id = :id"), {"id": a.id})
    session.commit()
    with rejected(session, "UNIQUE constraint failed: document_templates.configuration_key"):
        session.execute(
            text("UPDATE document_templates SET active = 1 WHERE id = :id"), {"id": b.id}
        )
    # deactivating the first lets the second become active (replacement by another template)
    session.execute(text("UPDATE document_templates SET active = 0 WHERE id = :id"), {"id": a.id})
    session.execute(text("UPDATE document_templates SET active = 1 WHERE id = :id"), {"id": b.id})


def test_configuration_is_frozen_once_a_version_exists(session):
    speech, thr = therapy_setup(session)
    other = f.therapist(session, "Άλλη")
    tpl = f.template(session, "Α", f"{speech.id}:{thr.id}", [(speech.id, thr.id)])
    tpl.configuration_key = f"{speech.id}:{other.id}"  # editable while there is no version
    session.execute(
        text("UPDATE template_configuration_items SET therapist_id = :o WHERE template_id = :t"),
        {"o": other.id, "t": tpl.id},
    )
    session.flush()
    f.version(session, tpl)
    session.commit()
    for sql in [
        "UPDATE template_configuration_items SET therapist_id = :thr WHERE template_id = :t",
        "DELETE FROM template_configuration_items WHERE template_id = :t",
        "INSERT INTO template_configuration_items (template_id, therapy_type_id, therapist_id) "
        "VALUES (:t, :tt, :thr)",
        "UPDATE document_templates SET configuration_key = 'x' WHERE id = :t",
    ]:
        with rejected(session, "configuration is fixed once a version exists"):
            session.execute(text(sql), {"t": tpl.id, "thr": thr.id, "tt": speech.id + 100})


# ---------------------------------------------------------------- issued documents
def issued_document(session):
    _, _, _, thr, per, refs = setup_period(session)
    f.period_therapy(session, per, refs["speech"], thr)
    tpl = f.template(session, "Πρότυπο", "1:1")
    tv = f.version(session, tpl)
    doc = f.document(session, per, tv)
    session.execute(text("UPDATE periods SET status = 'issued' WHERE id = :id"), {"id": per.id})
    session.commit()
    return per, tv, doc


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE generated_documents SET issue_date = '2000-01-01' WHERE id = :id",
        "UPDATE generated_documents SET snapshot = '{}' WHERE id = :id",
        "UPDATE generated_documents SET pdf_path = 'elsewhere.pdf' WHERE id = :id",
    ],
)
def test_issued_documents_cannot_be_modified(session, sql):
    _, _, doc = issued_document(session)
    with rejected(session, "issued document cannot be modified"):
        session.execute(text(sql), {"id": doc.id})


def test_issued_documents_cannot_be_deleted(session):
    per, tv, doc = issued_document(session)
    with rejected(session, "issued document cannot be deleted"):
        session.execute(text("DELETE FROM generated_documents WHERE id = :id"), {"id": doc.id})
    with rejected(session, "FOREIGN KEY constraint failed"):  # nor can what it points to
        session.execute(text("DELETE FROM periods WHERE id = :id"), {"id": per.id})


def test_document_references_exact_template_version_and_keeps_snapshot(session):
    _, tv, doc = issued_document(session)
    row = session.execute(
        text("SELECT template_version_id, snapshot FROM generated_documents WHERE id = :id"),
        {"id": doc.id},
    ).one()
    assert row.template_version_id == tv.id
    assert json.loads(row.snapshot)


def test_document_rules(session):
    per, tv, _ = issued_document(session)
    session.execute(text("UPDATE periods SET status = 'draft' WHERE id = :id"), {"id": per.id})
    with rejected(session, "versions must be consecutive"):
        f.document(session, per, tv, version_no=3)
    session.execute(text("UPDATE periods SET status = 'draft' WHERE id = :id"), {"id": per.id})
    with rejected(session, "reason_matches_version"):
        f.document(session, per, tv, version_no=2, correction_note=None)
    invalid = f.version(session, f.template(session, "Άκυρο", "9:9"), status="invalid")
    session.execute(text("UPDATE periods SET status = 'draft' WHERE id = :id"), {"id": per.id})
    with rejected(session, "valid template version"):
        f.document(session, per, invalid, version_no=2)


def test_document_hashes_and_snapshot_are_validated(session):
    _, _, _, thr, per, refs = setup_period(session)
    tv = f.version(session, f.template(session, "Π", "1:1"))
    session.commit()
    for kw, match in [
        ({"pdf_sha256": "abc"}, "pdf_sha256_sha256"),
        ({"snapshot": "not json"}, "snapshot_json"),
        ({"issue_date": None}, "NOT NULL constraint failed"),
    ]:
        with rejected(session, match):
            f.document(session, per, tv, **kw)


# ---------------------------------------------------------------- audit log
def test_audit_log_is_append_only(session):
    entry = f.add(
        session,
        AuditLog(
            action="document.issued",
            entity="generated_documents",
            entity_id=1,
            details=json.dumps({"version": 1}),
        ),
    )
    session.commit()
    assert entry.at is not None
    with rejected(session, "audit record cannot be modified"):
        session.execute(text("UPDATE audit_log SET action = 'x' WHERE id = :id"), {"id": entry.id})
    with rejected(session, "audit record cannot be deleted"):
        session.execute(text("DELETE FROM audit_log WHERE id = :id"), {"id": entry.id})
    f.add(session, AuditLog(action="period.reopened", entity="periods", entity_id=1))
    session.commit()
    assert session.execute(text("SELECT count(*) FROM audit_log")).scalar() == 2


def test_audit_details_must_be_json(session):
    with rejected(session, "details_json"):
        f.add(session, AuditLog(action="x", entity="y", details="{oops"))
