"""File store under THERAPY_DATA_DIR (outside the repository, D-05). (P-32)

Layout:
  templates/<sha256>.docx       uploaded template files, content-addressed
  documents/<name>.{docx,pdf}   issued documents
  previews/<id>/                temporary generation output, safe to delete

Rules: files are written to a temporary name and moved into place atomically, then
made read-only; an existing stored file is never overwritten (an identical template
upload returns the existing file after re-hashing it); paths stored in the database
are relative to the store root and are resolved without allowing ``..`` escapes;
every read can be checked against the recorded SHA-256.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.documents.errors import StorageError
from app.documents.package import inspect_docx, sha256_file

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")


@dataclass(frozen=True)
class StoredFile:
    relative_path: str  # what the database records, e.g. "templates/<sha>.docx"
    path: Path
    sha256: str
    size_bytes: int


class FileStore:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        if any((p / ".git").exists() for p in [self.root, *self.root.parents]):
            raise StorageError(f"storage root {self.root} is inside a Git repository")
        for sub in ("templates", "documents", "previews"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ paths
    def resolve(self, relative_path: str) -> Path:
        candidate = (self.root / relative_path).resolve()
        if not candidate.is_relative_to(self.root) or candidate == self.root:
            raise StorageError(f"path {relative_path!r} points outside the file store")
        return candidate

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    # ------------------------------------------------------------------ writes
    def _place(self, source: Path, target: Path) -> None:
        if target.exists():
            raise StorageError(f"{self._relative(target)} already exists and is never overwritten")
        partial = target.with_name(f".{target.name}.{uuid.uuid4().hex}.partial")
        try:
            shutil.copyfile(source, partial)
            os.replace(partial, target)
        finally:
            partial.unlink(missing_ok=True)
        target.chmod(stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)

    def put_template(self, source: Path) -> StoredFile:
        """Store an uploaded template (the upload itself is only read). The file must be
        a safe DOCX; an identical file already stored is reused after re-hashing."""
        info = inspect_docx(source)
        target = self.root / "templates" / f"{info.sha256}.docx"
        if target.exists():
            self.verify(self._relative(target), info.sha256)
        else:
            self._place(Path(source), target)
            if sha256_file(target) != info.sha256:
                raise StorageError("stored template does not match the uploaded file")
        return StoredFile(self._relative(target), target, info.sha256, info.size_bytes)

    def put_document(self, source: Path, name: str) -> StoredFile:
        """Store an issued file under ``documents/<name>``; ``name`` must be a plain file
        name (no folders, no patient data — callers use ids, e.g. 'period-12-v1.pdf')."""
        if not _NAME_RE.fullmatch(name) or name.endswith(".partial"):
            raise StorageError(f"invalid document file name {name!r}")
        expected = sha256_file(source)
        target = self.root / "documents" / name
        self._place(Path(source), target)
        if sha256_file(target) != expected:
            raise StorageError(f"stored document {name} does not match its source")
        return StoredFile(self._relative(target), target, expected, target.stat().st_size)

    # ------------------------------------------------------------------ reads
    def verify(self, relative_path: str, sha256: str) -> Path:
        """Return the file's path after checking it exists and still has ``sha256``."""
        path = self.resolve(relative_path)
        if not path.is_file():
            raise StorageError(f"stored file {relative_path} is missing")
        if sha256_file(path) != sha256:
            raise StorageError(f"stored file {relative_path} has been altered (SHA-256 differs)")
        return path

    # ------------------------------------------------------------------ previews
    def new_preview_dir(self) -> Path:
        path = self.root / "previews" / uuid.uuid4().hex
        path.mkdir()
        return path

    def cleanup_previews(self, older_than_seconds: float = 24 * 3600) -> int:
        """Delete preview folders older than the given age. Returns how many were removed."""
        cutoff = time.time() - older_than_seconds
        removed = 0
        for entry in (self.root / "previews").iterdir():
            if entry.is_dir() and entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry)
                removed += 1
        return removed
