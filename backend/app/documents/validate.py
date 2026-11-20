"""Template validation: the gate a template version must pass before activation
(ported from ``spike/validate.py``).

Errors block activation; warnings are shown to the administrator. Checks:
  * the file is a safe, well-formed DOCX and is the file the map was made for
  * every mapped location exists and currently holds what the map expects
  * every therapy section of the body is mapped, headings match, the therapist
    details are present (licence number, authority, a real licence date)
  * home-visit lines are mapped and ship with both boxes empty
  * a dry run with probe values renders, verifies and leaves images untouched
Warnings: date-like text the map does not cover (it would never be updated), and
text boxes, content controls or Word fields (their text cannot be mapped).
The therapy/therapist *configuration* of a template is checked against the
database when templates are registered (M4), not here.
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from docx import Document
from docx.document import Document as DocxDocument

from . import locate
from .errors import DocumentError, TemplateFileError
from .field_map import DATE_RE, EXPECTATIONS, FieldMap, Locator, TextField
from .package import inspect_docx
from .render import UNCHECKED, DocumentData, render
from .verify import verify_document

BLANK_RE = re.compile(r"^[\s.…_\-–]*$")  # tabs, dots, underscores: a blank form line
HOME_VISIT_MARKERS = ("Κατ’ οίκον", "Κατ' οίκον")
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _read(doc: DocxDocument, loc: Locator) -> str:
    return locate.read(locate.resolve(doc, loc))


def _check_text_field(
    doc: DocxDocument, report: ValidationReport, name: str, spec: TextField
) -> None:
    try:
        text = _read(doc, spec.loc)
    except DocumentError as e:
        report.errors.append(f"{name}: {e}")
        return
    if spec.between is not None:
        before, after = spec.between
        i = text.find(before)
        if i < 0 or (after and text.find(after, i + len(before)) < 0):
            report.errors.append(
                f"{name}: anchor text {before!r} / {after!r} not found at {spec.loc.describe()}"
            )
    elif not re.match(EXPECTATIONS[spec.expect], text):
        report.errors.append(
            f"{name}: template text {text!r} at {spec.loc.describe()} is not "
            f"a {spec.expect} as the map expects"
        )


def validate_template(template: Path, field_map: FieldMap) -> ValidationReport:
    report = ValidationReport()
    try:
        info = inspect_docx(template)
        doc = Document(str(template))
    except TemplateFileError as e:
        report.errors.append(f"template file: {e}")
        return report
    except Exception as e:  # noqa: BLE001 - python-docx raises several unrelated types
        report.errors.append(f"template file cannot be opened as a DOCX ({type(e).__name__})")
        return report
    if info.sha256 != field_map.template_sha256:
        report.errors.append("template file does not match the SHA-256 recorded in its field map")

    for name, spec in field_map.text_fields().items():
        _check_text_field(doc, report, name, spec)
    for name, spec in (("ada", field_map.ada), ("representative", field_map.representative)):
        if spec is not None and spec.mode == "static":
            _check_static_value(doc, report, name, spec)

    body_tables = len(doc.tables)
    if body_tables != len(field_map.blocks):
        report.errors.append(
            f"the template has {body_tables} tables but {len(field_map.blocks)} therapy sections "
            "are mapped; every section must be mapped so nothing stale is left behind"
        )
    for block in field_map.blocks:
        where = f"section {block.therapy!r}"
        try:
            heading = _read(doc, block.label_loc).strip()
            if heading != block.label:
                report.errors.append(f"{where}: heading is {heading!r}, expected {block.label!r}")
        except DocumentError as e:
            report.errors.append(f"{where}: heading: {e}")
        _check_text_field(
            doc,
            report,
            f"{where} session dates",
            TextField(loc=block.dates_loc, expect="date_list"),
        )
        _check_details(doc, report, where, block)
        _check_home_visit(doc, report, where, block)

    _scan_warnings(doc, field_map, report)
    if not report.errors:  # a dry run on a template known to be broken only adds noise
        _dry_run(Path(template), field_map, report)
    return report


def _check_static_value(doc, report: ValidationReport, name: str, spec) -> None:
    """A static field is printed exactly as the template has it, so it must not be blank."""
    try:
        text = _read(doc, spec.loc)
    except DocumentError:
        return  # already reported by the location check
    value = text
    if spec.between is not None:
        before, after = spec.between
        i = text.find(before)
        if i < 0:
            return  # already reported by the anchor check
        start = i + len(before)
        end = text.find(after, start) if after else len(text)
        value = text[start:end] if end >= 0 else ""
    if BLANK_RE.match(value):
        report.errors.append(
            f"{name} is mapped as static but is blank in the template; "
            "fill it in the template or map it as dynamic"
        )


def _check_details(doc, report: ValidationReport, where: str, block) -> None:
    for key, detail in block.therapist_details:
        if detail is None:  # optional (e.g. the name, when the template does not print it)
            continue
        try:
            label = _read(doc, detail.label_loc).strip()
            value = _read(doc, detail.value_loc)
        except DocumentError as e:
            report.errors.append(f"{where}: therapist detail {key!r}: {e}")
            continue
        if label != detail.label.strip():
            report.errors.append(
                f"{where}: label of {key!r} is {label!r}, expected {detail.label!r}"
            )
        if BLANK_RE.match(value):
            report.errors.append(f"{where}: therapist detail {key!r} is empty in the template")
        elif key == "license_date" and not re.fullmatch(DATE_RE, value.strip()):
            report.errors.append(f"{where}: licence date {value!r} is not a date")


def _check_home_visit(doc, report: ValidationReport, where: str, block) -> None:
    loc = block.label_loc
    section_text = ""
    if loc.part == "body" and loc.table is not None:
        try:
            cell = doc.tables[loc.table].rows[loc.row].cells[loc.cell]
            section_text = "\n".join(p.text for p in cell.paragraphs)
        except IndexError:
            pass  # already reported by the heading check
    if any(m in section_text for m in HOME_VISIT_MARKERS) and block.home_visit is None:
        report.errors.append(f"{where}: contains a home-visit (ΝΑΙ/ΟΧΙ) line that is not mapped")
    hv = block.home_visit
    if hv is None:
        return
    for label_loc, word in ((hv.yes_label_loc, "ΝΑΙ"), (hv.no_label_loc, "ΟΧΙ")):
        try:
            if word not in _read(doc, label_loc):
                report.errors.append(f"{where}: home-visit label {word} not found")
        except DocumentError as e:
            report.errors.append(f"{where}: home-visit label {word}: {e}")
    for box in (hv.yes_box_loc, hv.no_box_loc):
        try:
            text = _read(doc, box)
        except DocumentError as e:
            report.errors.append(f"{where}: home-visit box: {e}")
            continue
        if text != UNCHECKED:
            report.errors.append(
                f"{where}: home-visit box holds {text!r}; the template must "
                f"ship with both boxes empty ({UNCHECKED})"
            )


def _scan_warnings(doc: DocxDocument, field_map: FieldMap, report: ValidationReport) -> None:
    covered: set[str] = set()
    locations = [spec.loc for spec in field_map.text_fields().values()]
    for block in field_map.blocks:
        locations.append(block.dates_loc)
        locations += [d.value_loc for _, d in block.therapist_details if d is not None]
    for loc in locations:
        try:
            text = _read(doc, loc)
        except DocumentError:
            continue  # reported elsewhere
        if re.search(DATE_RE, text):
            covered.add(text)
    stories = [("body", doc.element.body)]
    for i, section in enumerate(doc.sections):
        for name, story in (
            ("header", section.header),
            ("footer", section.footer),
            ("first-page header", section.first_page_header),
            ("first-page footer", section.first_page_footer),
        ):
            if not story.is_linked_to_previous:
                stories.append((f"section {i} {name}", story._element))
    for where, element in stories:
        for paragraph in element.iter(_W + "p"):
            text = "".join(t.text or "" for t in paragraph.iter(_W + "t"))
            if re.search(DATE_RE, text) and not any(c and c in text for c in covered):
                report.warnings.append(
                    f"unmapped date-like text in the {where}: {text.strip()[:60]!r}"
                    " (it will NOT be updated)"
                )
        for tag, what in (
            ("txbxContent", "text boxes"),
            ("sdt", "content controls"),
            ("fldSimple", "Word fields"),
            ("instrText", "Word fields"),
        ):
            if next(element.iter(_W + tag), None) is not None:
                report.warnings.append(f"the {where} contains {what}; their text cannot be mapped")


def _dry_run(template: Path, field_map: FieldMap, report: ValidationReport) -> None:
    """Render with probe values (all invented) for every section and run the full
    verification."""
    for selection in ([b.therapy for b in field_map.blocks],):
        probe = DocumentData(
            first_name="ΠΡΟΒΑ",
            last_name="ΕΛΕΓΧΟΥ",
            father_name="ΠΑΤΡΟΣ",
            amka="99999999999",
            period_start=date(2031, 3, 3),
            period_end=date(2031, 4, 2),
            issue_date=date(2031, 4, 3),
            sessions={code: [date(2031, 3, 5), date(2031, 3, 12)] for code in selection},
            home_visit={
                b.therapy: True
                for b in field_map.blocks
                if b.home_visit is not None and b.therapy in selection
            },
            ada="ΠΡΟΒΑ-ΑΔΑ" if field_map.ada and field_map.ada.mode == "dynamic" else None,
            representative=(
                "ΠΡΟΒΑ ΕΚΠΡΟΣΩΠΟΣ"
                if field_map.representative and field_map.representative.mode == "dynamic"
                else None
            ),
        )
        what = "all sections"
        with tempfile.TemporaryDirectory(prefix="therapy-dryrun-") as tmp:
            out = Path(tmp) / "dry-run.docx"
            try:
                result = render(template, field_map, probe, out)
            except DocumentError as e:
                report.errors.append(f"dry run ({what}) failed: {e}")
                return
            report.errors += [
                f"dry run ({what}): {p}" for p in verify_document(template, out, result, field_map)
            ]
