"""PDF conversion (real LibreOffice when installed; a fake soffice for failure modes)
and PDF validation."""

from __future__ import annotations

import os
import stat
import sys
import time
from pathlib import Path

import pytest

from app.documents import (
    LibreOfficeNotFound,
    PdfConversionError,
    PdfInvalid,
    convert_docx_to_pdf,
    find_soffice,
    libreoffice_version,
    render,
    validate_pdf,
)

FAKE_SOFFICE = r"""
import sys, time
from pathlib import Path
mode = Path(__file__).with_suffix(".mode").read_text().strip()
args = sys.argv[1:]
out_dir = Path(args[args.index("--outdir") + 1])
if mode == "hang":
    time.sleep(120)
elif mode == "crash":
    sys.stderr.write("simulated crash\n"); sys.exit(3)
elif mode == "silent":
    sys.exit(0)  # LibreOffice sometimes 'succeeds' without writing anything
elif mode == "garbage":
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "document.pdf").write_bytes(b"this is not a pdf")
"""


def fake_soffice(tmp_path: Path, mode: str) -> Path:
    script = tmp_path / "fake_soffice.py"
    script.write_text(FAKE_SOFFICE, encoding="utf-8")
    script.with_suffix(".mode").write_text(mode, encoding="utf-8")
    if sys.platform == "win32":
        launcher = tmp_path / "soffice.cmd"
        launcher.write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8")
    else:
        launcher = tmp_path / "soffice"
        launcher.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8"
        )
        launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
    return launcher


def unembedded_pdf(path: Path) -> Path:
    """A one-page PDF saying 'Hello' in the standard font Helvetica, which is not embedded."""
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(200, 200)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    content = DecodedStreamObject()
    content.set_data(b"BT /F1 12 Tf 20 100 Td (Hello) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(content)
    with path.open("wb") as fh:
        writer.write(fh)
    return path


# ---------------------------------------------------------------- finding LibreOffice
def test_configured_path_that_does_not_exist_is_an_error(tmp_path):
    with pytest.raises(LibreOfficeNotFound, match="configured"):
        find_soffice(tmp_path / "nope.exe")


def test_configured_path_is_used(tmp_path):
    launcher = fake_soffice(tmp_path, "silent")
    assert find_soffice(launcher) == launcher


# ---------------------------------------------------------------- failure modes (fake soffice)
@pytest.mark.parametrize(
    "mode, match",
    [
        ("crash", "exited with code 3: simulated crash"),
        ("silent", "reported success but wrote no PDF"),
    ],
)
def test_conversion_failures_are_explicit(tmp_path, built, mode, match):
    with pytest.raises(PdfConversionError, match=match):
        convert_docx_to_pdf(built.path, tmp_path / "out.pdf", soffice=fake_soffice(tmp_path, mode))
    assert not (tmp_path / "out.pdf").exists()


def test_timeout_kills_the_conversion(tmp_path, built):
    started = time.monotonic()
    with pytest.raises(PdfConversionError, match="did not finish within 3 s"):
        convert_docx_to_pdf(
            built.path, tmp_path / "out.pdf", soffice=fake_soffice(tmp_path, "hang"), timeout=3
        )
    assert time.monotonic() - started < 60
    assert not (tmp_path / "out.pdf").exists()


def test_garbage_output_fails_validation(tmp_path, built):
    pdf = convert_docx_to_pdf(
        built.path, tmp_path / "out.pdf", soffice=fake_soffice(tmp_path, "garbage")
    )
    with pytest.raises(PdfInvalid, match="missing %PDF header"):
        validate_pdf(pdf)


def test_missing_input_and_existing_output(tmp_path, built):
    soffice = fake_soffice(tmp_path, "silent")
    with pytest.raises(PdfConversionError, match="input not found"):
        convert_docx_to_pdf(tmp_path / "nope.docx", tmp_path / "o.pdf", soffice=soffice)
    (tmp_path / "exists.pdf").write_bytes(b"x")
    with pytest.raises(FileExistsError):
        convert_docx_to_pdf(built.path, tmp_path / "exists.pdf", soffice=soffice)


# ---------------------------------------------------------------- validation without LibreOffice
def test_truncated_and_non_pdf_files_are_rejected(tmp_path):
    p = tmp_path / "x.pdf"
    p.write_bytes(unembedded_pdf(tmp_path / "ok.pdf").read_bytes()[:-7])
    with pytest.raises(PdfInvalid, match="truncated"):
        validate_pdf(p)
    p.write_bytes(b"<html>")
    with pytest.raises(PdfInvalid, match="missing %PDF header"):
        validate_pdf(p)
    with pytest.raises(PdfInvalid, match="not found"):
        validate_pdf(tmp_path / "missing.pdf")


def test_non_embedded_fonts_are_rejected(tmp_path):
    p = unembedded_pdf(tmp_path / "x.pdf")
    with pytest.raises(PdfInvalid, match="fonts not embedded.*Helvetica"):
        validate_pdf(p)
    report = validate_pdf(p, require_embedded_fonts=False)
    assert report.pages == 1 and report.fonts == ("Helvetica",)


def test_page_count_and_expected_text(tmp_path):
    p = unembedded_pdf(tmp_path / "x.pdf")
    with pytest.raises(PdfInvalid, match="1 page\\(s\\), expected 2"):
        validate_pdf(p, expected_pages=2, require_embedded_fonts=False)
    with pytest.raises(PdfInvalid, match="value of 'greeting' does not appear"):
        validate_pdf(p, expect_text={"greeting": "Goodbye"}, require_embedded_fonts=False)
    validate_pdf(p, expect_text={"greeting": "Hello"}, require_embedded_fonts=False)


# ---------------------------------------------------------------- real LibreOffice
@pytest.mark.libreoffice
def test_real_conversion_of_a_generated_document(tmp_path, built, fmap, data, soffice):
    out = tmp_path / "out.docx"
    render(built.path, fmap, data, out)
    pdf = convert_docx_to_pdf(out, tmp_path / "out.pdf", soffice=soffice)
    report = validate_pdf(
        pdf,
        expected_pages=1,
        expect_text={
            "patient": "ΓΕΩΡΓΙΟΣ ΔΟΚΙΜΑΣΤΙΚΟΣ του ΝΙΚΟΛΑΟΥ με ΑΜΚΑ 12345678901",
            "period": "16/10/2026 έως 15/11/2026",
            "dates": "16/10/26, 20/10/26",
        },
    )
    assert report.pages == 1 and report.fonts  # every font embedded (checked by validate_pdf)


@pytest.mark.libreoffice
def test_real_conversion_of_a_corrupted_docx_fails_explicitly(tmp_path, soffice):
    bad = tmp_path / "bad.docx"
    bad.write_bytes(b"PK\x03\x04 not really a document")
    with pytest.raises((PdfConversionError, PdfInvalid)):
        pdf = convert_docx_to_pdf(bad, tmp_path / "bad.pdf", soffice=soffice)
        validate_pdf(pdf)


@pytest.mark.libreoffice
def test_libreoffice_version_is_reported(soffice):
    version = libreoffice_version(soffice)
    assert version is None or version.startswith("LibreOffice ")


def test_no_patient_name_reaches_the_converter_command_line(tmp_path, built):
    """The input is copied to a neutral name first, so LibreOffice never sees a patient
    name in a path, whatever the caller's file is called."""
    named = tmp_path / "ΔΟΚΙΜΑΣΤΙΚΟΣ ΓΕΩΡΓΙΟΣ.docx"
    named.write_bytes(built.path.read_bytes())
    log = tmp_path / "args.txt"
    script = tmp_path / "rec.py"
    script.write_text(
        f"import sys; open(r'{log}', 'w', encoding='utf-8').write(' '.join(sys.argv))",
        encoding="utf-8",
    )
    if sys.platform == "win32":
        launcher = tmp_path / "rec.cmd"
        launcher.write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8")
    else:
        launcher = tmp_path / "rec"
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
        os.chmod(launcher, 0o755)
    with pytest.raises(PdfConversionError):
        convert_docx_to_pdf(named, tmp_path / "o.pdf", soffice=launcher)
    recorded = log.read_text(encoding="utf-8")
    assert "document.docx" in recorded and "ΔΟΚΙΜΑΣΤΙΚΟΣ" not in recorded
