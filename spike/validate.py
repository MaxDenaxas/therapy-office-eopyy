"""Template validation: must be clean before a template can be activated (spike).

Errors block activation. Warnings are shown to the administrator.
"""
from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from lxml import etree

from docx_render import (DATE_RE, EXPECT, MissingValue, RenderData, TemplateMismatch,
                         UNCHECKED, _resolve_run, _run_text, render, sha256_file,
                         verify_images_untouched, verify_preserved)

REQUIRED_GLOBAL = ("patient", "period", "issue_date")
OPTIONAL_GLOBAL = ("ada", "representative")
REQUIRED_DETAILS = ("license_no", "authority", "license_date")
_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
BLANK_RE = re.compile(r"^[\s.…_\-–]*$")  # tabs, dots, underscores: a blank form line


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _check_run(doc, rep: ValidationReport, what: str, spec: dict) -> None:
    try:
        run = _resolve_run(doc, spec["loc"])
        text = _run_text(run)
    except (TemplateMismatch, KeyError) as e:
        rep.errors.append(f"{what}: cannot be located reliably ({e})")
        return
    if "between" in spec:
        before, after = spec["between"]
        i = text.find(before)
        if i < 0 or (after and text.find(after, i + len(before)) < 0):
            rep.errors.append(f"{what}: anchor text {spec['between']} not found (run holds {text!r})")
    elif not re.match(EXPECT[spec["expect"]], text):
        rep.errors.append(f"{what}: current text {text!r} is not what the mapping expects")


def validate_template(template: Path, field_map: dict) -> ValidationReport:
    rep = ValidationReport()
    template = Path(template)
    if not template.is_file():
        rep.errors.append(f"Template file not found: {template}")
        return rep
    try:
        doc = Document(str(template))
    except Exception as e:  # not a readable DOCX
        rep.errors.append(f"Not a readable DOCX: {e}")
        return rep
    if field_map.get("sha256") and field_map["sha256"] != sha256_file(template):
        rep.errors.append("File does not match the SHA-256 recorded in the field map")

    # --- global fields -------------------------------------------------------
    fields = field_map.get("fields", {})
    for name in REQUIRED_GLOBAL:
        if name not in fields:
            rep.errors.append(f"Required field '{name}' is not mapped")
        else:
            _check_run(doc, rep, f"Field '{name}'", fields[name])
    for name in OPTIONAL_GLOBAL:
        if name in fields:
            _check_run(doc, rep, f"Field '{name}'", fields[name])
    if "patient_format" not in field_map:
        rep.errors.append("patient_format is missing from the field map")

    # --- blocks --------------------------------------------------------------
    blocks = field_map.get("blocks", [])
    if not blocks:
        rep.errors.append("Template maps no therapy blocks")
    if len(doc.tables) != len(blocks):
        rep.errors.append(f"Template has {len(doc.tables)} tables but {len(blocks)} are mapped; "
                          "every therapy section must be mapped so nothing stale is left behind")
    seen = set()
    pairs = set()
    mapped_runs = set()  # w:t elements that are legitimately fixed or dynamic
    for b in blocks:
        tid = b.get("therapy", "?")
        where = f"Block '{tid}'"
        if tid in seen:
            rep.errors.append(f"{where}: therapy mapped more than once")
        seen.add(tid)
        if not b.get("therapist"):
            rep.errors.append(f"{where}: no therapist assigned in the configuration")
        pairs.add((tid, b.get("therapist")))
        try:
            label = _run_text(_resolve_run(doc, b["label_loc"])).strip()
            if label != b["label"]:
                rep.errors.append(f"{where}: heading is {label!r}, expected {b['label']!r}")
        except (TemplateMismatch, KeyError) as e:
            rep.errors.append(f"{where}: heading cannot be located ({e})")
        _check_run(doc, rep, f"{where} session dates", {"loc": b.get("dates_loc", {}), "expect": "dates"})

        details = b.get("therapist_details", {})
        for key in REQUIRED_DETAILS:
            if key not in details:
                rep.errors.append(f"{where}: therapist detail '{key}' is not mapped")
                continue
            d = details[key]
            try:
                lab = _run_text(_resolve_run(doc, d["label_loc"])).strip()
                if lab != d["label"]:
                    rep.errors.append(f"{where}: label for '{key}' is {lab!r}, expected {d['label']!r}")
                val = _run_text(_resolve_run(doc, d["value_loc"]))
            except (TemplateMismatch, KeyError) as e:
                rep.errors.append(f"{where}: therapist detail '{key}' cannot be located ({e})")
                continue
            if BLANK_RE.match(val):
                rep.errors.append(f"{where}: therapist detail '{key}' is empty in the template")
            elif key == "license_date" and not re.fullmatch(DATE_RE, val.strip()):
                rep.errors.append(f"{where}: license date {val!r} is not a valid date")

        # an unmapped home-visit line would silently keep whatever the template says
        table_text = "\n".join(p.text for p in doc.tables[b["label_loc"]["table"]]
                               .rows[0].cells[0].paragraphs) if "label_loc" in b else ""
        has_hv_text = "Κατ’ οίκον" in table_text or "Κατ' οίκον" in table_text
        if has_hv_text and "home_visit" not in b:
            rep.errors.append(f"{where}: contains a home-visit (ΝΑΙ/ΟΧΙ) line that is not mapped")
        if "home_visit" in b:
            hv = b["home_visit"]
            for k, word in (("yes_label_loc", "ΝΑΙ"), ("no_label_loc", "ΟΧΙ")):
                try:
                    if word not in _run_text(_resolve_run(doc, hv[k])):
                        rep.errors.append(f"{where}: home-visit label {word} not found")
                except (TemplateMismatch, KeyError) as e:
                    rep.errors.append(f"{where}: home-visit {word} label cannot be located ({e})")
            for k in ("yes_box_loc", "no_box_loc"):
                try:
                    txt = _run_text(_resolve_run(doc, hv[k]))
                    if txt != UNCHECKED:
                        rep.errors.append(f"{where}: home-visit checkbox holds {txt!r}; the template "
                                          f"must ship with both boxes empty ({UNCHECKED})")
                except (TemplateMismatch, KeyError) as e:
                    rep.errors.append(f"{where}: home-visit checkbox cannot be located ({e})")

    declared = {tuple(x) for x in field_map.get("configuration_key", [])}
    if declared != pairs:
        rep.errors.append("configuration_key does not equal the (therapy, therapist) pairs of the blocks")

    # --- warnings about content the mapper cannot see ------------------------
    _scan_warnings(doc, field_map, rep)

    if rep.errors:  # a dry run on a template known to be broken adds only noise
        return rep
    _dry_run(template, field_map, rep)
    return rep


def _scan_warnings(doc, field_map: dict, rep: ValidationReport) -> None:
    fixed: set[str] = set()
    for b in field_map.get("blocks", []):
        for d in b.get("therapist_details", {}).values():
            try:
                fixed.add(_run_text(_resolve_run(doc, d["value_loc"])))
            except (TemplateMismatch, KeyError):
                pass
    parts = [("body", doc.element.body)]
    for i, sec in enumerate(doc.sections):
        for nm, hf in (("header", sec.header), ("footer", sec.footer),
                       ("first-page header", sec.first_page_header), ("first-page footer", sec.first_page_footer)):
            if not hf.is_linked_to_previous:
                parts.append((f"section {i + 1} {nm}", hf._element))
    mapped_texts = set()
    for name in ("patient", "period", "issue_date", "ada", "representative"):
        if name in field_map.get("fields", {}):
            try:
                mapped_texts.add(_run_text(_resolve_run(doc, field_map["fields"][name]["loc"])))
            except (TemplateMismatch, KeyError):
                pass
    for b in field_map.get("blocks", []):
        try:
            mapped_texts.add(_run_text(_resolve_run(doc, b["dates_loc"])))
        except (TemplateMismatch, KeyError):
            pass
    for where, el in parts:
        for t in etree.ElementBase.xpath(el, ".//w:t", namespaces=_NS):
            text = t.text or ""
            if text in mapped_texts or text in fixed:
                continue
            if re.search(DATE_RE, text):
                rep.warnings.append(f"Unmapped date-like text in {where}: {text.strip()[:60]!r} "
                                    "(it will NOT be updated)")
        if etree.ElementBase.xpath(el, ".//w:txbxContent", namespaces=_NS):
            rep.warnings.append(f"{where} contains text boxes; their text cannot be mapped or updated")
        if etree.ElementBase.xpath(el, ".//w:sdt", namespaces=_NS):
            rep.warnings.append(f"{where} contains content controls; their text cannot be mapped or updated")


def _dry_run(template: Path, field_map: dict, rep: ValidationReport) -> None:
    """Render with probe values; the output must contain every probe value and differ from
    the template only in the mapped runs (and not at all in the images)."""
    probe_start, probe_end = date(2031, 3, 3), date(2031, 4, 1)
    therapies = [b["therapy"] for b in field_map["blocks"]]
    data = RenderData(
        first_name="ΠΡΟΒΑ", last_name="ΕΛΕΓΧΟΥ", father_name="ΠΑΤΡΟΣ", amka="99999999999",
        period_start=probe_start, period_end=probe_end, issue_date=date(2031, 4, 2),
        sessions={t: [date(2031, 3, 5), date(2031, 3, 12)] for t in therapies},
        ada="ΠΡΟΒΑ-ΑΔΑ", representative="ΠΡΟΒΑ ΕΚΠΡΟΣΩΠΟΣ",
        home_visit={b["therapy"]: True for b in field_map["blocks"] if "home_visit" in b},
    )
    try:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "dryrun.docx"
            res = render(template, field_map, data, out)
            rep.errors += [f"Dry run: {p}" for p in verify_preserved(template, out, res.changes)]
            rep.errors += [f"Dry run: {p}" for p in verify_images_untouched(template, out)]
            text = "".join(t.text or "" for t in Document(str(out)).element.body.iter(qn("w:t")))
            for c in res.changes:
                if c.field != "watermark" and c.new not in text:
                    rep.errors.append(f"Dry run: value for '{c.field}' did not appear in the output")
    except (TemplateMismatch, MissingValue) as e:
        rep.errors.append(f"Dry run failed: {e}")
