"""Field map: where each dynamic value lives in a template (schema version 1).

The center's real templates contain *sample text*, not placeholder markers, so a
"placeholder" is a mapped location: a story part (body, header, footer), an
optional table cell, a paragraph and a span of runs, plus what that span must
currently contain (an expectation pattern or anchor text around the value).
Mappings are configured once per template version and bound to the template's
SHA-256 (D-07, P-12, P-28).
"""

from __future__ import annotations

import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DATE_RE = r"\d{1,2}/\d{1,2}/\d{2,4}"

# What a mapped span may contain *in the template* before rendering.
EXPECTATIONS: dict[str, str] = {
    "date_range": rf"^\s*(?:{DATE_RE} έως {DATE_RE})?\s*$",
    "date_list": rf"^\s*(?:{DATE_RE}(?:,\s*{DATE_RE})*)?\s*$",
    "date": rf"^\s*(?:{DATE_RE})?\s*$",
    "text": r"^\s*\S.*$",
}
Expectation = Literal["date_range", "date_list", "date", "text"]

PATIENT_KEYS = {"first", "last", "father", "amka"}


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Locator(_Model):
    part: Literal["body", "header", "footer"] = "body"
    section: int = Field(default=0, ge=0)
    kind: Literal["default", "first", "even"] = "default"  # which header/footer
    table: int | None = Field(default=None, ge=0)
    row: int | None = Field(default=None, ge=0)
    cell: int | None = Field(default=None, ge=0)
    paragraph: int = Field(ge=0)
    runs: tuple[int, int]  # inclusive run range; (n, n) for a single run

    @model_validator(mode="after")
    def _consistent(self) -> Locator:
        cell_parts = (self.table, self.row, self.cell)
        if any(v is None for v in cell_parts) and any(v is not None for v in cell_parts):
            raise ValueError("table, row and cell must be given together")
        if not 0 <= self.runs[0] <= self.runs[1]:
            raise ValueError("runs must be (start, end) with 0 <= start <= end")
        if self.part == "body" and (self.section, self.kind) != (0, "default"):
            raise ValueError("section/kind only apply to headers and footers")
        return self

    def describe(self) -> str:
        where = (
            self.part
            if self.part == "body"
            else f"{self.kind} {self.part} of section {self.section}"
        )
        if self.table is not None:
            where += f", table {self.table} row {self.row} cell {self.cell}"
        start, end = self.runs
        span = f"run {start}" if start == end else f"runs {start}-{end}"
        return f"{where}, paragraph {self.paragraph}, {span}"


class TextField(_Model):
    """Either the whole span must match ``expect`` and is replaced, or only the text
    between the ``between`` anchors is replaced (``""`` as second anchor = to the end)."""

    loc: Locator
    expect: Expectation | None = None
    between: tuple[str, str] | None = None

    @model_validator(mode="after")
    def _one_mode(self) -> TextField:
        if (self.expect is None) == (self.between is None):
            raise ValueError("give exactly one of 'expect' or 'between'")
        if self.between is not None and not self.between[0]:
            raise ValueError("the first 'between' anchor must not be empty")
        return self


class CenterField(TextField):
    """A center-level field (ΑΔΑ, representative). The mapping must say which kind it is:

    ``dynamic``  the value comes from the center settings and is required at generation;
                 a missing value is an error, never a silent fallback to the template text
    ``static``   the template's own text is authoritative and is never replaced; supplying
                 a value for it is an error (D-25)
    """

    mode: Literal["dynamic", "static"]


class LabeledValue(_Model):
    """A fixed value printed after a label (therapist details). Never modified."""

    label: str
    label_loc: Locator
    value_loc: Locator


class TherapistDetails(_Model):
    license_no: LabeledValue
    authority: LabeledValue
    license_date: LabeledValue
    name: LabeledValue | None = None  # only if the template prints the therapist's name


class HomeVisit(_Model):
    yes_label_loc: Locator
    yes_box_loc: Locator
    no_label_loc: Locator
    no_box_loc: Locator
    checked_font: str | None = None  # P-13: off by default


class Block(_Model):
    therapy: str = Field(min_length=1)  # therapy_types.code
    label: str = Field(min_length=1)  # heading as printed, e.g. "Λογοθεραπεία:"
    label_loc: Locator
    dates_loc: Locator
    therapist_details: TherapistDetails
    home_visit: HomeVisit | None = None


class DateFormats(_Model):
    """Tokens: d, dd, M, MM, yy, yyyy; separators / . - and space."""

    period_date: str = "d/MM/yyyy"  # 2/09/2026
    session_date: str = "d/M/yy"  # 3/9/26
    issue_date: str = "d/MM/yyyy"  # 19/10/2026
    period_separator: str = " έως "
    session_separator: str = ", "

    @field_validator("period_date", "session_date", "issue_date")
    @classmethod
    def _valid_format(cls, value: str) -> str:
        if not re.fullmatch(r"(dd?|MM?|yyyy|yy|[/.\- ])+", value) or not all(
            t in value for t in ("d", "M", "y")
        ):
            raise ValueError(f"unsupported date format {value!r}")
        return value


class FieldMap(_Model):
    schema_version: Literal[1] = 1
    template_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    patient_format: str = "{first} {last} του {father} με ΑΜΚΑ {amka}"
    formats: DateFormats = DateFormats()
    patient: TextField
    period: TextField
    issue_date: TextField
    ada: CenterField | None = None  # center setting (D-16, D-25)
    representative: CenterField | None = None  # center setting (D-16, D-25)
    blocks: tuple[Block, ...] = Field(min_length=1)
    remove_watermark: bool = True
    # extra template sample strings that must never survive into a generated document
    forbidden_text: tuple[str, ...] = ()

    @field_validator("patient_format")
    @classmethod
    def _patient_format(cls, value: str) -> str:
        keys = set(re.findall(r"{(\w*)}", value))
        if keys != PATIENT_KEYS:
            raise ValueError(f"patient_format must use exactly {sorted(PATIENT_KEYS)}")
        return value

    @field_validator("blocks")
    @classmethod
    def _unique_therapies(cls, blocks: tuple[Block, ...]) -> tuple[Block, ...]:
        codes = [b.therapy for b in blocks]
        if len(codes) != len(set(codes)):
            raise ValueError("each therapy may be mapped only once")
        return blocks

    def text_fields(self) -> dict[str, TextField]:
        fields = {"patient": self.patient, "period": self.period, "issue_date": self.issue_date}
        if self.ada is not None:
            fields["ada"] = self.ada
        if self.representative is not None:
            fields["representative"] = self.representative
        return fields


_TOKEN = re.compile(r"yyyy|yy|dd|d|MM|M")


def format_date(value: date, fmt: str) -> str:
    def token(m: re.Match[str]) -> str:
        return {
            "yyyy": f"{value.year:04d}",
            "yy": f"{value.year % 100:02d}",
            "dd": f"{value.day:02d}",
            "d": str(value.day),
            "MM": f"{value.month:02d}",
            "M": str(value.month),
        }[m.group()]

    return _TOKEN.sub(token, fmt)
