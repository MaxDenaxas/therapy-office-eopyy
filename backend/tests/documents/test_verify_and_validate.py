"""Generated-document verification (tamper detection) and the template activation gate."""

from __future__ import annotations

import zipfile

import pytest
from docx import Document
from docx.oxml.ns import qn

from app.documents import render, validate_template, verify_document
from app.documents.field_map import Locator, TextField
from fixtures import eopyy_builder as fx
from fixtures.eopyy_maps import production_field_map
from tests.documents.conftest import make_data


@pytest.fixture()
def generated(tmp_path, built, fmap, data):
    out = tmp_path / "out.docx"
    result = render(built.path, fmap, data, out)
    return out, result


def rewrite(src, dst, part, fn):
    with zipfile.ZipFile(src) as zi, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zo:
        for item in zi.infolist():
            blob = zi.read(item.filename)
            zo.writestr(item, fn(blob) if item.filename == part else blob)
    return dst


# ---------------------------------------------------------------- tamper detection
@pytest.mark.parametrize(
    "part, fn, match",
    [
        (
            "word/document.xml",
            lambda b: b.replace("Στοιχεία θεραπευτή".encode(), "Άλλο κείμενο".encode(), 1),
            "text changed outside the mapped fields",
        ),
        (
            "word/document.xml",
            lambda b: b.replace(b"<w:b/>", b"", 1),
            "structure or formatting differs",
        ),
        (
            "word/document.xml",
            lambda b: b.replace(b'<wp:extent cx="', b'<wp:extent cx="1', 1),
            "image placement",
        ),
        (
            "word/document.xml",
            lambda b: b.replace(b"<wp:posOffset>", b"<wp:posOffset>1", 1),
            "image placement",
        ),
        ("word/media/image1.png", lambda b: b + b"\x00", "image word/media/image1.png was changed"),
        ("word/styles.xml", lambda b: b.replace(b"<w:sz ", b"<w:i/><w:sz ", 1), "XML differs"),
        (
            "word/header1.xml",
            lambda b: b.replace(b"<w:p>", b"<w:p><w:r><w:t>X</w:t></w:r>", 1),
            "header1.xml",
        ),
    ],
)
def test_tampering_is_detected(tmp_path, built, fmap, generated, part, fn, match):
    out, result = generated
    tampered = rewrite(out, tmp_path / "tampered.docx", part, fn)
    problems = verify_document(built.path, tampered, result, fmap)
    assert any(match in p for p in problems), problems


def test_wrong_value_is_detected_by_read_back(tmp_path, built, fmap, generated):
    out, result = generated
    tampered = rewrite(
        out,
        tmp_path / "t.docx",
        "word/document.xml",
        lambda b: b.replace(b"20/11/2026", b"21/11/2026", 1),
    )
    problems = verify_document(built.path, tampered, result, fmap)
    assert any("'issue_date' does not hold the intended value" in p for p in problems)


def test_leftover_sample_text_is_detected(tmp_path, built, fmap, generated):
    out, result = generated
    tampered = rewrite(
        out,
        tmp_path / "t.docx",
        "word/document.xml",
        lambda b: b.replace(
            fx.CLOSING_SENTENCE.encode(), (fx.CLOSING_SENTENCE + " ΠΛΑΣΤΟ").encode(), 1
        ),
    )
    problems = verify_document(built.path, tampered, result, fmap)
    assert any("unresolved placeholder or sample text 'ΠΛΑΣΤΟ'" in p for p in problems)


def test_unmapped_stale_sample_value_is_detected(tmp_path):
    """A template sample value replaced in its field must not survive anywhere else."""
    built = fx.build_eopyy_docx(tmp_path / "t.docx")
    doc = Document(str(built.path))
    doc.paragraphs[built.layout.intro_paragraph - 1].runs[0]._r.find(qn("w:t")).text = (
        fx.TITLE + " " + fx.PERIOD_PLACEHOLDER
    )  # sample period also printed in the title
    doc.save(str(tmp_path / "c.docx"))
    built.path.unlink()
    (tmp_path / "c.docx").rename(built.path)
    fmap = production_field_map(built)
    out = tmp_path / "o.docx"
    result = render(built.path, fmap, make_data(built), out)
    problems = verify_document(built.path, out, result, fmap)
    assert any("template sample text of field 'period' is still present" in p for p in problems)


def test_merge_field_markers_are_detected(tmp_path, built, fmap, generated):
    out, result = generated
    tampered = rewrite(
        out,
        tmp_path / "t.docx",
        "word/document.xml",
        lambda b: b.replace(
            fx.CLOSING_SENTENCE.encode(), (fx.CLOSING_SENTENCE + " {{patient}}").encode(), 1
        ),
    )
    problems = verify_document(built.path, tampered, result, fmap)
    assert any("'{{'" in p for p in problems)


def test_corrupted_output_is_detected(tmp_path, built, fmap, generated):
    out, result = generated
    broken = tmp_path / "broken.docx"
    broken.write_bytes(out.read_bytes()[:1000])
    problems = verify_document(built.path, broken, result, fmap)
    assert any("not a valid DOCX" in p for p in problems)


def test_changed_template_is_detected(tmp_path, built, fmap, generated):
    out, result = generated
    built.path.write_bytes(built.path.read_bytes() + b"\x00")  # template altered afterwards
    problems = verify_document(built.path, out, result, fmap)
    assert any("does not match its field map" in p for p in problems)


# ---------------------------------------------------------------- activation gate
def test_good_template_validates(built, fmap):
    report = validate_template(built.path, fmap)
    assert report.ok, report.errors


def test_validation_with_header_footer_and_split_runs(tmp_path):
    built = fx.build_eopyy_docx(
        tmp_path / "t.docx",
        ada_in_header=True,
        representative_in_footer=True,
        split_runs=True,
        header_footer_images=True,
    )
    report = validate_template(built.path, production_field_map(built))
    assert report.ok, report.errors


def test_blank_therapist_details_are_rejected(tmp_path):
    blocks = (
        fx.TherapyBlock("speech", "Λογοθεραπεία:", "T1", "\t", authority="\t", license_date="\t"),
    )
    built = fx.build_eopyy_docx(tmp_path / "t.docx", blocks)
    report = validate_template(built.path, production_field_map(built))
    assert not report.ok
    assert sum("is empty in the template" in e for e in report.errors) == 3


@pytest.mark.parametrize(
    "mutate, match",
    [
        (lambda m: m.model_copy(update={"template_sha256": "0" * 64}), "SHA-256"),
        (lambda m: m.model_copy(update={"blocks": m.blocks[:2]}), "3 tables but 2"),
        (
            lambda m: m.model_copy(
                update={
                    "issue_date": TextField(loc=Locator(paragraph=500, runs=(0, 0)), expect="date")
                }
            ),
            "does not exist",
        ),
        (
            lambda m: m.model_copy(
                update={"patient": m.patient.model_copy(update={"between": ("ΔΕΝ ΥΠΑΡΧΕΙ ", " x")})}
            ),
            "anchor text",
        ),
        (
            lambda m: m.model_copy(
                update={
                    "blocks": (m.blocks[0].model_copy(update={"label": "Άλλο:"}), *m.blocks[1:])
                }
            ),
            "heading is",
        ),
        (
            lambda m: m.model_copy(
                update={
                    "blocks": (*m.blocks[:2], m.blocks[2].model_copy(update={"home_visit": None}))
                }
            ),
            "home-visit (ΝΑΙ/ΟΧΙ) line that is not mapped",
        ),
    ],
)
def test_broken_maps_are_rejected(built, fmap, mutate, match):
    report = validate_template(built.path, mutate(fmap))
    assert not report.ok and any(match in e for e in report.errors), report.errors


def test_static_center_field_must_not_be_blank(tmp_path):
    built = fx.build_eopyy_docx(tmp_path / "t.docx")
    doc = Document(str(built.path))
    doc.paragraphs[built.layout.ada_paragraph].runs[0]._r.find(qn("w:t")).text = "ΑΔΑ: 	"
    doc.save(str(tmp_path / "c.docx"))
    built.path.unlink()
    (tmp_path / "c.docx").rename(built.path)
    static = validate_template(built.path, production_field_map(built, center_mode="static"))
    assert any("ada is mapped as static but is blank" in e for e in static.errors)
    dynamic = validate_template(built.path, production_field_map(built))  # value supplied later
    assert dynamic.ok, dynamic.errors


def test_static_center_fields_validate(built):
    report = validate_template(built.path, production_field_map(built, center_mode="static"))
    assert report.ok, report.errors


def test_corrupted_template_is_rejected(tmp_path, fmap):
    bad = tmp_path / "bad.docx"
    bad.write_bytes(b"PK\x03\x04 broken")
    report = validate_template(bad, fmap)
    assert not report.ok and "template file" in report.errors[0]


def test_unmapped_dates_and_text_boxes_are_warned(tmp_path):
    built = fx.build_eopyy_docx(tmp_path / "t.docx")
    doc = Document(str(built.path))
    doc.sections[0].footer.add_paragraph("Ισχύει από 01/01/2026")
    doc.save(str(tmp_path / "c.docx"))
    built.path.unlink()
    (tmp_path / "c.docx").rename(built.path)
    report = validate_template(built.path, production_field_map(built))
    assert report.ok, report.errors
    assert any("unmapped date-like text" in w and "01/01/2026" in w for w in report.warnings)
