"""Synthetic EOPYY-style attestation DOCX, built entirely from code.

Purpose: a stand-in for the private center templates, so the document engine
(milestone M2) can be tested on a fresh clone with no private files.

Everything here is invented: names, license numbers, ΑΔΑ, the "signature" and
"stamp" images (tiny generated PNGs). Nothing is read from disk except the
Python packages. The structure deliberately mirrors the real form family:

  * header with a VML watermark ("ΔΕΙΓΜΑ – ΟΧΙ ΓΙΑ ΚΑΤΑΘΕΣΗ")
  * ΑΔΑ line, title, and an intro sentence whose runs hold the patient text and
    the underlined period
  * one bordered table per therapy: label + session-date run (a literal tab on a
    dotted-leader tab stop, as in the real form), a caption, optional home-visit
    line with two ☐ boxes, a therapist-details cell (inline signature image and a
    floating stamp image) and three fixed therapist-detail lines
  * closing sentence, representative lines, issue date and signer name

Usage:
    built = build_eopyy_docx(tmp_path / "fixture.docx")
    built.layout.block_tables["speech"]   # index into Document.tables
"""

from __future__ import annotations

import io
import math
import struct
import zipfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT, WD_TAB_LEADER
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls, qn
from docx.shared import Mm, Pt, Twips

FONT = "Microsoft Sans Serif"
UNCHECKED = "☐"

# ---- fake content: every value below is invented --------------------------------
ADA_LABEL = "ΑΔΑ: "
ADA_VALUE = "ΤΕΣΤ-0000"
TITLE = "Έντυπο Βεβαίωσης Θεραπειών Ειδικής Αγωγής για τον Ε.Ο.Π.Υ.Υ."
INTRO_PREFIX = "Βεβαιώνεται ότι ο/η "
PATIENT_PLACEHOLDER = "ΠΛΑΣΤΟ ΟΝΟΜΑ του ΠΛΑΣΤΟ με ΑΜΚΑ 00000000000"
INTRO_MIDDLE = " παρακολούθησε τις κάτωθι θεραπείες για το χρονικό διάστημα από "
PERIOD_PLACEHOLDER = "1/01/2000 έως 31/01/2000"
SAMPLE_SESSION_DATES = "3/1/00, 5/1/00, 7/1/00"
CLOSING_SENTENCE = (
    "Οι ως άνω συνεδρίες πραγματοποιήθηκαν και βεβαιώνονται από τον/την εκτελούντα/ούσα "
    "τις θεραπείες."
)
REPRESENTATIVE_LINES = (
    "Ο/Η Νόμιμος/η Εκπρόσωπος του Κέντρου",
    "ή Ο/Η Θεραπευτής/τρια",
    "Ημερομηνία / Σφραγίδα / Υπογραφή",
)
ISSUE_DATE_PLACEHOLDER = "1/02/2000"
REPRESENTATIVE_NAME = "Δοκιμαστική Εκπρόσωπος"
WATERMARK_ID = "PowerPlusWaterMarkObject"
WATERMARK_TEXT = "ΔΕΙΓΜΑ – ΟΧΙ ΓΙΑ ΚΑΤΑΘΕΣΗ"
DATES_CAPTION = "(Ημερομηνίες θεραπειών)"
HOME_VISIT_LABEL = "Κατ’ οίκον θεραπείες:"
DETAIL_LABELS = (
    "Αρ. άδειας άσκησης επαγγέλματος: ",
    "Αρμόδια εκδούσα αρχή: ",
    "Ημερομηνία: ",
)


@dataclass(frozen=True)
class TherapyBlock:
    code: str
    label: str  # heading as printed in the form
    therapist: str  # fake therapist tag, e.g. "T1"
    license_no: str
    authority: str = "Νομαρχία Δοκιμής"
    license_date: str = "01/01/2010"
    has_home_visit: bool = False

    @property
    def full_name(self) -> str:  # printed only with print_names=True
        return f"Θεραπευτής {self.therapist}"


DEFAULT_BLOCKS: tuple[TherapyBlock, ...] = (
    TherapyBlock("speech", "Λογοθεραπεία:", "T1", "TEST-101/10"),
    TherapyBlock("occupational", "Εργοθεραπεία:", "T2", "TEST-102/10"),
    TherapyBlock("physiotherapy", "Φυσικοθεραπεία:", "T3", "TEST-103/10", has_home_visit=True),
)

# all five sections of the real form, each with its own (fake) therapist
FIVE_SECTIONS: tuple[TherapyBlock, ...] = (
    TherapyBlock("speech", "Λογοθεραπεία:", "T1", "TEST-101/10"),
    TherapyBlock("occupational", "Εργοθεραπεία:", "T2", "TEST-102/10"),
    TherapyBlock("psychotherapy", "Ψυχοθεραπεία:", "T3", "TEST-103/10"),
    TherapyBlock("special_education", "Ειδική διαπαιδαγώγηση:", "T4", "TEST-104/10"),
    TherapyBlock("physiotherapy", "Φυσικοθεραπεία:", "T5", "TEST-105/10", has_home_visit=True),
)
NAME_LABEL = "Ονοματεπώνυμο: "


@dataclass
class Layout:
    """Where things are, so tests need not rediscover them."""

    ada_paragraph: int = -1  # body paragraph, or -1 when the ΑΔΑ line is in the header
    ada_header_paragraph: int = -1  # header paragraph when ada_in_header=True
    intro_paragraph: int = -1
    # run index ranges (inclusive) inside the intro paragraph
    patient_runs: tuple[int, int] = (0, 0)  # run(s) holding prefix + patient + middle text
    period_runs: tuple[int, int] = (1, 1)
    issue_date_paragraph: int = -1
    representative_paragraph: int = -1  # body paragraph, or -1 when it is in the footer
    representative_footer_paragraph: int = -1
    block_tables: dict[str, int] = field(default_factory=dict)  # code -> Document.tables index


@dataclass
class BuiltFixture:
    path: Path
    blocks: tuple[TherapyBlock, ...]
    layout: Layout
    images: tuple[str, ...]  # media file names embedded in the package
    names_printed: bool = False  # a 4th detail line with the therapist's name


# ---- tiny stdlib PNG generator (no Pillow) --------------------------------------
def _png(width: int, height: int, pixel: Callable[[int, int], tuple[int, int, int, int]]) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type: none
        for x in range(width):
            raw += bytes(pixel(x, y))
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def stamp_png(crossed: bool = False) -> bytes:
    """A transparent ring: stands in for a stamp. ``crossed`` adds a large X so the
    graphic can never be taken for a real stamp (TEST ONLY templates)."""
    size, c = 120, 59.5

    def px(x: int, y: int) -> tuple[int, int, int, int]:
        r = math.hypot(x - c, y - c)
        if crossed and r <= 56 and (abs(x - y) <= 3 or abs(x + y - size) <= 3):
            return (150, 20, 20, 255)
        return (150, 20, 20, 220) if 46 <= r <= 56 or 30 <= r <= 33 else (0, 0, 0, 0)

    return _png(size, size, px)


def signature_png() -> bytes:
    """A transparent squiggle: stands in for a signature."""
    w, h = 160, 50

    def px(x: int, y: int) -> tuple[int, int, int, int]:
        yc = h / 2 + 14 * math.sin(x / 9.0) * math.cos(x / 31.0)
        return (10, 30, 120, 255) if abs(y - yc) < 1.8 else (0, 0, 0, 0)

    return _png(w, h, px)


# ---- low-level helpers ----------------------------------------------------------
def _style_run(run, *, bold=False, underline=False, size=10.0) -> None:
    run.bold = bold or None
    run.underline = underline or None
    run.font.size = Pt(size)
    run.font.name = FONT
    rfonts = run._r.get_or_add_rPr().find(qn("w:rFonts"))
    rfonts.set(qn("w:eastAsia"), FONT)
    rfonts.set(qn("w:cs"), FONT)


def _add_run(paragraph, text: str, **style):
    """One run holding exactly one w:t with the text verbatim (tabs stay literal characters)."""
    run = paragraph.add_run()
    run._r.add_t(text)
    _style_run(run, **style)
    return run


def _floating_picture_xml(rid: str, name: str, cx: int, cy: int, x: int, y: int, pid: int) -> str:
    return f"""
<w:drawing {nsdecls("w", "wp", "a", "pic", "r")}>
 <wp:anchor distT="0" distB="0" distL="0" distR="0" simplePos="0" relativeHeight="251659264"
            behindDoc="0" locked="0" layoutInCell="1" allowOverlap="1">
  <wp:simplePos x="0" y="0"/>
  <wp:positionH relativeFrom="column"><wp:posOffset>{x}</wp:posOffset></wp:positionH>
  <wp:positionV relativeFrom="paragraph"><wp:posOffset>{y}</wp:posOffset></wp:positionV>
  <wp:extent cx="{cx}" cy="{cy}"/>
  <wp:effectExtent l="0" t="0" r="0" b="0"/>
  <wp:wrapNone/>
  <wp:docPr id="{pid}" name="{name}"/>
  <wp:cNvGraphicFramePr/>
  <a:graphic>
   <a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">
    <pic:pic>
     <pic:nvPicPr><pic:cNvPr id="{pid}" name="{name}"/><pic:cNvPicPr/></pic:nvPicPr>
     <pic:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>
     <pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>
      <a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>
    </pic:pic>
   </a:graphicData>
  </a:graphic>
 </wp:anchor>
</w:drawing>"""


def _add_watermark(doc) -> None:
    ns = (
        'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office"'
    )
    xml = f"""
<w:r {ns}><w:rPr><w:noProof/></w:rPr><w:pict>
 <v:shapetype id="_x0000_t136" coordsize="21600,21600" o:spt="136" adj="10800"
              path="m@7,l@8,m@5,21600l@6,21600e"><v:path textpathok="t" o:connecttype="custom"/>
              <v:textpath on="t" fitshape="t"/></v:shapetype>
 <v:shape id="{WATERMARK_ID}" o:spid="_x0000_s2049" type="#_x0000_t136"
          style="position:absolute;margin-left:0;margin-top:0;width:520pt;height:120pt;rotation:315;z-index:-251657216;mso-position-horizontal:center;mso-position-horizontal-relative:margin;mso-position-vertical:center;mso-position-vertical-relative:margin"
          o:allowincell="f" fillcolor="#c00000" stroked="f"><v:fill opacity=".3"/>
  <v:textpath style="font-family:&quot;Arial&quot;;font-size:1pt;font-weight:bold" string="{WATERMARK_TEXT}"/>
 </v:shape></w:pict></w:r>"""
    header = doc.sections[0].header
    header.is_linked_to_previous = False
    header.paragraphs[0]._p.append(parse_xml(xml))


# ---- builder --------------------------------------------------------------------
def _add_block(
    doc,
    block: TherapyBlock,
    pid: int,
    *,
    with_images: bool,
    sample_dates: bool,
    print_names: bool = False,
    crossed_stamps: bool = False,
) -> None:
    table = doc.add_table(rows=2, cols=2)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    for cell, width in zip(table.rows[1].cells, (Mm(56.5), Mm(113.5)), strict=True):
        cell.width = width

    # row 0: heading + session dates (spans both columns)
    top = table.rows[0].cells[0].merge(table.rows[0].cells[1])
    p = top.paragraphs[0]
    p.paragraph_format.tab_stops.add_tab_stop(Mm(166), WD_TAB_ALIGNMENT.RIGHT, WD_TAB_LEADER.DOTS)
    _add_run(p, block.label, bold=True, size=9)
    _add_run(p, " ", size=9)
    _add_run(p, SAMPLE_SESSION_DATES if sample_dates else "\t", size=9)
    cap = top.add_paragraph()
    cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _add_run(cap, DATES_CAPTION, size=7.5)
    if block.has_home_visit:
        hv = top.add_paragraph()
        _add_run(hv, HOME_VISIT_LABEL, size=9)
        _add_run(hv, "     ΝΑΙ  ", size=9)
        _add_run(hv, UNCHECKED, size=11)
        _add_run(hv, "       ΟΧΙ  ", size=9)
        _add_run(hv, UNCHECKED, size=11)

    # row 1, left: therapist block with signature + stamp images
    left = table.rows[1].cells[0]
    p = left.paragraphs[0]
    _add_run(p, "Στοιχεία θεραπευτή", underline=True, size=9)
    _add_run(left.add_paragraph(), "Σφραγίδα / Υπογραφή", size=9)
    if with_images:
        sig_par = left.add_paragraph()
        sig_par.add_run().add_picture(io.BytesIO(signature_png()), width=Mm(28))
        rid, _ = doc.part.get_or_add_image(io.BytesIO(stamp_png(crossed_stamps)))
        sig_par.add_run()._r.append(
            parse_xml(
                _floating_picture_xml(
                    rid, f"Stamp {pid}", int(Mm(20)), int(Mm(20)), int(Mm(22)), int(Mm(-7)), pid
                )
            )
        )

    # row 1, right: fixed (fake) therapist details, label run + value run each
    right = table.rows[1].cells[1]
    values = (block.license_no, block.authority, block.license_date)
    for i, (label, value) in enumerate(zip(DETAIL_LABELS, values, strict=True)):
        par = right.paragraphs[0] if i == 0 else right.add_paragraph()
        _add_run(par, label, size=9)
        _add_run(par, value, size=9)
    if print_names:
        par = right.add_paragraph()
        _add_run(par, NAME_LABEL, size=9)
        _add_run(par, block.full_name, size=9)

    doc.add_paragraph()  # spacer, as in the real form


def build_eopyy_docx(
    path: Path,
    blocks: tuple[TherapyBlock, ...] = DEFAULT_BLOCKS,
    *,
    with_images: bool = True,
    with_watermark: bool = True,
    sample_dates: bool = False,
    split_runs: bool = False,
    ada_in_header: bool = False,
    representative_in_footer: bool = False,
    header_footer_images: bool = False,
    print_names: bool = False,
    test_marking: str | None = None,
) -> BuiltFixture:
    """Write a synthetic attestation to ``path`` and describe it.

    Options exercising the document engine beyond the real form's shape:
      split_runs                the patient text and the period are each split over two
                                runs with identical formatting (as Word often does)
      ada_in_header             the ΑΔΑ line lives in the page header
      representative_in_footer  the representative's name lives in the page footer
      header_footer_images      a small image in the header and in the footer

    The output is deterministic: building twice with the same arguments yields
    byte-identical files (fixed timestamps), so hashes can be asserted in tests.
    """
    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Mm(210), Mm(297)
    sec.left_margin = sec.right_margin = Twips(1134)
    sec.top_margin = sec.bottom_margin = Twips(567)
    if with_watermark:
        _add_watermark(doc)
    header, footer = sec.header, sec.footer
    header.is_linked_to_previous = False
    footer.is_linked_to_previous = False

    layout = Layout()
    if ada_in_header:
        p = header.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        _add_run(p, ADA_LABEL + ADA_VALUE, size=10)
        layout.ada_header_paragraph = len(header.paragraphs) - 1
    if header_footer_images:
        header.add_paragraph().add_run().add_picture(io.BytesIO(stamp_png()), width=Mm(12))
        footer.paragraphs[0].add_run().add_picture(io.BytesIO(signature_png()), width=Mm(20))

    # python-docx's default template starts with one empty paragraph; reuse it
    p = doc.paragraphs[0] if doc.paragraphs else doc.add_paragraph()
    if test_marking:
        # static body text (not a removable watermark): it stays in every generated document
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _add_run(p, test_marking, bold=True, size=12)
        footer.add_paragraph().add_run(test_marking).bold = True
        p = doc.add_paragraph()
    if not ada_in_header:
        p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        _add_run(p, ADA_LABEL + ADA_VALUE, size=10)
        layout.ada_paragraph = len(doc.paragraphs) - 1
        p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _add_run(p, TITLE, bold=True, underline=True, size=11.5)

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    if split_runs:
        cut = INTRO_PREFIX + PATIENT_PLACEHOLDER
        first, second = cut[: len(INTRO_PREFIX) + 8], cut[len(INTRO_PREFIX) + 8 :] + INTRO_MIDDLE
        _add_run(p, first, size=10)
        _add_run(p, second, size=10)
        half = PERIOD_PLACEHOLDER.index(" έως ") + len(" έως ")
        _add_run(p, PERIOD_PLACEHOLDER[:half], underline=True, size=10)
        _add_run(p, PERIOD_PLACEHOLDER[half:], underline=True, size=10)
        layout.patient_runs, layout.period_runs = (0, 1), (2, 3)
    else:
        _add_run(p, INTRO_PREFIX + PATIENT_PLACEHOLDER + INTRO_MIDDLE, size=10)
        _add_run(p, PERIOD_PLACEHOLDER, underline=True, size=10)
    _add_run(p, ".", size=10)
    layout.intro_paragraph = len(doc.paragraphs) - 1

    for i, block in enumerate(blocks):
        _add_block(
            doc,
            block,
            100 + i,
            with_images=with_images,
            sample_dates=sample_dates,
            print_names=print_names,
            crossed_stamps=bool(test_marking),
        )
        layout.block_tables[block.code] = i

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    _add_run(p, CLOSING_SENTENCE, size=10)
    for line in REPRESENTATIVE_LINES:
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.left_indent = Twips(4800)
        _add_run(p, line, size=10)
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.left_indent = Twips(4800)
    _add_run(p, ISSUE_DATE_PLACEHOLDER, underline=True, size=10)
    layout.issue_date_paragraph = len(doc.paragraphs) - 1
    if representative_in_footer:
        p = footer.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _add_run(p, REPRESENTATIVE_NAME, size=10)
        layout.representative_footer_paragraph = len(footer.paragraphs) - 1
    else:
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.left_indent = Twips(4800)
        _add_run(p, REPRESENTATIVE_NAME, size=10)
        layout.representative_paragraph = len(doc.paragraphs) - 1

    props = doc.core_properties
    fixed = datetime(2000, 1, 1)
    props.author = props.last_modified_by = "Synthetic fixture"
    props.title = "Synthetic EOPYY-style attestation (test fixture)"
    props.created = props.modified = fixed
    props.revision = 1

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()
    doc.save(buffer)
    media = _write_deterministic(buffer.getvalue(), path)
    return BuiltFixture(
        path=path, blocks=tuple(blocks), layout=layout, images=media, names_printed=print_names
    )


def _write_deterministic(blob: bytes, path: Path) -> tuple[str, ...]:
    """Re-pack the package with fixed entry timestamps so output bytes are reproducible."""
    media = []
    with (
        zipfile.ZipFile(io.BytesIO(blob)) as src,
        zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as dst,
    ):
        for item in src.infolist():
            info = zipfile.ZipInfo(item.filename, date_time=(2000, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = item.external_attr
            dst.writestr(info, src.read(item.filename))
            if item.filename.startswith("word/media/"):
                media.append(item.filename.rsplit("/", 1)[-1])
    return tuple(sorted(media))
