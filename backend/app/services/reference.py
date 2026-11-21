"""Reference data (M5.1): therapy types and therapists.

Both are catalogues the administrator maintains; nothing is hard-coded (D-28). Records that
anything refers to are never deleted — they are deactivated. The caller owns the transaction;
every failure raises ``ReferenceDataError`` (a stable ``code`` plus an actionable message that
contains no personal data) and leaves the database unchanged once the caller rolls back.

Rules (enforced here with friendly messages, and by triggers in migration 0004 as a backstop):
  * a therapy type's ``code`` is immutable once any record uses it (stored template field maps
    name therapies by code);
  * a therapy type or therapist used by an *active* template cannot be deactivated, and a
    therapist's identity details (name, licence number, authority, date) cannot change while an
    active template uses the therapist: deactivate the template first, change the data, then
    upload / activate a corrected template version through the template library;
  * duplicate names and licence numbers are *warnings*, never errors.

The printed EOPYY code of a prescription lives on ``prescription_items`` and is independent of
``therapy_types.eopyy_code``, which is only a mapping aid (it may later be joined by an
additive code-mapping table; nothing here prevents that).
"""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    AuditLog,
    DocumentTemplate,
    PeriodTherapy,
    PrescriptionItem,
    TemplateConfigurationItem,
    Therapist,
    TherapistAssignment,
    TherapyType,
)

THERAPY_TYPE_FIELDS = frozenset(
    {"code", "name_el", "template_label", "eopyy_code", "eopyy_description", "supports_home_visit"}
)
THERAPY_TYPE_REQUIRED = frozenset({"code", "name_el", "template_label", "supports_home_visit"})
THERAPIST_FIELDS = frozenset(
    {"full_name", "specialty", "license_no", "license_authority", "license_date"}
)
THERAPIST_REQUIRED = frozenset({"full_name"})
# what a template prints about a therapist (D-27): frozen while an active template uses them
THERAPIST_IDENTITY_FIELDS = frozenset(
    {"full_name", "license_no", "license_authority", "license_date"}
)


class ReferenceDataError(Exception):
    """``code`` is stable and machine-readable; ``message`` is for the administrator;
    ``details`` holds ids and names of templates only (never personal data)."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


@dataclass(frozen=True)
class ReferenceWarning:
    code: str
    ids: tuple[int, ...] = ()


@dataclass
class Saved:
    item: Any
    warnings: list[ReferenceWarning] = field(default_factory=list)


# ---------------------------------------------------------------- helpers
def _fold(text: str) -> str:
    """Case-, accent- and spacing-insensitive form (Greek capitals drop accents)."""
    decomposed = unicodedata.normalize("NFD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(stripped.casefold().split())


def _clean(changes: Mapping[str, Any], allowed: frozenset[str], required: frozenset[str]) -> dict:
    unknown = set(changes) - allowed
    if unknown:
        raise ReferenceDataError("invalid_field", "unknown field(s): " + ", ".join(sorted(unknown)))
    cleaned: dict[str, Any] = {}
    for name, value in changes.items():
        if isinstance(value, str):
            value = value.strip() or None
        if value is None and name in required:
            raise ReferenceDataError("invalid_value", f"{name} must not be empty")
        cleaned[name] = value
    return cleaned


def _audit(session: Session, action: str, entity: str, entity_id: int, **details) -> None:
    session.add(
        AuditLog(
            action=action,
            entity=entity,
            entity_id=entity_id,
            details=json.dumps(details, sort_keys=True, default=str) if details else None,
        )
    )


_INTEGRITY_MESSAGES = (
    ("therapy_types.code", "duplicate_code", "another therapy type already has this code"),
    (
        "therapy_types.eopyy_code",
        "duplicate_eopyy_code",
        "another therapy type already has this EOPYY code",
    ),
    ("code cannot change once it is used", "code_locked", "the therapy type code is in use"),
    (
        "cannot be deactivated",
        "in_use_by_active_template",
        "it is used by an active template; deactivate the template first",
    ),
    (
        "cannot change identity details",
        "identity_locked",
        "the therapist is used by an active template; deactivate the template first",
    ),
)


def _flush(session: Session) -> None:
    """Flush; database rules that the checks above missed become ``ReferenceDataError``
    (the message is looked up, never taken from the exception: it could carry data)."""
    try:
        session.flush()
    except IntegrityError as exc:
        text = str(exc.orig)
        for needle, code, message in _INTEGRITY_MESSAGES:
            if needle in text:
                raise ReferenceDataError(code, message) from None
        raise ReferenceDataError(
            "constraint_violation", "the data violates a database rule"
        ) from None


def _active_templates(session: Session, column, entity_id: int) -> list[dict]:
    rows = session.execute(
        select(DocumentTemplate.id, DocumentTemplate.name)
        .join(
            TemplateConfigurationItem, TemplateConfigurationItem.template_id == DocumentTemplate.id
        )
        .where(column == entity_id, DocumentTemplate.active.is_(True))
        .order_by(DocumentTemplate.name)
    ).all()
    return [{"id": r.id, "name": r.name} for r in rows]


def _refuse_while_in_active_template(
    session: Session, column, entity_id: int, what: str, action: str, code: str
) -> None:
    templates = _active_templates(session, column, entity_id)
    if templates:
        names = ", ".join(repr(t["name"]) for t in templates)
        raise ReferenceDataError(
            code,
            f"this {what} is used by active template(s) {names}; deactivate "
            f"{'it' if len(templates) == 1 else 'them'} first, {action}, then upload and "
            "activate a corrected template version",
            templates=templates,
        )


# ---------------------------------------------------------------- therapy types
def get_therapy_type(session: Session, type_id: int) -> TherapyType:
    row = session.get(TherapyType, type_id)
    if row is None:
        raise ReferenceDataError("not_found", "therapy type not found")
    return row


def list_therapy_types(session: Session, active: bool | None = None) -> list[TherapyType]:
    query = select(TherapyType).order_by(TherapyType.name_el, TherapyType.id)
    if active is not None:
        query = query.where(TherapyType.active.is_(active))
    return list(session.scalars(query))


def therapy_type_references(session: Session, type_id: int) -> list[str]:
    """Names of the tables whose rows use this therapy type (why its code is frozen)."""
    used = []
    for table, model in (
        ("prescription_items", PrescriptionItem),
        ("period_therapies", PeriodTherapy),
        ("template_configuration_items", TemplateConfigurationItem),
        ("therapist_assignments", TherapistAssignment),
    ):
        count = session.scalar(
            select(func.count()).select_from(model).where(model.therapy_type_id == type_id)
        )
        if count:
            used.append(table)
    return used


def _therapy_type_warnings(session: Session, row: TherapyType) -> list[ReferenceWarning]:
    others = [
        t.id
        for t in session.scalars(select(TherapyType).where(TherapyType.id != row.id))
        if _fold(t.name_el) == _fold(row.name_el)
    ]
    return [ReferenceWarning("duplicate_name", tuple(others))] if others else []


def _check_therapy_type_unique(session: Session, row_id: int | None, changes: Mapping) -> None:
    for column, code, label in (
        ("code", "duplicate_code", "therapy type code"),
        ("eopyy_code", "duplicate_eopyy_code", "EOPYY code"),
    ):
        value = changes.get(column)
        if value is None:
            continue
        query = select(TherapyType.id).where(getattr(TherapyType, column) == value)
        if row_id is not None:
            query = query.where(TherapyType.id != row_id)
        if session.scalar(query) is not None:
            raise ReferenceDataError(code, f"another therapy type already has this {label}")


def create_therapy_type(session: Session, data: Mapping[str, Any]) -> Saved:
    data = {"supports_home_visit": False, **data}
    values = _clean(data, THERAPY_TYPE_FIELDS, THERAPY_TYPE_REQUIRED)
    missing = {"code", "name_el", "template_label"} - set(values)
    if missing:
        raise ReferenceDataError("invalid_value", "missing: " + ", ".join(sorted(missing)))
    _check_therapy_type_unique(session, None, values)
    row = TherapyType(**values, active=True)
    session.add(row)
    _flush(session)
    _audit(session, "therapy_type.created", "therapy_types", row.id)
    _flush(session)
    return Saved(row, _therapy_type_warnings(session, row))


def update_therapy_type(session: Session, type_id: int, changes: Mapping[str, Any]) -> Saved:
    row = get_therapy_type(session, type_id)
    values = _clean(changes, THERAPY_TYPE_FIELDS, THERAPY_TYPE_REQUIRED)
    changed = {k: v for k, v in values.items() if getattr(row, k) != v}
    if "code" in changed:
        used = therapy_type_references(session, row.id)
        if used:
            raise ReferenceDataError(
                "code_locked",
                "the therapy type code cannot change because it is already used by "
                + ", ".join(used)
                + "; create a new therapy type instead",
                referenced_by=used,
            )
    _check_therapy_type_unique(session, row.id, changed)
    for name, value in changed.items():
        setattr(row, name, value)
    if changed:
        _flush(session)
        _audit(session, "therapy_type.updated", "therapy_types", row.id, fields=sorted(changed))
        _flush(session)
    return Saved(row, _therapy_type_warnings(session, row))


def set_therapy_type_active(session: Session, type_id: int, active: bool) -> TherapyType:
    row = get_therapy_type(session, type_id)
    if row.active == active:
        return row  # idempotent: nothing changes, nothing is audited
    if not active:
        _refuse_while_in_active_template(
            session,
            TemplateConfigurationItem.therapy_type_id,
            row.id,
            "therapy type",
            "deactivate the therapy type",
            "in_use_by_active_template",
        )
    row.active = active
    _flush(session)
    _audit(
        session,
        "therapy_type.activated" if active else "therapy_type.deactivated",
        "therapy_types",
        row.id,
    )
    _flush(session)
    return row


# ---------------------------------------------------------------- therapists
def get_therapist(session: Session, therapist_id: int) -> Therapist:
    row = session.get(Therapist, therapist_id)
    if row is None:
        raise ReferenceDataError("not_found", "therapist not found")
    return row


def list_therapists(session: Session, active: bool | None = None) -> list[Therapist]:
    query = select(Therapist).order_by(Therapist.full_name, Therapist.id)
    if active is not None:
        query = query.where(Therapist.active.is_(active))
    return list(session.scalars(query))


def _therapist_warnings(session: Session, row: Therapist) -> list[ReferenceWarning]:
    warnings = []
    others = list(session.scalars(select(Therapist).where(Therapist.id != row.id)))
    same_name = [t.id for t in others if _fold(t.full_name) == _fold(row.full_name)]
    if same_name:
        warnings.append(ReferenceWarning("duplicate_full_name", tuple(same_name)))
    if row.license_no:
        same_licence = [
            t.id for t in others if t.license_no and _fold(t.license_no) == _fold(row.license_no)
        ]
        if same_licence:
            warnings.append(ReferenceWarning("duplicate_license_no", tuple(same_licence)))
    return warnings


def create_therapist(session: Session, data: Mapping[str, Any]) -> Saved:
    values = _clean(data, THERAPIST_FIELDS, THERAPIST_REQUIRED)
    if "full_name" not in values:
        raise ReferenceDataError("invalid_value", "missing: full_name")
    row = Therapist(**values, active=True)
    session.add(row)
    _flush(session)
    _audit(session, "therapist.created", "therapists", row.id)
    _flush(session)
    return Saved(row, _therapist_warnings(session, row))


def update_therapist(session: Session, therapist_id: int, changes: Mapping[str, Any]) -> Saved:
    row = get_therapist(session, therapist_id)
    values = _clean(changes, THERAPIST_FIELDS, THERAPIST_REQUIRED)
    changed = {k: v for k, v in values.items() if getattr(row, k) != v}
    if changed.keys() & THERAPIST_IDENTITY_FIELDS:
        _refuse_while_in_active_template(
            session,
            TemplateConfigurationItem.therapist_id,
            row.id,
            "therapist",
            "change the therapist's details",
            "identity_locked",
        )
    for name, value in changed.items():
        setattr(row, name, value)
    if changed:
        _flush(session)
        _audit(session, "therapist.updated", "therapists", row.id, fields=sorted(changed))
        _flush(session)
    return Saved(row, _therapist_warnings(session, row))


def set_therapist_active(session: Session, therapist_id: int, active: bool) -> Therapist:
    row = get_therapist(session, therapist_id)
    if row.active == active:
        return row
    if not active:
        _refuse_while_in_active_template(
            session,
            TemplateConfigurationItem.therapist_id,
            row.id,
            "therapist",
            "deactivate the therapist",
            "in_use_by_active_template",
        )
    row.active = active
    _flush(session)
    _audit(
        session,
        "therapist.activated" if active else "therapist.deactivated",
        "therapists",
        row.id,
    )
    _flush(session)
    return row


__all__ = [
    "ReferenceDataError",
    "Saved",
    "ReferenceWarning",
    "create_therapist",
    "create_therapy_type",
    "get_therapist",
    "get_therapy_type",
    "list_therapists",
    "list_therapy_types",
    "set_therapist_active",
    "set_therapy_type_active",
    "therapy_type_references",
    "update_therapist",
    "update_therapy_type",
]
