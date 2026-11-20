"""Render a document from a template, its field map and the data of one document period.

Ported from the validated spike (``spike/docx_render.py``) with the same behaviour
for the real form, plus: run spans, headers/footers, per-field date formats,
explicit data checks and a package safety check. Only mapped text nodes change;
the template file is opened read-only and verified unchanged afterwards.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from docx import Document
from docx.document import Document as DocxDocument
from docx.oxml.ns import qn
from lxml import etree

from . import locate
from .errors import InvalidDocumentData, MissingValue, TemplateIntegrityError, TemplateMismatch
from .field_map import EXPECTATIONS, Block, FieldMap, HomeVisit, TextField, format_date
from .package import inspect_docx, sha256_file

UNCHECKED, CHECKED = "☐", "☒"
WATERMARK_MARK = "WaterMark"  # Word names its watermark shapes "PowerPlusWaterMarkObject"
_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}


@dataclass(frozen=True)
class DocumentData:
    """Everything printed for one document period. Session dates are planned dates
    approved by the administrator (D-08); the engine only formats them."""

    first_name: str
    last_name: str
    father_name: str
    amka: str
    period_start: date
    period_end: date
    issue_date: date
    sessions: Mapping[str, Sequence[date]]  # therapy code -> dates
    home_visit: Mapping[str, bool] = field(default_factory=dict)  # therapy code -> ΝΑΙ/ΟΧΙ
    # center settings (D-16, D-25): required for fields mapped as dynamic, must be None for
    # fields mapped as static (the template's own text is kept)
    ada: str | None = None
    representative: str | None = None

    def check(self) -> None:
        for name in ("first_name", "last_name", "father_name"):
            if not getattr(self, name).strip():
                raise InvalidDocumentData(f"{name} is empty")
        if not re.fullmatch(r"\d{11}", self.amka):
            raise InvalidDocumentData("AMKA must be exactly 11 digits")
        if self.period_end < self.period_start:
            raise InvalidDocumentData("period end is before period start")
        for code, dates in self.sessions.items():
            if not dates:
                raise MissingValue(f"no session dates for therapy {code!r}")
            outside = [d for d in dates if not self.period_start <= d <= self.period_end]
            if outside:
                raise InvalidDocumentData(
                    f"{len(outside)} session date(s) of therapy {code!r} lie outside the period"
                )
        for value, name in ((self.ada, "ada"), (self.representative, "representative")):
            if value is not None and not value.strip():
                raise InvalidDocumentData(f"{name} is blank; use None to keep the template text")


@dataclass(frozen=True)
class FieldChange:
    field: str
    old: str
    new: str


@dataclass(frozen=True)
class TextEdit:
    part: str
    old: str
    new: str


@dataclass(frozen=True)
class ExpectedValue:
    field: str
    loc_description: str
    text: str


@dataclass
class RenderResult:
    output: Path
    template_sha256: str
    changes: list[FieldChange] = field(default_factory=list)
    edits: list[TextEdit] = field(default_factory=list)
    expected: list[ExpectedValue] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)  # optional fields left as in the template
    watermark_runs_removed: int = 0


class _Renderer:
    def __init__(self, doc: DocxDocument, result: RenderResult):
        self.doc, self.result = doc, result

    def apply(self, name: str, spec: TextField, value: str) -> None:
        span = locate.resolve(self.doc, spec.loc)
        old = locate.read(span)
        if spec.between is not None:
            before, after = spec.between
            i = old.find(before)
            j = old.find(after, i + len(before)) if after else len(old)
            if i < 0 or j < 0:
                raise TemplateMismatch(
                    f"{name} ({span.where}): anchor text {before!r} / {after!r} not found"
                )
            new = old[: i + len(before)] + value + old[j:]
        else:
            if not re.match(EXPECTATIONS[spec.expect], old):
                raise TemplateMismatch(
                    f"{name} ({span.where}): template text {old!r} "
                    f"is not a {spec.expect} as expected"
                )
            new = value
        self._write(name, span, old, new)

    def _write(self, name: str, span: locate.Span, old: str, new: str) -> None:
        for o, n in locate.write(span, new):
            self.result.edits.append(TextEdit(span.part, o, n))
        self.result.changes.append(FieldChange(name, old, new))
        self.result.expected.append(ExpectedValue(name, span.where, new))

    def block(self, block: Block, data: DocumentData, fmt) -> None:
        label = locate.read(locate.resolve(self.doc, block.label_loc)).strip()
        if label != block.label:
            raise TemplateMismatch(
                f"block {block.therapy}: heading is {label!r}, expected {block.label!r}"
            )
        dates = sorted(data.sessions[block.therapy])
        text = fmt.session_separator.join(format_date(d, fmt.session_date) for d in dates)
        self.apply(
            f"sessions:{block.therapy}",
            TextField(loc=block.dates_loc, expect="date_list"),
            text,
        )
        if block.home_visit is not None:
            self.home_visit(block.therapy, block.home_visit, data.home_visit.get(block.therapy))

    def home_visit(self, therapy: str, hv: HomeVisit, value: bool | None) -> None:
        """Tick exactly one box; only the glyph changes (ported unchanged from the spike)."""
        if value is None:
            raise MissingValue(f"home visit (ΝΑΙ/ΟΧΙ) must be chosen for therapy {therapy!r}")
        for loc, word in ((hv.yes_label_loc, "ΝΑΙ"), (hv.no_label_loc, "ΟΧΙ")):
            if word not in locate.read(locate.resolve(self.doc, loc)):
                raise TemplateMismatch(
                    f"home visit {therapy}: label {word} not found at {loc.describe()}"
                )
        for loc, tick, side in ((hv.yes_box_loc, value, "yes"), (hv.no_box_loc, not value, "no")):
            span = locate.resolve(self.doc, loc)
            old = locate.read(span)
            if old != UNCHECKED:
                raise TemplateMismatch(
                    f"home visit {therapy}: box at {span.where} holds {old!r}, "
                    f"expected an empty {UNCHECKED}"
                )
            self._write(f"home_visit:{therapy}:{side}", span, old, CHECKED if tick else UNCHECKED)
            if tick and hv.checked_font:
                _pin_font(span.runs[0], hv.checked_font)


def _pin_font(run, font: str) -> None:
    rpr = run._r.get_or_add_rPr()
    fonts = rpr.find(qn("w:rFonts"))
    if fonts is None:
        fonts = rpr.makeelement(qn("w:rFonts"), {})
        rpr.insert(0, fonts)
    for attr in ("ascii", "hAnsi", "eastAsia", "cs"):
        fonts.set(qn(f"w:{attr}"), font)


def watermark_runs(element) -> list:
    return [
        r
        for r in etree.ElementBase.xpath(element, ".//w:r[w:pict]", namespaces=_NS)
        if WATERMARK_MARK in etree.tostring(r, encoding="unicode")
    ]


def remove_watermarks(doc: DocxDocument) -> int:
    """Remove Word watermark shapes from the headers of the output document only."""
    removed = 0
    for section in doc.sections:
        for header in (section.header, section.first_page_header, section.even_page_header):
            if header.is_linked_to_previous:
                continue
            for run in watermark_runs(header._element):
                run.getparent().remove(run)
                removed += 1
    return removed


def render(template: Path, field_map: FieldMap, data: DocumentData, out: Path) -> RenderResult:
    """Write the generated DOCX to ``out`` (which must not exist yet)."""
    template, out = Path(template), Path(out)
    if template.resolve() == out.resolve():
        raise ValueError("refusing to write over the template")
    if out.exists():
        raise FileExistsError(f"output already exists: {out.name}")
    info = inspect_docx(template)
    if info.sha256 != field_map.template_sha256:
        raise TemplateIntegrityError(
            "template file does not match its field map (SHA-256 differs); "
            "the file changed or the wrong version is used"
        )
    data.check()
    mapped = {b.therapy for b in field_map.blocks}
    unexpected = sorted(set(data.sessions) - mapped)
    missing = sorted(mapped - set(data.sessions))
    # D-28: a template is selected by its exact combination, so every section it contains
    # is filled; a missing therapy means the wrong template or incomplete data, never a
    # section left blank with another therapist's signature on it
    if missing:
        raise MissingValue(f"no session dates supplied for template therapies {missing}")
    unselected_home_visit = sorted(set(data.home_visit) - set(data.sessions))
    if unselected_home_visit:
        raise InvalidDocumentData(
            f"home visit given for therapies {unselected_home_visit} that are not in this document"
        )
    if unexpected:
        raise InvalidDocumentData(
            f"therapies {unexpected} are not in this template; the wrong template was selected"
        )

    doc = Document(str(template))
    result = RenderResult(output=out, template_sha256=info.sha256)
    renderer = _Renderer(doc, result)
    fmt = field_map.formats

    patient = field_map.patient_format.format(
        first=data.first_name, last=data.last_name, father=data.father_name, amka=data.amka
    )
    renderer.apply("patient", field_map.patient, patient)
    period = (
        format_date(data.period_start, fmt.period_date)
        + fmt.period_separator
        + format_date(data.period_end, fmt.period_date)
    )
    renderer.apply("period", field_map.period, period)
    renderer.apply("issue_date", field_map.issue_date, format_date(data.issue_date, fmt.issue_date))
    for name, spec, value in (
        ("ada", field_map.ada, data.ada),
        ("representative", field_map.representative, data.representative),
    ):
        if spec is None:
            continue
        if spec.mode == "static":
            if value is not None:
                raise InvalidDocumentData(
                    f"{name} is static in this template (its own text is kept); "
                    "do not supply a value, or map the field as dynamic"
                )
            result.kept.append(name)
        elif value is None:
            raise MissingValue(
                f"{name} is a dynamic field of this template; set it in the center settings"
            )
        else:
            renderer.apply(name, spec, value)
    for block in field_map.blocks:
        renderer.block(block, data, fmt)
    if field_map.remove_watermark:
        result.watermark_runs_removed = remove_watermarks(doc)

    out.parent.mkdir(parents=True, exist_ok=True)
    partial = out.with_name(out.name + ".partial")
    try:
        doc.save(str(partial))
        os.replace(partial, out)
    finally:
        partial.unlink(missing_ok=True)
    if sha256_file(template) != info.sha256:
        raise TemplateIntegrityError("the template file changed while rendering")
    return result
