"""Explicit failure types of the document engine.

Messages name fields, locations and template text, never patient data: the values
being written (names, AMKA, dates) are kept out of exceptions and logs.
"""

from __future__ import annotations


class DocumentError(Exception):
    """Base class: the document must not be considered produced."""


class TemplateFileError(DocumentError):
    """The template file is unreadable, corrupted or unsafe (not a well-formed DOCX)."""


class TemplateIntegrityError(DocumentError):
    """The template file is not the one the field map was made for, or it changed."""


class TemplateMismatch(DocumentError):
    """The template does not match its field map (a location or its expected text)."""


class MissingValue(DocumentError):
    """A value the template needs was not supplied (e.g. home visit ΝΑΙ/ΟΧΙ)."""


class InvalidDocumentData(DocumentError):
    """Supplied data is inconsistent (e.g. a session date outside the period)."""


class DocumentVerificationError(DocumentError):
    """The generated DOCX failed verification. ``problems`` lists every finding."""

    def __init__(self, problems: list[str]):
        self.problems = list(problems)
        super().__init__("Generated document failed verification:\n- " + "\n- ".join(problems))


class LibreOfficeNotFound(DocumentError):
    """No usable LibreOffice (soffice) executable."""


class PdfConversionError(DocumentError):
    """LibreOffice did not produce a PDF (error exit, timeout or missing output)."""


class PdfInvalid(DocumentError):
    """The PDF exists but is not acceptable. ``problems`` lists every finding."""

    def __init__(self, problems: list[str]):
        self.problems = list(problems)
        super().__init__("PDF failed validation:\n- " + "\n- ".join(problems))


class StorageError(DocumentError):
    """A stored file is missing, altered, or an operation would overwrite one."""
