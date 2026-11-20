"""Resolve a Locator to runs, and read / write the text of a run span.

Rules (P-29):
  * every run in a span holds exactly one ``w:t`` (plus optional run properties);
    tabs, breaks, drawings or fields inside a mapped run are refused
  * a span of several runs must share identical formatting, ignoring only Word's
    revision-save ids (``w:rsid*``); the new text goes into the first run and the
    others are emptied — runs are never added or removed, so the XML structure stays
    identical and verification can prove it
"""

from __future__ import annotations

from dataclasses import dataclass

from docx.document import Document as DocxDocument
from docx.oxml.ns import qn
from docx.text.run import Run
from lxml import etree

from .errors import TemplateMismatch
from .field_map import Locator

XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


@dataclass(frozen=True)
class Span:
    part: str  # package part name, e.g. "word/document.xml", "word/header1.xml"
    runs: tuple[Run, ...]
    where: str  # human-readable location


def _story(doc: DocxDocument, loc: Locator):
    if loc.part == "body":
        return doc, "word/document.xml"
    try:
        section = doc.sections[loc.section]
    except IndexError as e:
        raise TemplateMismatch(f"{loc.describe()}: the document has no such section") from e
    if loc.part == "header":
        story = {
            "default": section.header,
            "first": section.first_page_header,
            "even": section.even_page_header,
        }[loc.kind]
    else:
        story = {
            "default": section.footer,
            "first": section.first_page_footer,
            "even": section.even_page_footer,
        }[loc.kind]
    if story.is_linked_to_previous:
        raise TemplateMismatch(f"{loc.describe()}: this {loc.part} is not defined in the template")
    return story, str(story.part.partname).lstrip("/")


def resolve(doc: DocxDocument, loc: Locator) -> Span:
    story, part = _story(doc, loc)
    try:
        if loc.table is not None:
            cell = story.tables[loc.table].rows[loc.row].cells[loc.cell]
            paragraph = cell.paragraphs[loc.paragraph]
        else:
            paragraph = story.paragraphs[loc.paragraph]
        runs = paragraph.runs
        start, end = loc.runs
        if end >= len(runs):
            raise IndexError
        span = tuple(runs[start : end + 1])
    except IndexError as e:
        raise TemplateMismatch(f"{loc.describe()}: location does not exist in the template") from e
    for run in span:
        _single_text_node(run, loc)
    if len(span) > 1:
        _uniform_formatting(span, loc)
    return Span(part=part, runs=span, where=loc.describe())


def _single_text_node(run: Run, loc: Locator) -> None:
    texts = run._r.findall(qn("w:t"))
    others = [c for c in run._r if c.tag not in (qn("w:rPr"), qn("w:t"))]
    if len(texts) != 1 or others:
        kinds = sorted({etree.QName(c).localname for c in others})
        raise TemplateMismatch(
            f"{loc.describe()}: a mapped run must hold exactly one text node "
            f"(found {len(texts)} text nodes{', and ' + ', '.join(kinds) if kinds else ''})"
        )


def _formatting_key(run: Run) -> bytes:
    rpr = run._r.find(qn("w:rPr"))
    if rpr is None:
        return b""
    rpr = etree.fromstring(etree.tostring(rpr))
    for el in rpr.iter():
        for attr in [a for a in el.attrib if etree.QName(a).localname.startswith("rsid")]:
            del el.attrib[attr]
    return etree.tostring(rpr, method="c14n")


def _uniform_formatting(span: tuple[Run, ...], loc: Locator) -> None:
    keys = {_formatting_key(r) for r in span}
    if len(keys) != 1:
        raise TemplateMismatch(
            f"{loc.describe()}: the runs of this field have different formatting; "
            "map a span whose runs look the same, or fix the template"
        )


def read(span: Span) -> str:
    return "".join(r._r.find(qn("w:t")).text or "" for r in span.runs)


def write(span: Span, text: str) -> list[tuple[str, str]]:
    """Put ``text`` into the first run, empty the rest. Returns the (old, new) text-node
    edits for verification. Never uses python-docx's ``run.text`` setter, which would
    rebuild the run's children and reinterpret tabs."""
    edits = []
    for i, run in enumerate(span.runs):
        node = run._r.find(qn("w:t"))
        old = node.text or ""
        new = text if i == 0 else ""
        node.text = new
        node.set(XML_SPACE, "preserve")
        edits.append((old, new))
    return edits
