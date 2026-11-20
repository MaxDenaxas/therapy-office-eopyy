"""DOCX -> PDF with LibreOffice headless, and PDF validation (ported from ``spike/pdf.py``).

Conversion (P-31):
  * one conversion at a time (process-wide lock); a private LibreOffice profile per run
  * the input is copied to a neutral file name in a temporary folder, so no patient
    name ever appears in a path or in LibreOffice's output
  * a timeout kills the whole process tree (soffice starts soffice.bin as a child)
  * a non-zero exit, a missing or empty output, or an invalid PDF is an error —
    LibreOffice sometimes exits 0 without writing anything
Validation: real PDF header and end marker, readable, not encrypted, at least one page
(or exactly the expected count), all fonts embedded, expected texts present.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from .errors import LibreOfficeNotFound, PdfConversionError, PdfInvalid

_LOCK = threading.Lock()


def find_soffice(configured: Path | None = None) -> Path:
    """The configured executable, or the first one found in PATH / standard folders.
    A configured path that does not exist is an error, never silently replaced."""
    if configured is not None:
        if Path(configured).is_file():
            return Path(configured)
        raise LibreOfficeNotFound(f"configured LibreOffice not found: {configured}")
    candidates: list[Path] = []
    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if base:
            candidates.append(Path(base) / "LibreOffice" / "program" / "soffice.exe")
    candidates += [
        Path("/usr/bin/soffice"),
        Path("/usr/lib/libreoffice/program/soffice"),
        Path("/Applications/LibreOffice.app/Contents/MacOS/soffice"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise LibreOfficeNotFound(
        "LibreOffice (soffice) not found; install it or set THERAPY_SOFFICE_PATH"
    )


def libreoffice_version(soffice: Path) -> str | None:
    """e.g. 'LibreOffice 26.8.3.1 <build id>' (recorded on every issued document)."""
    console = soffice.with_name("soffice.com")  # on Windows only the .com variant prints
    exe = console if sys.platform == "win32" and console.is_file() else soffice
    try:
        out = subprocess.run(
            [str(exe), "--version"],
            capture_output=True,
            text=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    line = next((ln.strip() for ln in out.splitlines() if ln.strip().startswith("LibreOffice")), "")
    return line or None


def _popen_kwargs() -> dict:
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


def _kill_tree(proc: subprocess.Popen) -> None:
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False
        )
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    proc.kill()


def _tail(text: str, lines: int = 5) -> str:
    kept = [ln for ln in text.strip().splitlines() if ln.strip()][-lines:]
    return " | ".join(kept) if kept else "(no output)"


def convert_docx_to_pdf(docx: Path, dest: Path, *, soffice: Path, timeout: int = 120) -> Path:
    """Convert ``docx`` into ``dest`` (which must not exist). Validation is separate."""
    docx, dest = Path(docx), Path(dest)
    if dest.exists():
        raise FileExistsError(f"output already exists: {dest.name}")
    if not docx.is_file():
        raise PdfConversionError(f"input not found: {docx.name}")
    with _LOCK, tempfile.TemporaryDirectory(prefix="therapy-pdf-") as tmp:
        work = Path(tmp)
        source = work / "in" / "document.docx"
        source.parent.mkdir()
        shutil.copyfile(docx, source)
        out_dir = work / "out"
        cmd = [
            str(soffice),
            f"-env:UserInstallation={(work / 'profile').as_uri()}",
            "--headless",
            "--norestore",
            "--nologo",
            "--nodefault",
            "--nolockcheck",
            "--convert-to",
            "pdf:writer_pdf_Export",
            "--outdir",
            str(out_dir),
            str(source),
        ]
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
                **_popen_kwargs(),
            )
        except OSError as e:
            raise PdfConversionError(f"LibreOffice could not be started ({e.strerror})") from e
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as e:
            _kill_tree(proc)
            proc.communicate()
            raise PdfConversionError(f"LibreOffice did not finish within {timeout} s") from e
        produced = out_dir / "document.pdf"
        if proc.returncode != 0:
            raise PdfConversionError(
                f"LibreOffice exited with code {proc.returncode}: {_tail(stderr or stdout)}"
            )
        if not produced.is_file() or produced.stat().st_size == 0:
            raise PdfConversionError(
                f"LibreOffice reported success but wrote no PDF: {_tail(stderr or stdout)}"
            )
        dest.parent.mkdir(parents=True, exist_ok=True)
        partial = dest.with_name(dest.name + ".partial")
        try:
            shutil.copyfile(produced, partial)
            os.replace(partial, dest)
        finally:
            partial.unlink(missing_ok=True)
    return dest


@dataclass(frozen=True)
class PdfReport:
    pages: int
    fonts: tuple[str, ...]
    size_bytes: int


def _font_is_embedded(font) -> bool:
    if font.get("/Subtype") == "/Type3":
        return True  # glyphs are drawn inside the PDF itself
    descriptor = font.get("/FontDescriptor")
    if descriptor is None and font.get("/Subtype") == "/Type0":
        descendant = font["/DescendantFonts"][0].get_object()
        descriptor = descendant.get("/FontDescriptor")
    if descriptor is None:
        return False
    descriptor = descriptor.get_object()
    return any(key in descriptor for key in ("/FontFile", "/FontFile2", "/FontFile3"))


def validate_pdf(
    path: Path,
    *,
    expected_pages: int | None = None,
    expect_text: Mapping[str, str] | None = None,
    require_embedded_fonts: bool = True,
) -> PdfReport:
    """Raise PdfInvalid listing every problem. ``expect_text`` maps a field name to text
    that must appear (messages name the field, never the value)."""
    path = Path(path)
    if not path.is_file():
        raise PdfInvalid([f"PDF not found: {path.name}"])
    data = path.read_bytes()
    problems: list[str] = []
    if not data.startswith(b"%PDF-"):
        problems.append("not a PDF file (missing %PDF header)")
    if b"%%EOF" not in data[-2048:]:
        problems.append("PDF is truncated (no end-of-file marker)")
    if problems:
        raise PdfInvalid(problems)
    try:
        reader = PdfReader(path, strict=False)
        if reader.is_encrypted:
            raise PdfInvalid(["PDF is encrypted"])
        pages = len(reader.pages)
        fonts: dict[str, bool] = {}
        text_parts = []
        for page in reader.pages:
            resources = page.get("/Resources")
            font_dict = resources.get_object().get("/Font") if resources is not None else None
            if font_dict is not None:
                for ref in font_dict.get_object().values():
                    font = ref.get_object()
                    name = str(font.get("/BaseFont", "(unnamed)")).lstrip("/")
                    name = re.sub(r"^[A-Z]{6}\+", "", name)  # subset prefix
                    fonts[name] = fonts.get(name, True) and _font_is_embedded(font)
            text_parts.append(page.extract_text() or "")
    except PdfInvalid:
        raise
    except (PdfReadError, KeyError, ValueError, TypeError) as e:
        raise PdfInvalid([f"PDF cannot be read ({type(e).__name__})"]) from e

    if pages == 0:
        problems.append("PDF has no pages")
    if expected_pages is not None and pages != expected_pages:
        problems.append(f"PDF has {pages} page(s), expected {expected_pages}")
    not_embedded = sorted(n for n, embedded in fonts.items() if not embedded)
    if require_embedded_fonts and not_embedded:
        problems.append(f"fonts not embedded (output would depend on the printer): {not_embedded}")
    text = " ".join(" ".join(text_parts).split())
    for name, expected in (expect_text or {}).items():
        if " ".join(expected.split()) not in text:
            problems.append(f"the value of {name!r} does not appear in the PDF text")
    if problems:
        raise PdfInvalid(problems)
    return PdfReport(pages=pages, fonts=tuple(sorted(fonts)), size_bytes=len(data))
