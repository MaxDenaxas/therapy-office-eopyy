"""Produce a verified DOCX and PDF for one document period (no database, no API).

render -> verify DOCX -> convert -> validate PDF. Any failure raises; nothing is
returned unless every check passed. Files are written into ``workdir`` (e.g. a
preview folder of the file store); issuing them is the caller's job (M7).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .errors import DocumentVerificationError
from .field_map import FieldMap, format_date
from .pdf import PdfReport, convert_docx_to_pdf, libreoffice_version, validate_pdf
from .render import DocumentData, RenderResult, render
from .verify import verify_document


@dataclass(frozen=True)
class GeneratedDocument:
    docx: Path
    pdf: Path
    render: RenderResult
    pdf_report: PdfReport
    libreoffice_version: str | None


def generate_document(
    template: Path,
    field_map: FieldMap,
    data: DocumentData,
    workdir: Path,
    *,
    soffice: Path,
    timeout: int = 120,
    expected_pages: int | None = None,
) -> GeneratedDocument:
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    docx, pdf = workdir / "document.docx", workdir / "document.pdf"

    result = render(template, field_map, data, docx)
    problems = verify_document(template, docx, result, field_map)
    if problems:
        raise DocumentVerificationError(problems)

    convert_docx_to_pdf(docx, pdf, soffice=soffice, timeout=timeout)
    fmt = field_map.formats
    expect_text = {
        "patient": field_map.patient_format.format(
            first=data.first_name, last=data.last_name, father=data.father_name, amka=data.amka
        ),
        "period": format_date(data.period_start, fmt.period_date)
        + fmt.period_separator
        + format_date(data.period_end, fmt.period_date),
        "issue_date": format_date(data.issue_date, fmt.issue_date),
    }
    report = validate_pdf(pdf, expected_pages=expected_pages, expect_text=expect_text)
    return GeneratedDocument(docx, pdf, result, report, libreoffice_version(soffice))
