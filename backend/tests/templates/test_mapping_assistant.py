"""Mapping assistant: suggestions for the EOPYY form family, never silent guesses."""

from __future__ import annotations

import pytest
from docx import Document

from app.documents import TemplateFileError, validate_template
from app.documents.mapping import suggest_field_map
from fixtures import eopyy_builder as fx
from fixtures.eopyy_maps import production_field_map

CODES = {b.label: b.code for b in fx.DEFAULT_BLOCKS}


def test_suggestion_equals_the_hand_made_map(tmp_path):
    built = fx.build_eopyy_docx(tmp_path / "t.docx")
    suggestion = suggest_field_map(
        built.path, therapy_codes=CODES, ada_mode="dynamic", representative_mode="dynamic"
    )
    assert suggestion.notes == []
    expected = production_field_map(built).model_copy(update={"forbidden_text": ()})
    assert suggestion.field_map == expected
    assert validate_template(built.path, suggestion.field_map).ok


def test_center_field_modes_are_the_administrators_choice(tmp_path):
    built = fx.build_eopyy_docx(tmp_path / "t.docx")
    static = suggest_field_map(
        built.path, therapy_codes=CODES, ada_mode="static", representative_mode=None
    )
    assert static.field_map.ada.mode == "static" and static.field_map.representative is None
    assert validate_template(built.path, static.field_map).ok


def test_ada_in_the_header_is_found(tmp_path):
    built = fx.build_eopyy_docx(tmp_path / "t.docx", ada_in_header=True)
    suggestion = suggest_field_map(
        built.path, therapy_codes=CODES, ada_mode="dynamic", representative_mode="dynamic"
    )
    assert suggestion.field_map.ada.loc.part == "header"
    assert validate_template(built.path, suggestion.field_map).ok


def test_unknown_headings_are_reported_not_guessed(tmp_path):
    built = fx.build_eopyy_docx(tmp_path / "t.docx")
    partial = {k: v for k, v in CODES.items() if v != "occupational"}
    suggestion = suggest_field_map(
        built.path, therapy_codes=partial, ada_mode=None, representative_mode=None
    )
    assert any("'Εργοθεραπεία:' is not linked to a therapy" in n for n in suggestion.notes)
    assert {b.therapy for b in suggestion.field_map.blocks} == {"speech", "physiotherapy"}
    report = validate_template(built.path, suggestion.field_map)
    assert not report.ok and any("3 tables but 2" in e for e in report.errors)


def test_split_patient_text_needs_manual_mapping(tmp_path):
    built = fx.build_eopyy_docx(tmp_path / "t.docx", split_runs=True)
    suggestion = suggest_field_map(
        built.path, therapy_codes=CODES, ada_mode=None, representative_mode=None
    )
    assert suggestion.field_map is None
    assert any("split over several runs" in n for n in suggestion.notes)


def test_other_documents_get_notes_not_a_map(tmp_path):
    path = tmp_path / "letter.docx"
    doc = Document()
    doc.add_paragraph("Μια απλή επιστολή χωρίς πίνακες.")
    doc.save(str(path))
    suggestion = suggest_field_map(
        path, therapy_codes=CODES, ada_mode="dynamic", representative_mode="dynamic"
    )
    assert suggestion.field_map is None and suggestion.notes


def test_unsafe_files_are_refused(tmp_path):
    bad = tmp_path / "bad.docx"
    bad.write_bytes(b"nope")
    with pytest.raises(TemplateFileError):
        suggest_field_map(bad, therapy_codes=CODES, ada_mode=None, representative_mode=None)
