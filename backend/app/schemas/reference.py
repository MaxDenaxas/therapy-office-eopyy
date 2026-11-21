"""Schemas for therapy types and therapists (M5.1).

Validation errors never echo submitted values (app/errors.py drops them); the messages below
therefore only name the rule.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# stable ASCII identifier: stored template field maps and the scheduler refer to it
THERAPY_CODE_PATTERN = r"^[a-z][a-z0-9_-]{0,63}$"
EOPYY_CODE_PATTERN = r"^[0-9]{1,16}$"


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @field_validator("*", mode="before")
    @classmethod
    def _blank_to_none(cls, value):
        """A text field sent as "" or only spaces means "no value"."""
        return None if isinstance(value, str) and not value.strip() else value


class _Update(_Request):
    """PATCH: only the fields that are present change; required ones cannot become empty."""

    REQUIRED: ClassVar[frozenset[str]] = frozenset()

    @model_validator(mode="after")
    def _check_fields(self):
        if not self.model_fields_set:
            raise ValueError("at least one field must be given")
        for name in self.model_fields_set & self.REQUIRED:
            if getattr(self, name) is None:
                raise ValueError(f"{name} must not be empty")
        return self

    def changes(self) -> dict:
        return {name: getattr(self, name) for name in self.model_fields_set}


# ---------------------------------------------------------------- therapy types
class TherapyTypeCreate(_Request):
    code: str = Field(pattern=THERAPY_CODE_PATTERN)
    name_el: str = Field(min_length=1, max_length=200)
    template_label: str = Field(min_length=1, max_length=100)
    eopyy_code: str | None = Field(default=None, pattern=EOPYY_CODE_PATTERN)
    eopyy_description: str | None = Field(default=None, max_length=200)
    supports_home_visit: bool = False


class TherapyTypeUpdate(_Update):
    REQUIRED: ClassVar[frozenset[str]] = frozenset(
        {"code", "name_el", "template_label", "supports_home_visit"}
    )
    code: str | None = Field(default=None, pattern=THERAPY_CODE_PATTERN)
    name_el: str | None = Field(default=None, min_length=1, max_length=200)
    template_label: str | None = Field(default=None, min_length=1, max_length=100)
    eopyy_code: str | None = Field(default=None, pattern=EOPYY_CODE_PATTERN)
    eopyy_description: str | None = Field(default=None, max_length=200)
    supports_home_visit: bool | None = None


class TherapyTypeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name_el: str
    template_label: str
    eopyy_code: str | None
    eopyy_description: str | None
    supports_home_visit: bool
    active: bool


# ---------------------------------------------------------------- therapists
class TherapistCreate(_Request):
    full_name: str = Field(min_length=1, max_length=200)
    specialty: str | None = Field(default=None, max_length=200)
    license_no: str | None = Field(default=None, max_length=64)
    license_authority: str | None = Field(default=None, max_length=200)
    license_date: date | None = None


class TherapistUpdate(_Update):
    REQUIRED: ClassVar[frozenset[str]] = frozenset({"full_name"})
    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    specialty: str | None = Field(default=None, max_length=200)
    license_no: str | None = Field(default=None, max_length=64)
    license_authority: str | None = Field(default=None, max_length=200)
    license_date: date | None = None


class TherapistOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    full_name: str
    specialty: str | None
    license_no: str | None
    license_authority: str | None
    license_date: date | None
    active: bool
    created_at: datetime


# ---------------------------------------------------------------- responses
class WarningOut(BaseModel):
    code: str
    ids: list[int] = []


class TherapyTypeResponse(BaseModel):
    item: TherapyTypeOut
    warnings: list[WarningOut] = []


class TherapyTypeList(BaseModel):
    items: list[TherapyTypeOut]


class TherapistResponse(BaseModel):
    item: TherapistOut
    warnings: list[WarningOut] = []


class TherapistList(BaseModel):
    items: list[TherapistOut]
