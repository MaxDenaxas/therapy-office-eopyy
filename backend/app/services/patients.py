"""Patients (M5.2, ARCHITECTURE.md §5.14).

The caller owns the transaction; every failure raises ``PatientError`` (a stable ``code`` and an
actionable message that never contains the AMKA, a name or any other submitted value) and
leaves the database unchanged once the caller rolls back.

Rules:
  * patients are never deleted (no service function; trigger ``patients_no_delete``) — they are
    deactivated, and can be reactivated;
  * the AMKA is exactly 11 ASCII digits and unique across active *and* inactive patients; it is
    changed only through ``correct_amka``, which needs an explicit acknowledgement when issued
    documents exist (they keep the previous details in their immutable snapshot, R-PAT-2);
  * names and notes follow ``app.text`` (normalisation, allowed characters, lengths);
  * a patient with the same first, last and father's name (compared with ``therapy_fold``) is a
    *warning*, never an error;
  * deactivation is always allowed and changes nothing else; it reports open assignments, draft
    prescriptions and unissued periods as warnings. Creating new assignments, prescriptions and
    periods for an inactive patient is refused by M5.3, M5.4 and M6;
  * ``updated_at`` moves (to the injected clock, whole seconds) only when a value changes; a
    request that changes nothing writes neither ``updated_at`` nor an audit entry.

Search: ``query`` matches the start of words in the three name fields (every token must match),
ignoring accents, case and final sigma; ``amka`` is an exact match. Lists and searches return
**active patients by default**; ``active=False`` returns inactive ones only. ``active=None``
(every patient) exists for internal callers only — the API cannot express it. Nothing in this
module logs.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import String, func, literal, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import GeneratedDocument, Patient, Period, Prescription, TherapistAssignment
from app.services.audit import record_audit
from app.text import (
    SEARCH_MAX_TOKENS,
    TextRuleError,
    normalize_amka,
    normalize_name,
    normalize_notes,
    search_tokens,
)

NAME_FIELDS = ("first_name", "last_name", "father_name")
EDITABLE_FIELDS = frozenset({*NAME_FIELDS, "notes"})
CREATE_FIELDS = EDITABLE_FIELDS | {"amka"}
DEFAULT_LIMIT = 50
MAX_LIMIT = 100
MAX_OFFSET = 1_000_000
ENTITY = "patients"


class PatientError(Exception):
    """``details`` holds ids and counts only."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


@dataclass(frozen=True)
class PatientWarning:
    code: str
    ids: tuple[int, ...] = ()
    count: int | None = None


@dataclass
class Saved:
    item: Patient
    warnings: list[PatientWarning] = field(default_factory=list)


@dataclass(frozen=True)
class Page:
    items: list[Patient]
    total: int
    offset: int
    limit: int


@dataclass(frozen=True)
class Usage:
    prescriptions: int
    draft_prescriptions: int
    open_assignments: int
    unissued_periods: int
    issued_documents: int


# ---------------------------------------------------------------- helpers
def _stamp(now: datetime) -> datetime:
    return now.replace(microsecond=0, tzinfo=None)


def _normalize(name: str, value: Any) -> Any:
    try:
        if name in NAME_FIELDS:
            if value is None:
                raise TextRuleError("invalid_value", f"{name} must not be empty")
            return normalize_name(value, name)
        if name == "notes":
            return normalize_notes(value, name)
        if name == "amka":
            return normalize_amka(value, name)
    except TextRuleError as exc:
        raise PatientError(exc.code, exc.message) from None
    raise PatientError("invalid_field", "unknown fields are not allowed")  # pragma: no cover


def _clean(data: Mapping[str, Any], allowed: frozenset[str]) -> dict[str, Any]:
    if not set(data) <= allowed:
        # never name the fields: a caller (e.g. future extraction) could pass keys derived
        # from document data
        raise PatientError("invalid_field", "unknown fields are not allowed")
    return {name: _normalize(name, value) for name, value in data.items()}


_INTEGRITY_MESSAGES = (
    ("patients.amka", "duplicate_amka", "another patient already has this AMKA"),
    ("never deleted", "patient_not_deletable", "patients are deactivated, never deleted"),
)


def _flush(session: Session) -> None:
    """Database rules the checks above missed become ``PatientError`` (the message is looked up,
    never taken from the exception: it could carry data)."""
    try:
        session.flush()
    except IntegrityError as exc:
        text = str(exc.orig)
        for needle, code, message in _INTEGRITY_MESSAGES:
            if needle in text:
                raise PatientError(code, message) from None
        raise PatientError("constraint_violation", "the data violates a database rule") from None


def _check_amka_free(session: Session, amka: str, patient_id: int | None) -> None:
    query = select(Patient.id, Patient.active).where(Patient.amka == amka)
    if patient_id is not None:
        query = query.where(Patient.id != patient_id)
    other = session.execute(query).first()
    if other is not None:
        raise PatientError(
            "duplicate_amka",
            "another patient already has this AMKA"
            + ("" if other.active else " (inactive: reactivate that patient instead)"),
            patient_id=other.id,
            active=bool(other.active),
        )


def _fold(column):
    return func.therapy_fold(column, type_=String)


def _duplicate_warnings(session: Session, row: Patient) -> list[PatientWarning]:
    ids = session.scalars(
        select(Patient.id)
        .where(
            Patient.id != row.id,
            _fold(Patient.first_name) == func.therapy_fold(row.first_name),
            _fold(Patient.last_name) == func.therapy_fold(row.last_name),
            _fold(Patient.father_name) == func.therapy_fold(row.father_name),
        )
        .order_by(Patient.id)
    ).all()
    return [PatientWarning("possible_duplicate", tuple(ids))] if ids else []


def _count(session: Session, query) -> int:
    return session.scalar(select(func.count()).select_from(query.subquery())) or 0


# ---------------------------------------------------------------- reads
def get_patient(session: Session, patient_id: int) -> Patient:
    row = session.get(Patient, patient_id)
    if row is None:
        raise PatientError("not_found", "patient not found")
    return row


def patient_usage(session: Session, patient_id: int) -> Usage:
    get_patient(session, patient_id)
    prescriptions = select(Prescription.id).where(Prescription.patient_id == patient_id)
    return Usage(
        prescriptions=_count(session, prescriptions),
        draft_prescriptions=_count(session, prescriptions.where(Prescription.status == "draft")),
        open_assignments=_count(
            session,
            select(TherapistAssignment.id).where(
                TherapistAssignment.patient_id == patient_id,
                TherapistAssignment.valid_to.is_(None),
            ),
        ),
        unissued_periods=_count(
            session,
            select(Period.id).where(Period.patient_id == patient_id, Period.status != "issued"),
        ),
        issued_documents=_count(
            session,
            select(GeneratedDocument.id)
            .join(Period, Period.id == GeneratedDocument.period_id)
            .where(Period.patient_id == patient_id),
        ),
    )


def _check_page(offset: int, limit: int) -> None:
    if not (0 <= offset <= MAX_OFFSET) or not (1 <= limit <= MAX_LIMIT):
        raise PatientError("invalid_value", f"offset must be 0-{MAX_OFFSET}, limit 1-{MAX_LIMIT}")


def _page(session: Session, conditions: list, offset: int, limit: int) -> Page:
    _check_page(offset, limit)
    total = session.scalar(select(func.count()).select_from(Patient).where(*conditions)) or 0
    items = session.scalars(
        select(Patient)
        .where(*conditions)
        .order_by(
            _fold(Patient.last_name),
            _fold(Patient.first_name),
            _fold(Patient.father_name),
            Patient.id,
        )
        .offset(offset)
        .limit(limit)
    ).all()
    return Page(list(items), total, offset, limit)


def _active_condition(active: bool | None) -> list:
    return [] if active is None else [Patient.active.is_(active)]


def list_patients(
    session: Session,
    active: bool | None = True,
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
) -> Page:
    return _page(session, _active_condition(active), offset, limit)


def search_patients(
    session: Session,
    *,
    query: str | None = None,
    amka: str | None = None,
    active: bool | None = True,
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
) -> Page:
    """Exactly one of ``query`` (name words) or ``amka`` (exact). Read-only."""
    if (query is None) == (amka is None):
        raise PatientError("invalid_query", "give either a name query or an AMKA, not both")
    conditions = _active_condition(active)
    if amka is not None:
        conditions.append(Patient.amka == _normalize("amka", amka))
    else:
        if not isinstance(query, str) or len(query) > 100:
            raise PatientError("invalid_query", "the query must be text of at most 100 characters")
        tokens = search_tokens(query)
        if not tokens:
            raise PatientError("invalid_query", "the query contains no letters or digits")
        if len(tokens) > SEARCH_MAX_TOKENS:
            raise PatientError("invalid_query", f"at most {SEARCH_MAX_TOKENS} words")
        # ' first last father' with single spaces: ' ' + token can only match a word start;
        # instr() compares literally, so % _ \ need no escaping
        haystack = (
            literal(" ", String)
            + _fold(Patient.first_name)
            + literal(" ", String)
            + _fold(Patient.last_name)
            + literal(" ", String)
            + _fold(Patient.father_name)
        )
        conditions.extend(func.instr(haystack, " " + token) > 0 for token in tokens)
    return _page(session, conditions, offset, limit)


# ---------------------------------------------------------------- writes
def create_patient(session: Session, data: Mapping[str, Any], now: datetime) -> Saved:
    values = _clean(data, CREATE_FIELDS)
    missing = {*NAME_FIELDS, "amka"} - set(values)
    if missing:
        raise PatientError("invalid_value", "missing: " + ", ".join(sorted(missing)))
    _check_amka_free(session, values["amka"], None)
    stamp = _stamp(now)
    row = Patient(**values, active=True, created_at=stamp, updated_at=stamp)
    session.add(row)
    _flush(session)
    record_audit(session, "patient.created", ENTITY, row.id)
    _flush(session)
    return Saved(row, _duplicate_warnings(session, row))


def update_patient(
    session: Session, patient_id: int, changes: Mapping[str, Any], now: datetime
) -> Saved:
    """Names and notes only; the AMKA changes through ``correct_amka``."""
    row = get_patient(session, patient_id)
    if "amka" in changes:
        raise PatientError("invalid_field", "the AMKA is changed through the AMKA correction")
    values = _clean(changes, EDITABLE_FIELDS)
    changed = {k: v for k, v in values.items() if getattr(row, k) != v}
    if changed:
        for name, value in changed.items():
            setattr(row, name, value)
        row.updated_at = _stamp(now)
        _flush(session)
        record_audit(session, "patient.updated", ENTITY, row.id, fields=sorted(changed))
        _flush(session)
    return Saved(row, _duplicate_warnings(session, row))


def correct_amka(
    session: Session,
    patient_id: int,
    amka: Any,
    now: datetime,
    acknowledge_issued_documents: bool = False,
) -> Saved:
    row = get_patient(session, patient_id)
    new = _normalize("amka", amka)
    if new == row.amka:
        return Saved(row)  # nothing changes, nothing is audited
    _check_amka_free(session, new, row.id)
    issued = patient_usage(session, row.id).issued_documents
    if issued and not acknowledge_issued_documents:
        raise PatientError(
            "amka_change_needs_confirmation",
            "issued documents of this patient show the current AMKA and will not change; "
            "confirm the correction (issue corrected documents where needed)",
            issued_documents=issued,
        )
    row.amka = new
    row.updated_at = _stamp(now)
    _flush(session)
    record_audit(session, "patient.amka_corrected", ENTITY, row.id, issued_documents=issued)
    _flush(session)
    warnings = (
        [PatientWarning("issued_documents_show_previous_details", count=issued)] if issued else []
    )
    return Saved(row, warnings)


def set_patient_active(session: Session, patient_id: int, active: bool, now: datetime) -> Saved:
    row = get_patient(session, patient_id)
    if row.active == active:
        return Saved(row)  # idempotent: nothing changes, nothing is audited
    row.active = active
    row.updated_at = _stamp(now)
    _flush(session)
    record_audit(session, "patient.activated" if active else "patient.deactivated", ENTITY, row.id)
    _flush(session)
    warnings: list[PatientWarning] = []
    if not active:
        usage = patient_usage(session, row.id)
        for code, count in (
            ("open_assignments", usage.open_assignments),
            ("draft_prescriptions", usage.draft_prescriptions),
            ("unissued_periods", usage.unissued_periods),
        ):
            if count:
                warnings.append(PatientWarning(code, count=count))
    return Saved(row, warnings)


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "MAX_OFFSET",
    "Page",
    "PatientError",
    "PatientWarning",
    "Saved",
    "Usage",
    "correct_amka",
    "create_patient",
    "get_patient",
    "list_patients",
    "patient_usage",
    "search_patients",
    "set_patient_active",
    "update_patient",
]
