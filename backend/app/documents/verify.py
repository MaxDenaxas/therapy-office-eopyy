"""Verification of a generated DOCX against its template (nothing is trusted unchecked).

verify_preserved         every package part is identical to the template, except the
                         mapped text nodes (exactly the recorded edits) and the removed
                         watermark; images and layout are covered by this check
verify_images_untouched  media bytes and every drawing's size/position/crop/rotation
verify_values            re-reads each mapped field from the output (correct data)
find_leftovers           unresolved placeholders: template sample values or marker
                         text still present, or a watermark left in the output
verify_document          all of the above plus template integrity; returns problems
"""

from __future__ import annotations

import zipfile
from collections import Counter
from pathlib import Path

from docx import Document
from lxml import etree

from . import locate
from .errors import DocumentError, TemplateFileError
from .field_map import FieldMap
from .package import inspect_docx, parse_xml, sha256_file
from .render import CHECKED, UNCHECKED, WATERMARK_MARK, RenderResult, watermark_runs

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
STORY_PREFIXES = ("word/document.xml", "word/header", "word/footer")
# Word merge-field / template-placeholder markers that must never reach a generated document
GENERIC_MARKERS = ("{{", "}}", "«", "»", "MERGEFIELD")


def _c14n(el) -> bytes:
    return etree.tostring(el, method="c14n")


def _unordered(root) -> list[bytes]:
    return sorted(_c14n(c) for c in root)


def _is_story(name: str) -> bool:
    return name.endswith(".xml") and name.startswith(STORY_PREFIXES)


def _texts(root) -> list[str]:
    return [t.text or "" for t in root.iter(_W + "t")]


def _normalise_structure(root) -> None:
    """Blank text nodes and the attributes the engine may legitimately touch, so what
    remains is the document's structure and formatting."""
    for r in root.iter(_W + "r"):  # pinned glyph font on ☐/☒ runs (P-13, opt-in)
        if "".join(_texts(r)) in (UNCHECKED, CHECKED):
            rpr = r.find(_W + "rPr")
            if rpr is not None and rpr.find(_W + "rFonts") is not None:
                rpr.remove(rpr.find(_W + "rFonts"))
    for t in root.iter(_W + "t"):
        t.text = ""
        t.attrib.pop(_XML_SPACE, None)


def verify_preserved(template: Path, output: Path, result: RenderResult) -> list[str]:
    problems: list[str] = []
    expected_by_part: dict[str, Counter] = {}
    for e in result.edits:
        if e.old != e.new:
            expected_by_part.setdefault(e.part, Counter())[(e.old, e.new)] += 1
    with zipfile.ZipFile(template) as zt, zipfile.ZipFile(output) as zo:
        names_t, names_o = set(zt.namelist()), set(zo.namelist())
        if names_t != names_o:
            problems.append(
                f"package parts differ (missing {sorted(names_t - names_o)}, "
                f"added {sorted(names_o - names_t)})"
            )
        for name in sorted(names_t & names_o):
            a, b = zt.read(name), zo.read(name)
            if _is_story(name):
                problems += _verify_story(
                    name,
                    a,
                    b,
                    expected_by_part.pop(name, Counter()),
                    watermark_removed=result.watermark_runs_removed > 0,
                )
            elif name.endswith((".rels", "[Content_Types].xml")):
                if _unordered(parse_xml(a, name)) != _unordered(parse_xml(b, name)):
                    problems.append(f"{name}: relationships or content types differ")
            elif name.endswith(".xml"):
                if _c14n(parse_xml(a, name)) != _c14n(parse_xml(b, name)):
                    problems.append(f"{name}: XML differs from the template")
            elif a != b:
                problems.append(f"{name}: binary part differs from the template")
    for part, missing in expected_by_part.items():
        problems.append(f"{part}: {sum(missing.values())} recorded edit(s) not found in the output")
    return problems


def _verify_story(
    name: str, a: bytes, b: bytes, expected: Counter, *, watermark_removed: bool
) -> list[str]:
    problems = []
    ra, rb = parse_xml(a, name), parse_xml(b, name)
    if name.startswith("word/header"):
        if watermark_removed:
            for run in watermark_runs(ra):
                run.getparent().remove(run)
        if watermark_runs(rb):
            problems.append(f"{name}: the sample watermark is still present")
    ta, tb = _texts(ra), _texts(rb)
    if len(ta) != len(tb):
        return [*problems, f"{name}: number of text nodes changed ({len(ta)} -> {len(tb)})"]
    actual = Counter((x, y) for x, y in zip(ta, tb, strict=True) if x != y)
    if actual != expected:
        unexpected = sum((actual - expected).values())
        missing = sum((expected - actual).values())
        problems.append(
            f"{name}: text changed outside the mapped fields "
            f"({unexpected} unexpected change(s), {missing} expected change(s) missing)"
        )
    _normalise_structure(ra)
    _normalise_structure(rb)
    if _c14n(ra) != _c14n(rb):
        problems.append(f"{name}: structure or formatting differs from the template")
    return problems


def verify_images_untouched(template: Path, output: Path) -> list[str]:
    """Images must not be replaced, moved, resized, cropped or rotated."""
    problems: list[str] = []
    with zipfile.ZipFile(template) as zt, zipfile.ZipFile(output) as zo:
        out_names = set(zo.namelist())
        for name in zt.namelist():
            if name.startswith("word/media/"):
                if name not in out_names or zt.read(name) != zo.read(name):
                    problems.append(f"image {name} was changed or removed")
            elif _is_story(name) and name in out_names:
                ra, rb = parse_xml(zt.read(name), name), parse_xml(zo.read(name), name)
                pa = [_c14n(e) for e in ra.iter(_W + "drawing", _W + "pict")]
                pb = [_c14n(e) for e in rb.iter(_W + "drawing", _W + "pict")]
                if name.startswith("word/header"):  # the removed watermark is the only exception
                    pa = [x for x in pa if WATERMARK_MARK.encode() not in x]
                if pa != pb:
                    problems.append(
                        f"{name}: image placement (size/position/crop/rotation) differs"
                    )
    return problems


def verify_values(output: Path, result: RenderResult, field_map: FieldMap) -> list[str]:
    """Re-open the output and read every mapped field back."""
    problems: list[str] = []
    doc = Document(str(output))
    locations = {**field_map.text_fields()}
    for block in field_map.blocks:
        locations[f"sessions:{block.therapy}"] = block.dates_loc
        if block.home_visit is not None:
            locations[f"home_visit:{block.therapy}:yes"] = block.home_visit.yes_box_loc
            locations[f"home_visit:{block.therapy}:no"] = block.home_visit.no_box_loc
    written = {e.field: e for e in result.expected}
    for name, spec in locations.items():
        if name in result.kept:
            continue
        if name not in written:
            problems.append(f"field {name!r} was not written")
            continue
        loc = spec.loc if hasattr(spec, "loc") else spec
        try:
            actual = locate.read(locate.resolve(doc, loc))
        except DocumentError as e:
            problems.append(f"field {name!r} cannot be read back: {e}")
            continue
        if actual != written[name].text:
            problems.append(f"field {name!r} does not hold the intended value")
    for block in field_map.blocks:
        if block.home_visit is None:
            continue
        ticks = [written.get(f"home_visit:{block.therapy}:{s}") for s in ("yes", "no")]
        if [t.text if t else None for t in ticks].count(CHECKED) != 1:
            problems.append(f"home visit of {block.therapy!r}: exactly one box must be ticked")
    return problems


def find_leftovers(output: Path, result: RenderResult, field_map: FieldMap) -> list[str]:
    """Unresolved placeholders: marker text, configured sample strings, or template
    sample values that were replaced in a field but still appear elsewhere."""
    with zipfile.ZipFile(output) as zo:
        text = "\n".join(
            "".join(_texts(parse_xml(zo.read(n), n))) for n in zo.namelist() if _is_story(n)
        )
    problems = []
    for marker in (*GENERIC_MARKERS, *field_map.forbidden_text):
        if marker and marker in text:
            problems.append(f"unresolved placeholder or sample text {marker!r} found")
    for change in result.changes:
        if change.field.startswith("home_visit:"):
            continue
        old_value = change.old.strip()
        new_value = change.new
        meaningful = len(old_value) >= 4 and any(ch.isalnum() for ch in old_value)
        if meaningful and old_value not in new_value and old_value in text:
            problems.append(f"template sample text of field {change.field!r} is still present")
    return problems


def verify_document(
    template: Path, output: Path, result: RenderResult, field_map: FieldMap
) -> list[str]:
    """Everything that must hold before a generated DOCX is accepted."""
    problems: list[str] = []
    if sha256_file(template) != field_map.template_sha256:
        problems.append("template file does not match its field map (SHA-256 differs)")
    if result.template_sha256 != field_map.template_sha256:
        problems.append("document was rendered from a different template version")
    try:
        inspect_docx(output)
        Document(str(output))
    except TemplateFileError as e:
        return [*problems, f"generated file is not a valid DOCX: {e}"]
    except Exception as e:  # noqa: BLE001 - python-docx raises several unrelated types
        return [*problems, f"generated file cannot be opened as a DOCX ({type(e).__name__})"]
    problems += verify_preserved(template, output, result)
    problems += verify_images_untouched(template, output)
    problems += verify_values(output, result, field_map)
    problems += find_leftovers(output, result, field_map)
    return problems
