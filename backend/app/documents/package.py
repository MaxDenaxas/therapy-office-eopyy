"""DOCX package inspection: hashing and safety checks before python-docx touches a file.

A DOCX is a ZIP of XML parts. We refuse anything that is not a plain Word document:
broken or oversized archives (zip bombs), macro-enabled packages, XML with DOCTYPE /
entity declarations (XXE), and relationships that point outside the package (other
than hyperlinks) — LibreOffice would try to fetch those during conversion.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from dataclasses import dataclass
from pathlib import Path

from lxml import etree

from .errors import TemplateFileError

MAIN_DOCUMENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
)
REQUIRED_PARTS = ("[Content_Types].xml", "_rels/.rels", "word/document.xml")
HYPERLINK_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"

MAX_FILE_BYTES = 20 * 1024 * 1024  # a template is a few hundred KB at most
MAX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
MAX_ENTRIES = 2000
MAX_RATIO = 200  # uncompressed/compressed per entry

_SAFE_PARSER = etree.XMLParser(
    resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False
)


@dataclass(frozen=True)
class PackageInfo:
    sha256: str
    size_bytes: int
    parts: tuple[str, ...]
    media: tuple[str, ...]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_xml(data: bytes, name: str) -> etree._Element:
    """Parse one package part safely. DOCTYPE declarations are refused outright."""
    if b"<!DOCTYPE" in data[:2048].upper() or b"<!ENTITY" in data.upper():
        raise TemplateFileError(f"{name}: DOCTYPE/ENTITY declarations are not allowed")
    try:
        return etree.fromstring(data, parser=_SAFE_PARSER)
    except etree.XMLSyntaxError as e:
        raise TemplateFileError(f"{name}: malformed XML ({e})") from e


def inspect_docx(path: Path) -> PackageInfo:
    """Validate a DOCX file and return its hash. Raises TemplateFileError when unsafe."""
    path = Path(path)
    if not path.is_file():
        raise TemplateFileError(f"file not found: {path.name}")
    data = path.read_bytes()
    if not data:
        raise TemplateFileError("file is empty")
    if len(data) > MAX_FILE_BYTES:
        raise TemplateFileError(f"file is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB")
    return inspect_docx_bytes(data)


def inspect_docx_bytes(data: bytes) -> PackageInfo:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise TemplateFileError("not a DOCX file (not a ZIP archive)") from e
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ENTRIES:
            raise TemplateFileError("package has too many entries")
        total = 0
        for info in infos:
            if info.filename.startswith("/") or ".." in info.filename.split("/"):
                raise TemplateFileError(f"unsafe entry name {info.filename!r}")
            total += info.file_size
            if info.compress_size and info.file_size / info.compress_size > MAX_RATIO:
                raise TemplateFileError(f"{info.filename}: suspicious compression ratio")
        if total > MAX_UNCOMPRESSED_BYTES:
            raise TemplateFileError("package expands to an unreasonable size")
        names = {i.filename for i in infos}
        for required in REQUIRED_PARTS:
            if required not in names:
                raise TemplateFileError(f"not a Word document: missing {required}")
        if any(n.lower().endswith("vbaproject.bin") for n in names):
            raise TemplateFileError("macro-enabled documents are not accepted")
        try:
            bad = archive.testzip()
        except (zipfile.BadZipFile, OSError, EOFError) as e:
            raise TemplateFileError(f"corrupted archive ({e})") from e
        if bad is not None:
            raise TemplateFileError(f"corrupted archive entry {bad!r}")

        content_types = parse_xml(archive.read("[Content_Types].xml"), "[Content_Types].xml")
        main = [
            o.get("ContentType") for o in content_types if o.get("PartName") == "/word/document.xml"
        ]
        if main != [MAIN_DOCUMENT_TYPE]:
            raise TemplateFileError(
                "not a plain Word document (macro-enabled, template or other type)"
            )

        for name in sorted(names):
            if name.endswith((".xml", ".rels")):
                root = parse_xml(archive.read(name), name)
                if name.endswith(".rels"):
                    for rel in root:
                        if rel.get("TargetMode") == "External" and rel.get("Type") != HYPERLINK_REL:
                            raise TemplateFileError(
                                f"{name}: external reference "
                                f"({rel.get('Type', '').rsplit('/', 1)[-1]})"
                                " is not allowed"
                            )
        media = tuple(sorted(n for n in names if n.startswith("word/media/")))
    return PackageInfo(
        sha256=sha256_bytes(data), size_bytes=len(data), parts=tuple(sorted(names)), media=media
    )
