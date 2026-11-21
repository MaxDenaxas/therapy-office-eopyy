"""Schemas for patients (M5.2).

Requests do **not** use ``str_strip_whitespace`` (unlike the M5.1 schemas): Pydantic would strip
tabs and newlines silently before any validator runs, and the approved rules (``app.text``)
refuse them instead. Each text field is normalised by the same functions the service uses.

``PatientSummary`` (lists and search results) has **no** ``amka`` field — only ``amka_masked`` —
so a full AMKA in a list is impossible by construction. ``PatientDetail`` carries it.
Validation messages name the rule, never the submitted value (app/errors.py drops values).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from app.models import Patient
from app.services.patients import DEFAULT_LIMIT, MAX_LIMIT, MAX_OFFSET
from app.text import TextRuleError, mask_amka, normalize_amka, normalize_name, normalize_notes

_NAMES = ("first_name", "last_name", "father_name")


def _apply(fn, value, field: str):
    try:
        return fn(value, field)
    except TextRuleError as exc:
        raise ValueError(exc.message) from None


class _Request(BaseModel):
    """Unknown fields are refused *without naming them*: with ``extra="forbid"`` alone Pydantic
    reports each unknown key in the error location, and a key could itself be an AMKA or a name
    (the error body echoes locations). ``extra="forbid"`` stays as a backstop."""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _no_unknown_fields(cls, data):
        if isinstance(data, dict) and not set(data) <= set(cls.model_fields):
            raise ValueError("unknown fields are not allowed")
        return data


class PatientCreate(_Request):
    first_name: str
    last_name: str
    father_name: str
    amka: str
    notes: str | None = None

    @field_validator(*_NAMES)
    @classmethod
    def _name(cls, value: str, info: ValidationInfo) -> str:
        return _apply(normalize_name, value, info.field_name)

    @field_validator("amka")
    @classmethod
    def _amka(cls, value: str) -> str:
        return _apply(normalize_amka, value, "amka")

    @field_validator("notes")
    @classmethod
    def _notes(cls, value: str | None) -> str | None:
        return _apply(normalize_notes, value, "notes")


class PatientUpdate(_Request):
    """PATCH: only the fields sent change. The AMKA is not accepted here (unknown field → 422);
    it changes through ``POST /api/patients/{id}/amka``."""

    first_name: str | None = None
    last_name: str | None = None
    father_name: str | None = None
    notes: str | None = None

    @field_validator(*_NAMES)
    @classmethod
    def _name(cls, value: str | None, info: ValidationInfo) -> str:
        if value is None:
            raise ValueError(f"{info.field_name} must not be empty")
        return _apply(normalize_name, value, info.field_name)

    @field_validator("notes")
    @classmethod
    def _notes(cls, value: str | None) -> str | None:
        return _apply(normalize_notes, value, "notes")

    @model_validator(mode="after")
    def _something(self):
        if not self.model_fields_set:
            raise ValueError("at least one field must be given")
        return self

    def changes(self) -> dict:
        return {name: getattr(self, name) for name in self.model_fields_set}


class AmkaCorrection(_Request):
    amka: str
    acknowledge_issued_documents: bool = False

    @field_validator("amka")
    @classmethod
    def _amka(cls, value: str) -> str:
        return _apply(normalize_amka, value, "amka")


class EmptyBody(_Request):
    """activate / deactivate: an empty JSON object (or no body at all)."""


class PatientSearch(_Request):
    """Personal search values travel only in this POST body, never in a URL."""

    query: str | None = Field(default=None, min_length=1, max_length=100)
    amka: str | None = None
    active: bool = True  # omitted: active patients only; false: inactive only
    offset: int = Field(default=0, ge=0, le=MAX_OFFSET)
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)

    @field_validator("amka")
    @classmethod
    def _amka(cls, value: str | None) -> str | None:
        return None if value is None else _apply(normalize_amka, value, "amka")

    @model_validator(mode="after")
    def _one_criterion(self):
        if (self.query is None) == (self.amka is None):
            raise ValueError("give exactly one of query or amka")
        return self


# ---------------------------------------------------------------- responses
class PatientSummary(BaseModel):
    id: int
    first_name: str
    last_name: str
    father_name: str
    amka_masked: str
    active: bool

    @classmethod
    def of(cls, row: Patient) -> PatientSummary:
        return cls(
            id=row.id,
            first_name=row.first_name,
            last_name=row.last_name,
            father_name=row.father_name,
            amka_masked=mask_amka(row.amka),
            active=row.active,
        )


class PatientDetail(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    first_name: str
    last_name: str
    father_name: str
    amka: str
    notes: str | None
    active: bool
    created_at: datetime
    updated_at: datetime


class PatientWarningOut(BaseModel):
    code: str
    ids: list[int] = []
    count: int | None = None


class UsageOut(BaseModel):
    prescriptions: int
    draft_prescriptions: int
    open_assignments: int
    unissued_periods: int
    issued_documents: int


class PatientResponse(BaseModel):
    item: PatientDetail
    warnings: list[PatientWarningOut] = []


class PatientDetailResponse(BaseModel):
    item: PatientDetail
    usage: UsageOut


class PatientPage(BaseModel):
    items: list[PatientSummary]
    total: int
    offset: int
    limit: int
