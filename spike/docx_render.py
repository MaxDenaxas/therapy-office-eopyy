"""Field-map driven DOCX renderer (spike).

Only the text of explicitly mapped runs is changed. The template file is never
opened for writing; output goes to a new path, and the template's SHA-256 is
checked before and after.
"""
from __future__ import annotations

import hashlib
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from lxml import etree

XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"

DATE_RE = r"\d{1,2}/\d{1,2}/\d{2,4}"
EXPECT = {
    "period": rf"^\s*(?:{DATE_RE} έως {DATE_RE})?\s*$",
    "dates": rf"^\s*(?:{DATE_RE}(?:,\s*{DATE_RE})*)?\s*$",
    "date": rf"^\s*(?:{DATE_RE})?\s*$",
    "name": r"^\s*\S.*$",
}


UNCHECKED, CHECKED = "☐", "☒"


class TemplateMismatch(Exception):
    """The template no longer matches its field map. Never guess; fail loudly."""


class MissingValue(ValueError):
    """A value the template needs was not supplied (e.g. home-visit ΝΑΙ/ΟΧΙ)."""


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fmt_period_date(d: date) -> str:  # 2/09/2026
    return f"{d.day}/{d.month:02d}/{d.year}"


def fmt_session_date(d: date) -> str:  # 3/9/26
    return f"{d.day}/{d.month}/{d.year % 100:02d}"


def fmt_issue_date(d: date) -> str:  # 19/10/2026
    return f"{d.day}/{d.month:02d}/{d.year}"


@dataclass
class RenderData:
    first_name: str
    last_name: str
    father_name: str
    amka: str
    period_start: date
    period_end: date
    issue_date: date
    # therapy_id -> sorted confirmed session dates
    sessions: dict[str, list[date]]
    # centre-level settings; None = leave the template's own text untouched
    ada: str | None = None
    representative: str | None = None
    # therapy_id -> True (ΝΑΙ) / False (ΟΧΙ); required for blocks that map a home-visit field
    home_visit: dict[str, bool] = field(default_factory=dict)


@dataclass
class Change:
    field: str
    old: str
    new: str


@dataclass
class RenderResult:
    output: Path
    changes: list[Change] = field(default_factory=list)


# ---------------------------------------------------------------- run helpers
def _run_text(run) -> str:
    r = run._r
    ts = r.findall(qn("w:t"))
    others = [c for c in r if c.tag not in (qn("w:rPr"), qn("w:t"))]
    if len(ts) != 1 or others:
        raise TemplateMismatch(
            "Mapped run must contain exactly one w:t and nothing else "
            f"(found {len(ts)} w:t, {len(others)} other children)"
        )
    return ts[0].text or ""


def _set_run_text(run, text: str) -> None:
    # Edit w:t directly: python-docx's run.text setter would rebuild the run's
    # children and reinterpret tabs.
    t = run._r.find(qn("w:t"))
    t.text = text
    t.set(XML_SPACE, "preserve")


def _resolve_run(doc, loc: dict):
    try:
        if "table" in loc:
            cell = doc.tables[loc["table"]].rows[loc["row"]].cells[loc["cell"]]
            return cell.paragraphs[loc["p"]].runs[loc["r"]]
        return doc.paragraphs[loc["p"]].runs[loc["r"]]
    except (IndexError, KeyError) as e:
        raise TemplateMismatch(f"Locator {loc} does not exist in the template") from e


def _apply(doc, name: str, spec: dict, new_text_fn, changes: list[Change]) -> None:
    run = _resolve_run(doc, spec["loc"])
    old = _run_text(run)
    if "between" in spec:
        before, after = spec["between"]
        i = old.find(before)
        j = old.find(after, i + len(before)) if after else len(old)  # "" = to end of run
        if i < 0 or j < 0 or j < i + len(before):
            raise TemplateMismatch(f"{name}: anchors {spec['between']} not found in run")
        i += len(before)
        new = old[:i] + new_text_fn(old[i:j]) + old[j:]
    else:
        if not re.match(EXPECT[spec["expect"]], old):
            raise TemplateMismatch(f"{name}: unexpected current text {old!r}")
        new = new_text_fn(old)
    _set_run_text(run, new)
    changes.append(Change(name, old, new))


def _force_font(run, font: str) -> None:
    """Pin the glyph font on the ticked box only. Without this LibreOffice substitutes a narrower
    fallback font for U+2612, which shifts the ΟΧΙ label. Size, colour etc. are untouched."""
    rpr = run._r.get_or_add_rPr()
    rf = rpr.find(qn("w:rFonts"))
    if rf is None:
        rf = rpr.makeelement(qn("w:rFonts"), {})
        rpr.insert(0, rf)
    for attr in ("ascii", "hAnsi", "eastAsia", "cs"):
        rf.set(qn(f"w:{attr}"), font)


def _apply_home_visit(doc, tid: str, spec: dict, value: bool | None, changes: list[Change]) -> None:
    """Tick exactly one of the ΝΑΙ / ΟΧΙ boxes. Only the glyph's text changes, so run
    formatting, spacing and position are untouched."""
    if value is None:
        raise MissingValue(f"Home-visit (ΝΑΙ/ΟΧΙ) must be chosen for {tid}")
    for key, word in (("yes_label_loc", "ΝΑΙ"), ("no_label_loc", "ΟΧΙ")):
        if word not in _run_text(_resolve_run(doc, spec[key])):
            raise TemplateMismatch(f"{tid}: label {word} not found where the field map expects it")
    for key, tick in (("yes_box_loc", value), ("no_box_loc", not value)):
        run = _resolve_run(doc, spec[key])
        old = _run_text(run)
        if old != UNCHECKED:
            raise TemplateMismatch(f"{tid}: checkbox run holds {old!r}, expected an empty {UNCHECKED}")
        new = CHECKED if tick else UNCHECKED
        _set_run_text(run, new)
        if tick and spec.get("checked_font"):
            _force_font(run, spec["checked_font"])
        changes.append(Change(f"home_visit:{tid}:{'yes' if key.startswith('yes') else 'no'}", old, new))


# ---------------------------------------------------------------- watermark
_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
WATERMARK_MARK = "WaterMark"  # Word names its watermark shapes "PowerPlusWaterMarkObject"


def _watermark_runs(header_el) -> list:
    return [r for r in etree.ElementBase.xpath(header_el, ".//w:r[w:pict]", namespaces=_NS)
            if WATERMARK_MARK in etree.tostring(r, encoding="unicode")]


def remove_watermarks(doc) -> int:
    """Remove Word watermark shapes from the headers of the OUTPUT document only."""
    n = 0
    for sec in doc.sections:
        for hdr in (sec.header, sec.first_page_header, sec.even_page_header):
            if hdr.is_linked_to_previous:
                continue
            for r in _watermark_runs(hdr._element):
                r.getparent().remove(r)
                n += 1
    return n


# ---------------------------------------------------------------- main render
def render(template: Path, field_map: dict, data: RenderData, out: Path) -> RenderResult:
    template, out = Path(template), Path(out)
    if template.resolve() == out.resolve():
        raise ValueError("Refusing to write over the template")
    sha_before = sha256_file(template)
    if field_map.get("sha256") and field_map["sha256"] != sha_before:
        raise TemplateMismatch("Template file does not match the registered SHA-256")

    doc = Document(str(template))
    changes: list[Change] = []
    f = field_map["fields"]

    patient = field_map["patient_format"].format(
        first=data.first_name, last=data.last_name, father=data.father_name, amka=data.amka
    )
    _apply(doc, "patient", f["patient"], lambda _old: patient, changes)
    _apply(
        doc, "period", f["period"],
        lambda _old: f"{fmt_period_date(data.period_start)} έως {fmt_period_date(data.period_end)}",
        changes,
    )
    _apply(doc, "issue_date", f["issue_date"], lambda _old: fmt_issue_date(data.issue_date), changes)
    if "ada" in f and data.ada is not None:
        _apply(doc, "ada", f["ada"], lambda _old: data.ada, changes)
    if "representative" in f and data.representative is not None:
        _apply(doc, "representative", f["representative"], lambda _old: data.representative, changes)

    for block in field_map["blocks"]:
        tid = block["therapy"]
        label_run = _resolve_run(doc, block["label_loc"])
        if _run_text(label_run).strip() != block["label"]:
            raise TemplateMismatch(f"Block {tid}: label anchor {block['label']!r} not found")
        if tid not in data.sessions:
            raise TemplateMismatch(f"No sessions supplied for template block {tid}")
        text = ", ".join(fmt_session_date(d) for d in sorted(data.sessions[tid]))
        _apply(doc, f"sessions:{tid}", {"loc": block["dates_loc"], "expect": "dates"},
               lambda _old, text=text: text, changes)
        if "home_visit" in block:
            _apply_home_visit(doc, tid, block["home_visit"], data.home_visit.get(tid), changes)

    if field_map.get("remove_watermark", True):
        removed = remove_watermarks(doc)
        if removed:
            changes.append(Change("watermark", f"{removed} watermark run(s)", "removed"))

    out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out))
    if sha256_file(template) != sha_before:
        raise RuntimeError("Template changed during render. This must never happen")
    return RenderResult(out, changes)


# ---------------------------------------------------------------- verification
def _c14n(el) -> bytes:
    return etree.tostring(el, method="c14n")


def _norm_unordered(xml: bytes) -> list[bytes]:
    root = etree.fromstring(xml)
    return sorted(_c14n(c) for c in root)


def verify_preserved(template: Path, output: Path, changes: list[Change]) -> list[str]:
    """Return a list of problems (empty = everything outside mapped runs is intact)."""
    problems: list[str] = []
    with zipfile.ZipFile(template) as zt, zipfile.ZipFile(output) as zo:
        nt, no = set(zt.namelist()), set(zo.namelist())
        if nt != no:
            problems.append(f"package parts differ: only-template={sorted(nt-no)} only-output={sorted(no-nt)}")
        for name in sorted(nt & no):
            a, b = zt.read(name), zo.read(name)
            if name == "word/document.xml":
                problems += _verify_document_xml(a, b, changes)
            elif name.endswith((".rels", "[Content_Types].xml")):
                if _norm_unordered(a) != _norm_unordered(b):
                    problems.append(f"{name}: relationships/content types differ")
            elif name.startswith("word/header") and name.endswith(".xml"):
                ea, eb = etree.fromstring(a), etree.fromstring(b)
                if any(c.field == "watermark" for c in changes):
                    for r in _watermark_runs(ea):
                        r.getparent().remove(r)
                if _watermark_runs(eb):
                    problems.append(f"{name}: watermark still present in output")
                if _c14n(ea) != _c14n(eb):
                    problems.append(f"{name}: header differs beyond watermark removal")
            elif name.endswith(".xml"):
                if _c14n(etree.fromstring(a)) != _c14n(etree.fromstring(b)):
                    problems.append(f"{name}: XML differs")
            elif a != b:
                problems.append(f"{name}: binary part differs")
    return problems


def _verify_document_xml(a: bytes, b: bytes, changes: list[Change]) -> list[str]:
    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    ra, rb = etree.fromstring(a), etree.fromstring(b)
    ta, tb = list(ra.iter(W + "t")), list(rb.iter(W + "t"))
    problems = []
    if len(ta) != len(tb):
        return [f"document.xml: w:t count {len(ta)} -> {len(tb)}"]
    diffs = [(x.text or "", y.text or "") for x, y in zip(ta, tb) if (x.text or "") != (y.text or "")]
    expected = [(c.old, c.new) for c in changes if c.old != c.new and c.field != "watermark"]
    if sorted(diffs) != sorted(expected):
        problems.append(f"document.xml: text diffs {diffs} != expected {expected}")
    for root in (ra, rb):  # the pinned glyph font on ☐/☒ runs is the one allowed formatting change
        for r in root.iter(W + "r"):
            if "".join(t.text or "" for t in r.iter(W + "t")) in (UNCHECKED, CHECKED):
                rpr = r.find(W + "rPr")
                if rpr is not None and rpr.find(W + "rFonts") is not None:
                    rpr.remove(rpr.find(W + "rFonts"))
    for el in list(ra.iter(W + "t")) + list(rb.iter(W + "t")):
        el.text = ""
    if _c14n(ra) != _c14n(rb):
        problems.append("document.xml: structure/formatting differs outside w:t text")
    return problems


def verify_images_untouched(template: Path, output: Path) -> list[str]:
    """Images must not be replaced, moved, resized, cropped or rotated.

    Checks (a) every media file is byte-identical, and (b) every DrawingML/VML
    placement element (size, position, anchoring, crop, rotation) is canonically
    identical in the document body, headers and footers."""
    problems: list[str] = []
    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(template) as zt, zipfile.ZipFile(output) as zo:
        media = [n for n in zt.namelist() if n.startswith("word/media/")]
        for n in media:
            if n not in zo.namelist() or zt.read(n) != zo.read(n):
                problems.append(f"media file changed or missing: {n}")
        for n in zt.namelist():
            if not (n == "word/document.xml" or n.startswith(("word/header", "word/footer"))):
                continue
            ra, rb = etree.fromstring(zt.read(n)), etree.fromstring(zo.read(n))
            pa = [_c14n(e) for e in ra.iter(W + "drawing", W + "pict")]
            pb = [_c14n(e) for e in rb.iter(W + "drawing", W + "pict")]
            # a removed watermark is the only permitted difference (header parts)
            if n.startswith("word/header"):
                pa = [x for x in pa if WATERMARK_MARK.encode() not in x]
            if pa != pb:
                problems.append(f"{n}: image/drawing placement elements differ")
    return problems
