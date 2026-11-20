"""Rendering: fields in paragraphs, tables, headers and footers; split runs; home visit;
watermark; formatting and image preservation; failure cases."""

from __future__ import annotations

import zipfile
from datetime import date

import pytest
from docx import Document
from docx.oxml.ns import qn

from app.documents import (
    InvalidDocumentData,
    MissingValue,
    TemplateIntegrityError,
    TemplateMismatch,
    render,
    sha256_file,
    verify_document,
)
from app.documents.render import CHECKED, UNCHECKED
from fixtures import eopyy_builder as fx
from fixtures.eopyy_maps import production_field_map
from tests.documents.conftest import PERIOD, make_data

PATIENT_TEXT = "ΓΕΩΡΓΙΟΣ ΔΟΚΙΜΑΣΤΙΚΟΣ του ΝΙΚΟΛΑΟΥ με ΑΜΚΑ 12345678901"
DATES_TEXT = "16/10/26, 20/10/26, 23/10/26, 27/10/26, 30/10/26, 3/11/26, 6/11/26, 10/11/26"


def rendered(tmp_path, built, fmap, data, name="out.docx"):
    out = tmp_path / name
    result = render(built.path, fmap, data, out)
    return out, result


def all_text(path) -> str:
    with zipfile.ZipFile(path) as z:
        return "".join(
            z.read(n).decode("utf-8")
            for n in z.namelist()
            if n.startswith("word/") and n.endswith(".xml")
        )


# ---------------------------------------------------------------- happy path
def test_all_fields_are_written_and_verified(tmp_path, built, fmap, data):
    out, result = rendered(tmp_path, built, fmap, data)
    doc = Document(str(out))
    lay = built.layout
    intro = doc.paragraphs[lay.intro_paragraph]
    assert PATIENT_TEXT in intro.text
    assert intro.runs[1].text == "16/10/2026 έως 15/11/2026"
    assert doc.paragraphs[lay.issue_date_paragraph].text == "20/11/2026"
    assert doc.paragraphs[lay.ada_paragraph].text == "ΑΔΑ: ΨΨ00-ΤΕΣΤ"
    assert doc.paragraphs[lay.representative_paragraph].text == "Δοκιμαστική Εκπρόσωπος Κέντρου"
    for table in doc.tables:
        assert table.rows[0].cells[0].paragraphs[0].runs[2].text == DATES_TEXT
    assert verify_document(built.path, out, result, fmap) == []


def test_session_dates_are_sorted_and_formatted(tmp_path, built, fmap):
    data = make_data(
        built, sessions={b.code: [date(2026, 11, 3), date(2026, 10, 16)] for b in built.blocks}
    )
    out, _ = rendered(tmp_path, built, fmap, data)
    assert (
        Document(str(out)).tables[0].rows[0].cells[0].paragraphs[0].runs[2].text
        == "16/10/26, 3/11/26"
    )


def test_custom_date_formats(tmp_path, built, data):
    from app.documents.field_map import DateFormats

    formats = DateFormats(issue_date="dd.MM.yyyy", session_date="dd/MM/yy")
    fmap = production_field_map(built, formats=formats)
    out, _ = rendered(tmp_path, built, fmap, data)
    doc = Document(str(out))
    assert doc.paragraphs[built.layout.issue_date_paragraph].text == "20.11.2026"
    assert (
        doc.tables[0].rows[0].cells[0].paragraphs[0].runs[2].text.startswith("16/10/26, 20/10/26")
    )


def test_template_file_is_never_modified(tmp_path, built, fmap, data):
    before = built.path.read_bytes()
    rendered(tmp_path, built, fmap, data)
    assert built.path.read_bytes() == before


def test_refuses_to_overwrite_anything(tmp_path, built, fmap, data):
    with pytest.raises(ValueError, match="over the template"):
        render(built.path, fmap, data, built.path.with_name("x") / ".." / built.path.name)
    out, _ = rendered(tmp_path, built, fmap, data)
    with pytest.raises(FileExistsError):
        render(built.path, fmap, data, out)


# ---------------------------------------------------------------- formatting / structure
def test_run_formatting_is_untouched(tmp_path, built, fmap, data):
    out, _ = rendered(tmp_path, built, fmap, data)
    before, after = Document(str(built.path)), Document(str(out))
    pairs = [(before.paragraphs[i], after.paragraphs[i]) for i in range(len(before.paragraphs))]
    pairs += [
        (pb, pa)
        for tb, ta in zip(before.tables, after.tables, strict=True)
        for rb, ra in zip(tb.rows, ta.rows, strict=True)
        for cb, ca in zip(rb.cells, ra.cells, strict=True)
        for pb, pa in zip(cb.paragraphs, ca.paragraphs, strict=True)
    ]
    for pb, pa in pairs:
        assert len(pb.runs) == len(pa.runs)
        for rb, ra in zip(pb.runs, pa.runs, strict=True):
            fb, fa = rb._r.find(qn("w:rPr")), ra._r.find(qn("w:rPr"))
            assert (fb.xml if fb is not None else None) == (fa.xml if fa is not None else None)


def test_page_layout_and_tables_are_untouched(tmp_path, built, fmap, data):
    out, _ = rendered(tmp_path, built, fmap, data)
    before, after = Document(str(built.path)), Document(str(out))
    sb, sa = before.sections[0], after.sections[0]
    for attr in (
        "page_width",
        "page_height",
        "left_margin",
        "right_margin",
        "top_margin",
        "bottom_margin",
        "orientation",
    ):
        assert getattr(sb, attr) == getattr(sa, attr)
    assert [t._tbl.xml.count("<w:tc>") for t in before.tables] == [
        t._tbl.xml.count("<w:tc>") for t in after.tables
    ]


def test_images_and_their_placement_are_preserved(tmp_path, data):
    built = fx.build_eopyy_docx(tmp_path / "t.docx", header_footer_images=True)
    fmap = production_field_map(built)
    out, result = rendered(tmp_path, built, fmap, make_data(built))
    with zipfile.ZipFile(built.path) as zt, zipfile.ZipFile(out) as zo:
        media = [n for n in zt.namelist() if n.startswith("word/media/")]
        assert len(media) >= 2
        assert all(zt.read(n) == zo.read(n) for n in media)
        for part in ("word/header1.xml", "word/footer1.xml"):
            assert b"<pic:pic" in zo.read(part)
    assert verify_document(built.path, out, result, fmap) == []


# ---------------------------------------------------------------- headers, footers, split runs
def test_fields_in_header_and_footer(tmp_path):
    built = fx.build_eopyy_docx(
        tmp_path / "t.docx", ada_in_header=True, representative_in_footer=True
    )
    fmap = production_field_map(built)
    out, result = rendered(tmp_path, built, fmap, make_data(built))
    doc = Document(str(out))
    header = doc.sections[0].header.paragraphs[built.layout.ada_header_paragraph]
    footer = doc.sections[0].footer.paragraphs[built.layout.representative_footer_paragraph]
    assert header.text == "ΑΔΑ: ΨΨ00-ΤΕΣΤ"
    assert footer.text == "Δοκιμαστική Εκπρόσωπος Κέντρου"
    assert {e.part for e in result.edits} >= {
        "word/document.xml",
        "word/header1.xml",
        "word/footer1.xml",
    }
    assert verify_document(built.path, out, result, fmap) == []


def test_fields_split_across_runs(tmp_path):
    built = fx.build_eopyy_docx(tmp_path / "t.docx", split_runs=True)
    fmap = production_field_map(built)
    out, result = rendered(tmp_path, built, fmap, make_data(built))
    intro = Document(str(out)).paragraphs[built.layout.intro_paragraph]
    assert PATIENT_TEXT in intro.text
    assert "16/10/2026 έως 15/11/2026" in intro.text
    assert len(intro.runs) == 5  # no run added or removed
    assert intro.runs[1].text == "" and intro.runs[3].text == ""  # value lives in the first run
    assert verify_document(built.path, out, result, fmap) == []


def test_split_runs_with_different_formatting_are_refused(tmp_path):
    built = fx.build_eopyy_docx(tmp_path / "t.docx", split_runs=True)
    doc = Document(str(built.path))
    doc.paragraphs[built.layout.intro_paragraph].runs[3].bold = True
    changed = tmp_path / "changed.docx"
    doc.save(str(changed))
    built.path.unlink()
    changed.rename(built.path)
    fmap = production_field_map(built)
    with pytest.raises(TemplateMismatch, match="different formatting"):
        render(built.path, fmap, make_data(built), tmp_path / "out.docx")


def test_revision_ids_do_not_count_as_formatting(tmp_path):
    built = fx.build_eopyy_docx(tmp_path / "t.docx", split_runs=True)
    doc = Document(str(built.path))
    run = doc.paragraphs[built.layout.intro_paragraph].runs[3]
    run._r.get_or_add_rPr().set(qn("w:rsidRPr"), "00AB12CD")
    changed = tmp_path / "c.docx"
    doc.save(str(changed))
    built.path.unlink()
    changed.rename(built.path)
    fmap = production_field_map(built)
    out, result = rendered(tmp_path, built, fmap, make_data(built))
    assert verify_document(built.path, out, result, fmap) == []


# ---------------------------------------------------------------- home visit
@pytest.mark.parametrize(
    "value, boxes", [(True, (CHECKED, UNCHECKED)), (False, (UNCHECKED, CHECKED))]
)
def test_home_visit_ticks_exactly_one_box(tmp_path, built, fmap, value, boxes):
    data = make_data(built, home_visit={"physiotherapy": value})
    out, result = rendered(tmp_path, built, fmap, data)
    runs = Document(str(out)).tables[2].rows[0].cells[0].paragraphs[2].runs
    assert (runs[2].text, runs[4].text) == boxes
    assert verify_document(built.path, out, result, fmap) == []


def test_home_visit_must_be_chosen(tmp_path, built, fmap):
    with pytest.raises(MissingValue, match="home visit"):
        rendered(tmp_path, built, fmap, make_data(built, home_visit={}))


def test_prechecked_home_visit_box_is_refused(tmp_path):
    built = fx.build_eopyy_docx(tmp_path / "t.docx")
    doc = Document(str(built.path))
    doc.tables[2].rows[0].cells[0].paragraphs[2].runs[2]._r.find(qn("w:t")).text = CHECKED
    doc.save(str(tmp_path / "c.docx"))
    built.path.unlink()
    (tmp_path / "c.docx").rename(built.path)
    with pytest.raises(TemplateMismatch, match="expected an empty"):
        render(built.path, production_field_map(built), make_data(built), tmp_path / "o.docx")


def test_checked_font_option_only_changes_the_ticked_run(tmp_path, built, fmap):
    blocks = list(fmap.blocks)
    hv = blocks[2].home_visit.model_copy(update={"checked_font": "MS Gothic"})
    blocks[2] = blocks[2].model_copy(update={"home_visit": hv})
    fmap = fmap.model_copy(update={"blocks": tuple(blocks)})
    out, result = rendered(tmp_path, built, fmap, make_data(built))
    run = Document(str(out)).tables[2].rows[0].cells[0].paragraphs[2].runs[2]
    assert run._r.find(qn("w:rPr")).find(qn("w:rFonts")).get(qn("w:ascii")) == "MS Gothic"
    assert verify_document(built.path, out, result, fmap) == []


# ---------------------------------------------------------------- watermark
def test_watermark_removed_from_output_only(tmp_path, built, fmap, data):
    out, result = rendered(tmp_path, built, fmap, data)
    assert result.watermark_runs_removed == 1
    with zipfile.ZipFile(built.path) as zt, zipfile.ZipFile(out) as zo:
        assert fx.WATERMARK_ID.encode() in zt.read("word/header1.xml")
        assert fx.WATERMARK_ID.encode() not in zo.read("word/header1.xml")


def test_watermark_kept_when_configured(tmp_path, built, fmap, data):
    out, result = rendered(
        tmp_path, built, fmap.model_copy(update={"remove_watermark": False}), data
    )
    assert result.watermark_runs_removed == 0
    with zipfile.ZipFile(out) as zo:
        assert fx.WATERMARK_ID.encode() in zo.read("word/header1.xml")


# ---------------------------------------------------------------- optional center fields
# ---------------------------------------------------------------- center fields (D-25)
def test_static_center_fields_keep_template_text(tmp_path, built):
    fmap = production_field_map(built, center_mode="static")
    data = make_data(built, ada=None, representative=None)
    out, result = rendered(tmp_path, built, fmap, data)
    doc = Document(str(out))
    assert doc.paragraphs[built.layout.ada_paragraph].text == fx.ADA_LABEL + fx.ADA_VALUE
    assert doc.paragraphs[built.layout.representative_paragraph].text == fx.REPRESENTATIVE_NAME
    assert sorted(result.kept) == ["ada", "representative"]
    assert verify_document(built.path, out, result, fmap) == []


@pytest.mark.parametrize("missing", ["ada", "representative"])
def test_dynamic_center_field_without_a_value_fails(tmp_path, built, fmap, missing):
    with pytest.raises(MissingValue, match=f"{missing} is a dynamic field.*center settings"):
        rendered(tmp_path, built, fmap, make_data(built, **{missing: None}))
    assert not (tmp_path / "out.docx").exists()


@pytest.mark.parametrize("supplied", ["ada", "representative"])
def test_static_center_field_cannot_be_overridden(tmp_path, built, supplied):
    fmap = production_field_map(built, center_mode="static")
    data = make_data(built, **{"ada": None, "representative": None, supplied: "Νέα τιμή"})
    with pytest.raises(InvalidDocumentData, match=f"{supplied} is static"):
        rendered(tmp_path, built, fmap, data)


def test_center_field_mode_must_be_stated(built, fmap):
    from pydantic import ValidationError

    from app.documents.field_map import FieldMap

    raw = fmap.model_dump()
    del raw["ada"]["mode"]
    with pytest.raises(ValidationError, match="mode"):
        FieldMap.model_validate(raw)


def test_unmapped_center_fields_need_no_value(tmp_path, built):
    """A template without an ΑΔΑ/representative mapping simply has no such field."""
    fmap = production_field_map(built, ada=None, representative=None)
    out, result = rendered(tmp_path, built, fmap, make_data(built, ada=None, representative=None))
    assert result.kept == []
    assert verify_document(built.path, out, result, fmap) == []


# ---------------------------------------------------------------- zero session dates (D-24)
@pytest.mark.parametrize("empty", ["speech", "occupational", "physiotherapy"])
def test_a_therapy_with_zero_session_dates_stops_generation(tmp_path, built, fmap, empty):
    sessions = {b.code: ([] if b.code == empty else [date(2026, 10, 20)]) for b in built.blocks}
    with pytest.raises(MissingValue, match=f"no session dates for therapy '{empty}'"):
        rendered(tmp_path, built, fmap, make_data(built, sessions=sessions))
    assert not (tmp_path / "out.docx").exists()


# ---------------------------------------------------------------- failures
def test_template_hash_mismatch(tmp_path, built, data):
    fmap = production_field_map(built, template_sha256="0" * 64)
    with pytest.raises(TemplateIntegrityError, match="SHA-256"):
        render(built.path, fmap, data, tmp_path / "o.docx")


@pytest.mark.parametrize(
    "override, error, match",
    [
        # D-28: every section of the (exactly matched) template must be filled
        ({"sessions": {}}, MissingValue, "no session dates supplied"),
        ({"sessions": {"speech": [date(2026, 10, 16)]}}, MissingValue, "no session dates supplied"),
        (
            {
                "sessions": {
                    c: [date(2026, 10, 16)]
                    for c in ("speech", "occupational", "physiotherapy", "psychotherapy")
                }
            },
            InvalidDocumentData,
            "wrong template",
        ),
        (
            {
                "sessions": {
                    c: [date(2026, 11, 16)] for c in ("speech", "occupational", "physiotherapy")
                }
            },
            InvalidDocumentData,
            "outside the period",
        ),
        (
            {"sessions": {c: [] for c in ("speech", "occupational", "physiotherapy")}},
            MissingValue,
            "no session dates",
        ),
        ({"amka": "1234"}, InvalidDocumentData, "AMKA"),
        ({"period_end": PERIOD[0].replace(day=1)}, InvalidDocumentData, "before period start"),
        ({"ada": "  "}, InvalidDocumentData, "blank"),
    ],
)
def test_invalid_data_is_refused(tmp_path, built, fmap, override, error, match):
    with pytest.raises(error, match=match):
        render(built.path, fmap, make_data(built, **override), tmp_path / "o.docx")
    assert not (tmp_path / "o.docx").exists()


def test_unexpected_template_text_is_refused(tmp_path, built, fmap, data):
    blocks = list(fmap.blocks)
    # point the dates of the first block at its heading run: not a date list
    blocks[0] = blocks[0].model_copy(update={"dates_loc": blocks[0].label_loc})
    with pytest.raises(TemplateMismatch, match="not a date_list"):
        render(
            built.path, fmap.model_copy(update={"blocks": tuple(blocks)}), data, tmp_path / "o.docx"
        )


def test_missing_location_is_refused(tmp_path, built, fmap, data):
    from app.documents.field_map import Locator, TextField

    bad = TextField(loc=Locator(paragraph=999, runs=(0, 0)), expect="date")
    with pytest.raises(TemplateMismatch, match="does not exist"):
        render(built.path, fmap.model_copy(update={"issue_date": bad}), data, tmp_path / "o.docx")


def test_run_with_tab_element_is_refused(tmp_path, built, fmap, data):
    doc = Document(str(built.path))
    run = doc.paragraphs[built.layout.issue_date_paragraph].runs[0]
    run._r.append(run._r.makeelement(qn("w:tab"), {}))
    doc.save(str(tmp_path / "c.docx"))
    built.path.unlink()
    (tmp_path / "c.docx").rename(built.path)
    fmap = production_field_map(built)
    with pytest.raises(TemplateMismatch, match="exactly one text node"):
        render(built.path, fmap, data, tmp_path / "o.docx")


def test_header_location_must_exist(tmp_path, built, fmap, data):
    from app.documents.field_map import CenterField, Locator

    bad = CenterField(
        loc=Locator(part="header", kind="first", paragraph=0, runs=(0, 0)),
        expect="text",
        mode="dynamic",
    )
    with pytest.raises(TemplateMismatch, match="not defined"):
        render(
            built.path, fmap.model_copy(update={"representative": bad}), data, tmp_path / "o.docx"
        )


def test_no_patient_data_in_error_messages(tmp_path, built, fmap):
    data = make_data(
        built,
        sessions={c: [date(2026, 12, 1)] for c in ("speech", "occupational", "physiotherapy")},
    )
    with pytest.raises(InvalidDocumentData) as info:
        render(built.path, fmap, data, tmp_path / "o.docx")
    message = str(info.value)
    for value in ("ΓΕΩΡΓΙΟΣ", "ΔΟΚΙΜΑΣΤΙΚΟΣ", "12345678901", "1/12/26"):
        assert value not in message
    assert sha256_file(built.path)  # template still readable
    assert "12345678901" not in all_text(built.path)
