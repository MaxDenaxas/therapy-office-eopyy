"""Template library (M4): register templates, add immutable versions, validate, activate,
and find the exact template for a therapy/therapist combination.

The caller owns the transaction (``app.db.session_scope``). Files go to the
``FileStore`` (outside the repository, content-addressed, read-only) before the
database row is written; if the transaction then fails, the stored file is simply
reused by the next identical upload.

Rules
  * configuration = set of (therapy_type_id, therapist_id); canonical key "tt:th,tt:th"
    sorted by therapy type; frozen once a version exists (database trigger)
  * a version is immutable; a correction is a new version; old versions and the
    documents issued from them are never touched
  * the same file cannot be a second version of a template, nor serve another template
  * activation re-checks everything against today's data and refuses conflicts
  * matching is exact: one active, valid, hash-verified version or an explicit error
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.documents import DocumentError, FieldMap, validate_template
from app.documents import locate as docx_locate
from app.models import (
    AuditLog,
    CenterSettings,
    DocumentTemplate,
    Period,
    PeriodTherapy,
    TemplateConfigurationItem,
    TemplateVersion,
    Therapist,
    TherapistAssignment,
    TherapyType,
)
from app.storage import FileStore

FIELD_MAP_SCHEMA_VERSION = 1


# ---------------------------------------------------------------- errors
class TemplateLibraryError(Exception):
    """Base class; messages are actionable and contain no patient data."""


class TemplateNotFound(TemplateLibraryError):
    pass


class InvalidConfiguration(TemplateLibraryError):
    pass


class DuplicateTemplateFile(TemplateLibraryError):
    pass


class InvalidFieldMap(TemplateLibraryError):
    pass


class TemplateValidationFailed(TemplateLibraryError):
    def __init__(self, errors: list[str], warnings: list[str] | None = None):
        self.errors, self.warnings = list(errors), list(warnings or [])
        super().__init__("template cannot be used:\n- " + "\n- ".join(self.errors))


class TemplateConflict(TemplateLibraryError):
    pass


class NoMatchingTemplate(TemplateLibraryError):
    pass


class AmbiguousTemplates(TemplateLibraryError):
    pass


# ---------------------------------------------------------------- results
@dataclass(frozen=True)
class VersionResult:
    version: TemplateVersion
    valid: bool
    errors: tuple[str, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class ActivationResult:
    template: DocumentTemplate
    version: TemplateVersion
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class MatchedTemplate:
    template_id: int
    template_name: str
    version_id: int
    version_no: int
    path: Path
    sha256: str
    field_map: FieldMap
    configuration_key: str
    selected_therapies: tuple[str, ...] = ()  # therapy codes of the template = of the request


@dataclass
class RegistrationResult:
    template: DocumentTemplate
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- configuration
Pair = tuple[int, int]  # (therapy_type_id, therapist_id)


def configuration_key(pairs: Iterable[Pair]) -> str:
    pairs = sorted(set(pairs))
    therapy_types = [t for t, _ in pairs]
    if not pairs:
        raise InvalidConfiguration("a template needs at least one therapy/therapist pair")
    if len(therapy_types) != len(set(therapy_types)):
        raise InvalidConfiguration("each therapy may appear only once in a configuration")
    return ",".join(f"{t}:{th}" for t, th in pairs)


def _describe(session: Session, pairs: Iterable[Pair]) -> str:
    parts = []
    for tt_id, th_id in sorted(pairs):
        tt, th = session.get(TherapyType, tt_id), session.get(Therapist, th_id)
        parts.append(
            f"{tt.name_el if tt else f'therapy #{tt_id}'} "
            f"({th.full_name if th else f'therapist #{th_id}'})"
        )
    return " + ".join(parts)


def _check_pairs_exist(session: Session, pairs: Iterable[Pair]) -> None:
    for tt_id, th_id in pairs:
        if session.get(TherapyType, tt_id) is None:
            raise InvalidConfiguration(f"therapy type #{tt_id} does not exist")
        if session.get(Therapist, th_id) is None:
            raise InvalidConfiguration(f"therapist #{th_id} does not exist")


def _template(session: Session, template_id: int) -> DocumentTemplate:
    template = session.get(DocumentTemplate, template_id)
    if template is None:
        raise TemplateNotFound(f"template #{template_id} does not exist")
    return template


def _pairs(session: Session, template_id: int) -> list[Pair]:
    rows = session.scalars(
        select(TemplateConfigurationItem).where(
            TemplateConfigurationItem.template_id == template_id
        )
    ).all()
    return sorted((r.therapy_type_id, r.therapist_id) for r in rows)


def _audit(session: Session, action: str, entity_id: int, **details) -> None:
    session.add(
        AuditLog(
            action=action,
            entity="document_templates",
            entity_id=entity_id,
            details=json.dumps(details, sort_keys=True, default=str),
        )
    )


# ---------------------------------------------------------------- registration
def register_template(
    session: Session, name: str, pairs: Iterable[Pair], description: str | None = None
) -> RegistrationResult:
    pairs = sorted(set(pairs))
    key = configuration_key(pairs)
    _check_pairs_exist(session, pairs)
    if not name.strip():
        raise InvalidConfiguration("a template needs a name")
    if session.scalar(select(DocumentTemplate).where(DocumentTemplate.name == name)):
        raise InvalidConfiguration(f"a template named {name!r} already exists")
    template = DocumentTemplate(name=name, description=description, configuration_key=key)
    session.add(template)
    session.flush()
    for tt_id, th_id in pairs:
        session.add(
            TemplateConfigurationItem(
                template_id=template.id, therapy_type_id=tt_id, therapist_id=th_id
            )
        )
    session.flush()
    result = RegistrationResult(template)
    same = session.scalars(
        select(DocumentTemplate).where(
            DocumentTemplate.configuration_key == key, DocumentTemplate.id != template.id
        )
    ).all()
    for other in same:
        state = "active" if other.active else "inactive"
        result.warnings.append(
            f"template {other.name!r} ({state}) has the same combination; only one of them can be "
            "active at a time"
        )
    _audit(session, "template.registered", template.id, configuration_key=key)
    session.flush()
    return result


# ---------------------------------------------------------------- validation (engine + config)
def _configuration_checks(
    session: Session, template: DocumentTemplate, stored: Path, fmap: FieldMap
) -> tuple[list[str], list[str]]:
    """Checks the engine cannot know: the map's sections are exactly the configured
    therapies, the configuration is consistent and active, licence numbers agree,
    center settings exist for dynamic center fields."""
    errors: list[str] = []
    warnings: list[str] = []
    pairs = _pairs(session, template.id)
    if configuration_key(pairs) != template.configuration_key:
        errors.append("the stored configuration key does not match the configured therapies")
    codes = {}
    for tt_id, th_id in pairs:
        tt, th = session.get(TherapyType, tt_id), session.get(Therapist, th_id)
        codes[tt.code] = th
        if not tt.active:
            errors.append(f"therapy {tt.name_el!r} is inactive")
        if not th.active:
            errors.append(f"therapist {th.full_name!r} is inactive")
    mapped = {b.therapy for b in fmap.blocks}
    for code in sorted(set(codes) - mapped):
        errors.append(f"configured therapy {code!r} has no section in the field map")
    for code in sorted(mapped - set(codes)):
        errors.append(f"the field map has a section for {code!r}, which is not configured")
    try:
        doc = Document(str(stored))
    except Exception:  # noqa: BLE001 - reported by the engine validation already
        return errors, warnings
    for block in fmap.blocks:
        therapist = codes.get(block.therapy)
        if therapist is None:
            continue
        identity_errors, identity_warnings = _identity_checks(doc, block, therapist)
        errors += identity_errors
        warnings += identity_warnings
    settings = session.get(CenterSettings, 1)
    for name in ("ada", "representative"):
        spec = getattr(fmap, name)
        value = getattr(settings, "representative_name" if name == "representative" else name)
        if spec is not None and spec.mode == "dynamic" and not value:
            warnings.append(
                f"{name} is dynamic in this template but not set in the center "
                "settings; documents cannot be generated until it is"
            )
    return errors, warnings


def _normalise(text: str) -> str:
    """Compare printed text with records ignoring case, accents (Greek capitals carry none),
    surrounding punctuation and spacing."""
    decomposed = unicodedata.normalize("NFD", text.casefold())
    plain = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
    return " ".join(plain.replace("\t", " ").split()).strip(" .,;:")


def _printed_date(text: str) -> date | None:
    match = re.fullmatch(r"\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*", text)
    if not match:
        return None
    day, month, year = map(int, match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _identity_checks(doc, block, therapist: Therapist) -> tuple[list[str], list[str]]:
    """D-27: every therapist detail the template prints must agree with the therapist's
    record; a detail missing from the record cannot be checked and blocks the version.
    Signatures and stamps cannot be verified automatically: a section without any image
    is only flagged, and their identity needs the administrator's visual check."""
    errors: list[str] = []
    warnings: list[str] = []
    who = f"therapist {therapist.full_name!r}"
    details = block.therapist_details
    checks = [
        ("licence number", details.license_no, therapist.license_no, "text"),
        ("issuing authority", details.authority, therapist.license_authority, "text"),
        ("licence date", details.license_date, therapist.license_date, "date"),
    ]
    if details.name is not None:
        checks.append(("name", details.name, therapist.full_name, "text"))
    for what, spec, recorded, kind in checks:
        try:
            printed = docx_locate.read(docx_locate.resolve(doc, spec.value_loc))
        except DocumentError:
            continue  # reported by the engine validation
        where = f"section {block.therapy!r}"
        if recorded is None or (isinstance(recorded, str) and not recorded.strip()):
            errors.append(
                f"{where} prints the {what}, but {who} has none on record; enter it so "
                "the template can be checked"
            )
        elif kind == "date":
            if _printed_date(printed) != recorded:
                errors.append(
                    f"{where} prints {what} {printed.strip()!r}, but {who} has "
                    f"{recorded:%d/%m/%Y}; wrong template or wrong therapist"
                )
        elif _normalise(printed) != _normalise(recorded):
            errors.append(
                f"{where} prints {what} {printed.strip()!r}, but {who} has "
                f"{recorded!r}; wrong template or wrong therapist"
            )
    loc = block.label_loc
    if loc.part == "body" and loc.table is not None and loc.table < len(doc.tables):
        table = doc.tables[loc.table]._tbl
        if next(table.iter(qn("w:drawing"), qn("w:pict")), None) is None:
            warnings.append(
                f"section {block.therapy!r} contains no image: check that it carries "
                "the therapist's signature and stamp, or that they are added by hand"
            )
    return errors, warnings


def _parse_map(raw: FieldMap | Mapping | str) -> FieldMap:
    try:
        if isinstance(raw, FieldMap):
            return raw
        if isinstance(raw, str):
            return FieldMap.model_validate_json(raw)
        return FieldMap.model_validate(raw)
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())
        raise InvalidFieldMap(f"the field map is not valid: {problems}") from e


def add_version(
    session: Session,
    store: FileStore,
    template_id: int,
    upload: Path,
    field_map: FieldMap | Mapping | str,
    *,
    original_filename: str | None = None,
) -> VersionResult:
    """Store the upload, validate it with its field map, and record an immutable version
    (valid or invalid, with the full report). Never changes existing versions."""
    template = _template(session, template_id)
    fmap = _parse_map(field_map)
    stored = store.put_template(upload)  # inspects the DOCX; raises TemplateFileError
    if fmap.template_sha256 != stored.sha256:
        raise InvalidFieldMap("the field map was made for a different file (SHA-256 differs)")
    same_file = session.scalars(
        select(TemplateVersion).where(TemplateVersion.sha256 == stored.sha256)
    ).all()
    for other in same_file:
        if other.template_id == template.id:
            raise DuplicateTemplateFile(
                f"this file is identical to version {other.version_no} of this template"
            )
        raise DuplicateTemplateFile(
            f"this file is already registered for template #{other.template_id}; a file "
            "can serve only one therapy/therapist combination"
        )

    report = validate_template(stored.path, fmap)
    errors, warnings = list(report.errors), list(report.warnings)
    more_errors, more_warnings = _configuration_checks(session, template, stored.path, fmap)
    errors += more_errors
    warnings += more_warnings
    number = (
        session.scalar(
            select(func.max(TemplateVersion.version_no)).where(
                TemplateVersion.template_id == template.id
            )
        )
        or 0
    ) + 1
    version = TemplateVersion(
        template_id=template.id,
        version_no=number,
        original_filename=(original_filename or Path(upload).name)[:255],
        sha256=stored.sha256,
        storage_path=stored.relative_path,
        size_bytes=stored.size_bytes,
        field_map=fmap.model_dump_json(),
        field_map_schema_version=FIELD_MAP_SCHEMA_VERSION,
        validation_status="invalid" if errors else "valid",
        validation_report=json.dumps({"errors": errors, "warnings": warnings}, ensure_ascii=False),
    )
    session.add(version)
    session.flush()
    _audit(
        session,
        "template.version_added",
        template.id,
        version_id=version.id,
        version_no=number,
        valid=not errors,
        sha256=stored.sha256,
    )
    session.flush()
    return VersionResult(version, not errors, tuple(errors), tuple(warnings))


# ---------------------------------------------------------------- activation
def activate_template(
    session: Session, store: FileStore, template_id: int, version_id: int | None = None
) -> ActivationResult:
    """Make ``version_id`` (default: the newest valid version) the template's current
    version and activate the template. Everything is re-checked first."""
    template = _template(session, template_id)
    if version_id is None:
        version = session.scalars(
            select(TemplateVersion)
            .where(
                TemplateVersion.template_id == template.id,
                TemplateVersion.validation_status == "valid",
            )
            .order_by(TemplateVersion.version_no.desc())
        ).first()
        if version is None:
            raise TemplateValidationFailed(["the template has no valid version to activate"])
    else:
        version = session.get(TemplateVersion, version_id)
        if version is None or version.template_id != template.id:
            raise TemplateNotFound(f"version #{version_id} does not belong to this template")
    if version.validation_status != "valid":
        report = json.loads(version.validation_report or "{}")
        raise TemplateValidationFailed(report.get("errors") or ["the version is invalid"])

    try:
        path = store.verify(version.storage_path, version.sha256)
    except DocumentError as e:
        raise TemplateValidationFailed([f"stored file: {e}"]) from e
    fmap = FieldMap.model_validate_json(version.field_map)
    report = validate_template(path, fmap)
    errors, warnings = list(report.errors), list(report.warnings)
    more_errors, more_warnings = _configuration_checks(session, template, path, fmap)
    errors += more_errors
    warnings += more_warnings
    if errors:
        raise TemplateValidationFailed(errors, warnings)

    clash = session.scalars(
        select(DocumentTemplate).where(
            DocumentTemplate.configuration_key == template.configuration_key,
            DocumentTemplate.active.is_(True),
            DocumentTemplate.id != template.id,
        )
    ).first()
    if clash is not None:
        raise TemplateConflict(
            f"template {clash.name!r} is already active for "
            f"{_describe(session, _pairs(session, template.id))}; deactivate it first"
        )
    template.current_version_id = version.id
    template.active = True
    session.flush()
    _audit(
        session,
        "template.activated",
        template.id,
        version_id=version.id,
        version_no=version.version_no,
    )
    session.flush()
    return ActivationResult(template, version, tuple(warnings))


def deactivate_template(session: Session, template_id: int) -> DocumentTemplate:
    template = _template(session, template_id)
    template.active = False
    session.flush()
    _audit(session, "template.deactivated", template.id)
    session.flush()
    return template


# ---------------------------------------------------------------- matching
def match_template(session: Session, store: FileStore, pairs: Iterable[Pair]) -> MatchedTemplate:
    """The single active template whose configuration **exactly** equals the requested
    (therapy, therapist) pairs (D-28): no larger template, no smaller one, no other
    therapist. None -> NoMatchingTemplate; several -> AmbiguousTemplates (normally
    impossible: one active template per combination is a database constraint)."""
    pairs = sorted(set(pairs))
    key = configuration_key(pairs)  # validates the request (non-empty, one therapist per therapy)
    candidates = session.scalars(
        select(DocumentTemplate)
        .where(DocumentTemplate.configuration_key == key, DocumentTemplate.active.is_(True))
        .order_by(DocumentTemplate.name)
    ).all()
    if not candidates:
        raise NoMatchingTemplate(
            f"no active template for exactly {_describe(session, pairs)}; register and activate one"
        )
    if len(candidates) > 1:
        names = ", ".join(t.name for t in candidates)
        raise AmbiguousTemplates(
            f"several active templates match {_describe(session, pairs)}: {names}; "
            "deactivate all but one"
        )
    template = candidates[0]
    if _pairs(session, template.id) != pairs:
        raise NoMatchingTemplate(
            f"template {template.name!r} has an inconsistent configuration; it cannot be used"
        )
    version = session.get(TemplateVersion, template.current_version_id)
    if version is None or version.validation_status != "valid":
        raise NoMatchingTemplate(f"template {template.name!r} has no valid current version")
    try:
        path = store.verify(version.storage_path, version.sha256)
    except DocumentError as e:
        raise NoMatchingTemplate(f"template {template.name!r} cannot be used: {e}") from e
    fmap = FieldMap.model_validate_json(version.field_map)
    selected = tuple(sorted(session.get(TherapyType, tt_id).code for tt_id, _ in pairs))
    if set(selected) != {b.therapy for b in fmap.blocks}:  # never render a mismatched map
        raise NoMatchingTemplate(
            f"template {template.name!r}: its sections do not equal its configuration"
        )
    return MatchedTemplate(
        template.id,
        template.name,
        version.id,
        version.version_no,
        path,
        version.sha256,
        fmap,
        template.configuration_key,
        selected,
    )


def match_for_period(session: Session, store: FileStore, period_id: int) -> MatchedTemplate:
    """Authoritative match for a document: the period's own therapy/therapist snapshot (P-05)."""
    if session.get(Period, period_id) is None:
        raise LookupError(f"document period {period_id} does not exist")
    rows = session.scalars(select(PeriodTherapy).where(PeriodTherapy.period_id == period_id)).all()
    if not rows:
        raise NoMatchingTemplate("the document period has no therapies")
    return match_template(session, store, [(r.therapy_type_id, r.therapist_id) for r in rows])


def match_for_patient(session: Session, store: FileStore, patient_id: int) -> MatchedTemplate:
    """Early check at registration / assignment change: the patient's current assignments."""
    rows = session.scalars(
        select(TherapistAssignment).where(
            TherapistAssignment.patient_id == patient_id, TherapistAssignment.valid_to.is_(None)
        )
    ).all()
    if not rows:
        raise NoMatchingTemplate("the patient has no current therapist assignments")
    return match_template(session, store, [(r.therapy_type_id, r.therapist_id) for r in rows])
