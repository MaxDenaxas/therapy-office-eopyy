"""DOCX -> PDF via LibreOffice headless (spike)."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def find_soffice() -> Path:
    env = os.environ.get("SOFFICE_PATH")
    candidates = [Path(env)] if env else []
    which = shutil.which("soffice")
    if which:
        candidates.append(Path(which))
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if base:
            candidates.append(Path(base) / "LibreOffice" / "program" / "soffice.exe")
    for c in candidates:
        if c.is_file():
            return c
    raise FileNotFoundError("LibreOffice (soffice) not found. Set SOFFICE_PATH.")


def docx_to_pdf(docx: Path, out_dir: Path, timeout: int = 180) -> Path:
    docx, out_dir = Path(docx).resolve(), Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    # Private profile dir avoids lock/crash problems with a user's running LibreOffice.
    with tempfile.TemporaryDirectory(prefix="lo-profile-") as profile:
        cmd = [
            str(find_soffice()),
            f"-env:UserInstallation={Path(profile).as_uri()}",
            "--headless", "--norestore",
            "--convert-to", "pdf:writer_pdf_Export",
            "--outdir", str(out_dir),
            str(docx),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    pdf = out_dir / (docx.stem + ".pdf")
    if proc.returncode != 0 or not pdf.is_file():
        raise RuntimeError(f"LibreOffice conversion failed: {proc.stdout}\n{proc.stderr}")
    return pdf
