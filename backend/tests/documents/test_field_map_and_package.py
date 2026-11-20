"""Field-map schema, date formatting, template hashing and safe DOCX inspection."""

from __future__ import annotations

import hashlib
import zipfile
from datetime import date

import pytest
from pydantic import ValidationError

from app.documents import TemplateFileError, inspect_docx, sha256_file
from app.documents.field_map import FieldMap, Locator, TextField, format_date
from app.documents.package import MAX_RATIO


# ---------------------------------------------------------------- field map schema
def test_field_map_round_trips_through_json(fmap):
    again = FieldMap.model_validate_json(fmap.model_dump_json())
    assert again == fmap


def test_unknown_keys_and_bad_values_are_rejected(fmap):
    raw = fmap.model_dump()
    with pytest.raises(ValidationError):
        FieldMap.model_validate({**raw, "surprise": 1})
    with pytest.raises(ValidationError):
        FieldMap.model_validate({**raw, "template_sha256": "abc"})
    with pytest.raises(ValidationError):
        FieldMap.model_validate({**raw, "patient_format": "{first} {last}"})  # AMKA missing
    with pytest.raises(ValidationError):
        FieldMap.model_validate({**raw, "blocks": (raw["blocks"][0], raw["blocks"][0])})
    with pytest.raises(ValidationError):
        FieldMap.model_validate({**raw, "blocks": ()})


@pytest.mark.parametrize(
    "kwargs",
    [
        {"paragraph": 0, "runs": (2, 1)},  # reversed span
        {"paragraph": 0, "runs": (0, 0), "table": 1},  # table without row/cell
        {"paragraph": 0, "runs": (0, 0), "section": 1},  # section only for header/footer
    ],
)
def test_invalid_locators(kwargs):
    with pytest.raises(ValidationError):
        Locator(**kwargs)


def test_text_field_needs_exactly_one_mode():
    loc = Locator(paragraph=0, runs=(0, 0))
    with pytest.raises(ValidationError):
        TextField(loc=loc)
    with pytest.raises(ValidationError):
        TextField(loc=loc, expect="date", between=("a", "b"))


@pytest.mark.parametrize(
    "fmt, expected",
    [
        ("d/MM/yyyy", "2/09/2026"),
        ("d/M/yy", "2/9/26"),
        ("dd/MM/yyyy", "02/09/2026"),
        ("dd.MM.yy", "02.09.26"),
    ],
)
def test_date_formats(fmt, expected):
    assert format_date(date(2026, 9, 2), fmt) == expected


def test_unsupported_date_format_is_rejected(fmap):
    with pytest.raises(ValidationError, match="unsupported date format"):
        FieldMap.model_validate({**fmap.model_dump(), "formats": {"issue_date": "%d/%m/%Y"}})


# ---------------------------------------------------------------- hashing / package
def test_hash_is_sha256_of_the_bytes(built):
    expected = hashlib.sha256(built.path.read_bytes()).hexdigest()
    assert sha256_file(built.path) == inspect_docx(built.path).sha256 == expected


def test_valid_fixture_passes_inspection(built):
    info = inspect_docx(built.path)
    assert "word/document.xml" in info.parts and len(info.media) == 2


def _rezip(src, dst, *, drop=(), replace=None, add=None):
    replace, add = replace or {}, add or {}
    with zipfile.ZipFile(src) as zi, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zo:
        for item in zi.infolist():
            if item.filename in drop:
                continue
            zo.writestr(item.filename, replace.get(item.filename, zi.read(item.filename)))
        for name, blob in add.items():
            zo.writestr(name, blob)
    return dst


@pytest.mark.parametrize(
    "make, match",
    [
        (lambda p, t: t.write_bytes(b""), "empty"),
        (lambda p, t: t.write_bytes(b"not a zip at all"), "not a ZIP"),
        (lambda p, t: t.write_bytes(p.read_bytes()[:500]), "not a ZIP|corrupted"),
        (lambda p, t: _rezip(p, t, drop=("word/document.xml",)), "missing word/document.xml"),
        (lambda p, t: _rezip(p, t, add={"word/vbaProject.bin": b"x"}), "macro"),
        (
            lambda p, t: _rezip(p, t, replace={"word/document.xml": b"<w:document><broken"}),
            "malformed XML",
        ),
        (
            lambda p, t: _rezip(
                p,
                t,
                replace={
                    "word/document.xml": b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><x>&e;</x>'
                },
            ),
            "DOCTYPE",
        ),
        (
            lambda p, t: _rezip(
                p,
                t,
                add={
                    "word/_rels/extra.xml.rels": b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    b'<Relationship Id="r1" TargetMode="External" '
                    b'Target="http://example.invalid/x.png" '
                    b'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"/>'
                    b"</Relationships>"
                },
            ),
            "external reference",
        ),
        (lambda p, t: _rezip(p, t, add={"bomb.txt": b"0" * (MAX_RATIO * 2000)}), "compression"),
    ],
)
def test_unsafe_or_corrupted_files_are_rejected(built, tmp_path, make, match):
    target = tmp_path / "bad.docx"
    make(built.path, target)
    with pytest.raises(TemplateFileError, match=match):
        inspect_docx(target)


def test_macro_enabled_content_type_is_rejected(built, tmp_path):
    with zipfile.ZipFile(built.path) as z:
        ct = z.read("[Content_Types].xml")
    bad = ct.replace(b"document.main+xml", b"document.macroEnabled.main+xml")
    target = _rezip(built.path, tmp_path / "m.docx", replace={"[Content_Types].xml": bad})
    with pytest.raises(TemplateFileError, match="not a plain Word document"):
        inspect_docx(target)


def test_missing_file(tmp_path):
    with pytest.raises(TemplateFileError, match="not found"):
        inspect_docx(tmp_path / "nope.docx")


def test_hyperlinks_are_allowed(built, tmp_path):
    rels = (
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Id="r1" TargetMode="External" Target="https://example.invalid/" '
        b'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"/>'
        b"</Relationships>"
    )
    target = _rezip(built.path, tmp_path / "h.docx", add={"word/_rels/extra.xml.rels": rels})
    inspect_docx(target)
