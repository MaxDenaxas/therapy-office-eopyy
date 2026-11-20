"""Document engine: templates + field maps + data -> verified DOCX and PDF.

Independent of the API, the database and the frontend. Entry points:
  inspect_docx       safe package check + SHA-256 of a DOCX
  validate_template  activation gate for a template version and its field map
  render             fill one template (DOCX only)
  verify_document    prove a generated DOCX differs from its template only where mapped
  generate_document  render -> verify -> PDF -> validate, all or nothing
"""

from .errors import (
    DocumentError,
    DocumentVerificationError,
    InvalidDocumentData,
    LibreOfficeNotFound,
    MissingValue,
    PdfConversionError,
    PdfInvalid,
    StorageError,
    TemplateFileError,
    TemplateIntegrityError,
    TemplateMismatch,
)
from .field_map import FieldMap, format_date
from .package import PackageInfo, inspect_docx, sha256_file
from .pdf import PdfReport, convert_docx_to_pdf, find_soffice, libreoffice_version, validate_pdf
from .pipeline import GeneratedDocument, generate_document
from .render import DocumentData, RenderResult, render
from .validate import ValidationReport, validate_template
from .verify import verify_document

__all__ = [
    "DocumentData",
    "DocumentError",
    "DocumentVerificationError",
    "FieldMap",
    "GeneratedDocument",
    "InvalidDocumentData",
    "LibreOfficeNotFound",
    "MissingValue",
    "PackageInfo",
    "PdfConversionError",
    "PdfInvalid",
    "PdfReport",
    "RenderResult",
    "StorageError",
    "TemplateFileError",
    "TemplateIntegrityError",
    "TemplateMismatch",
    "ValidationReport",
    "convert_docx_to_pdf",
    "find_soffice",
    "format_date",
    "generate_document",
    "inspect_docx",
    "libreoffice_version",
    "render",
    "sha256_file",
    "validate_pdf",
    "validate_template",
    "verify_document",
]
