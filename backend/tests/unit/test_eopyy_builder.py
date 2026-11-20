import zipfile
from pathlib import Path

import pytest
from docx import Document
from docx.oxml.ns import qn

from fixtures import eopyy_builder as fx


@pytest.fixture()
def built(tmp_path):
    return fx.build_eopyy_docx(tmp_path / "fixture.docx")


def test_opens_with_python_docx_and_has_one_table_per_block(built):
    doc = Document(str(built.path))
    assert len(doc.tables) == len(built.blocks) == 3
    for block in built.blocks:
        table = doc.tables[built.layout.block_tables[block.code]]
        assert table.rows[0].cells[0].paragraphs[0].runs[0].text == block.label


def test_dynamic_fields_are_where_the_layout_says(built):
    paras = Document(str(built.path)).paragraphs
    lay = built.layout
    assert paras[lay.ada_paragraph].text == fx.ADA_LABEL + fx.ADA_VALUE
    intro = paras[lay.intro_paragraph]
    assert [r.text for r in intro.runs][1] == fx.PERIOD_PLACEHOLDER
    assert fx.PATIENT_PLACEHOLDER in intro.runs[0].text
    assert paras[lay.issue_date_paragraph].text == fx.ISSUE_DATE_PLACEHOLDER
    assert paras[lay.representative_paragraph].text == fx.REPRESENTATIVE_NAME


def test_session_date_cell_is_a_literal_tab_in_a_single_text_node(built):
    doc = Document(str(built.path))
    run = doc.tables[0].rows[0].cells[0].paragraphs[0].runs[-1]
    ts = run._r.findall(qn("w:t"))
    assert len(ts) == 1 and ts[0].text == "\t"  # same shape as the real form's blank date line


def test_home_visit_line_only_where_requested(built):
    doc = Document(str(built.path))
    flags = []
    for table in doc.tables:
        text = "\n".join(p.text for p in table.rows[0].cells[0].paragraphs)
        flags.append(fx.HOME_VISIT_LABEL in text)
    assert flags == [False, False, True]
    hv = doc.tables[2].rows[0].cells[0].paragraphs[2]
    assert [r.text for r in hv.runs][2::2] == [fx.UNCHECKED, fx.UNCHECKED]


def test_therapist_details_are_present_and_fake(built):
    doc = Document(str(built.path))
    cell = doc.tables[0].rows[1].cells[1]
    assert [p.runs[1].text for p in cell.paragraphs] == [
        "TEST-101/10",
        "Νομαρχία Δοκιμής",
        "01/01/2010",
    ]


def test_images_are_embedded_and_generated_not_read_from_disk(built):
    assert len(built.images) == 2  # one deduplicated signature + one deduplicated stamp
    with zipfile.ZipFile(built.path) as z:
        for name in built.images:
            assert z.read(f"word/media/{name}").startswith(b"\x89PNG\r\n\x1a\n")
        document_xml = z.read("word/document.xml")
        assert b"<wp:anchor" in document_xml  # floating stamp
        assert b"<wp:inline" in document_xml  # inline signature


def test_watermark_present_by_default_and_optional(tmp_path):
    with_wm = fx.build_eopyy_docx(tmp_path / "a.docx")
    without = fx.build_eopyy_docx(tmp_path / "b.docx", with_watermark=False)
    with zipfile.ZipFile(with_wm.path) as z:
        assert fx.WATERMARK_ID.encode() in z.read("word/header1.xml")
    with zipfile.ZipFile(without.path) as z:
        headers = [n for n in z.namelist() if n.startswith("word/header")]
        assert all(fx.WATERMARK_ID.encode() not in z.read(n) for n in headers)


def test_without_images_has_no_media(tmp_path):
    built = fx.build_eopyy_docx(tmp_path / "x.docx", with_images=False)
    assert built.images == ()


def test_sample_dates_option(tmp_path):
    built = fx.build_eopyy_docx(tmp_path / "x.docx", sample_dates=True)
    run = Document(str(built.path)).tables[0].rows[0].cells[0].paragraphs[0].runs[-1]
    assert run.text == fx.SAMPLE_SESSION_DATES


def test_output_is_byte_for_byte_reproducible(tmp_path):
    a = fx.build_eopyy_docx(tmp_path / "a.docx").path.read_bytes()
    b = fx.build_eopyy_docx(tmp_path / "b.docx").path.read_bytes()
    assert a == b


def test_custom_blocks(tmp_path):
    blocks = (fx.TherapyBlock("psychotherapy", "Ψυχοθεραπεία:", "T9", "TEST-909/10"),)
    built = fx.build_eopyy_docx(tmp_path / "x.docx", blocks)
    assert len(Document(str(built.path)).tables) == 1


def test_builder_is_self_contained():
    """No dependency on the spike, the private template or other ignored files."""
    src = Path(fx.__file__).read_text(encoding="utf-8")
    assert "import spike" not in src and "from spike" not in src
    assert "eopyy-template" not in src
    assert "read_bytes" not in src and ".open(" not in src
