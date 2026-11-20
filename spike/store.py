"""Minimal SQLite + file store showing the document-safety rules (spike).

Rules enforced by the database itself (triggers), not only by application code:
  * template_versions: file identity (sha256/path/field_map/key) can never change; no deletes
  * documents: append-only (no UPDATE, no DELETE)
  * period_therapies: no edits once the period is 'issued'
Files are content-addressed and made read-only.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from docx_render import (MissingValue, RenderData, render, sha256_file,
                         verify_images_untouched, verify_preserved)
from pdf import docx_to_pdf
from validate import validate_template

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE patients(id INTEGER PRIMARY KEY, first_name TEXT NOT NULL, last_name TEXT NOT NULL,
                      father_name TEXT NOT NULL, amka TEXT NOT NULL);
CREATE TABLE template_versions(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, version_no INTEGER NOT NULL,
  sha256 TEXT NOT NULL, path TEXT NOT NULL, configuration_key TEXT NOT NULL, field_map TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 0, UNIQUE(name, version_no));
CREATE TABLE periods(id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL REFERENCES patients(id),
  start_date TEXT NOT NULL, end_date TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','issued')));
CREATE TABLE period_therapies(id INTEGER PRIMARY KEY, period_id INTEGER NOT NULL REFERENCES periods(id),
  therapy_id TEXT NOT NULL, therapist_id TEXT NOT NULL,
  home_visit INTEGER CHECK(home_visit IN (0,1)),          -- NULL = not chosen yet
  UNIQUE(period_id, therapy_id));
CREATE TABLE documents(id INTEGER PRIMARY KEY, period_id INTEGER NOT NULL REFERENCES periods(id),
  version INTEGER NOT NULL, template_version_id INTEGER NOT NULL REFERENCES template_versions(id),
  template_sha256 TEXT NOT NULL, issue_date TEXT NOT NULL,
  docx_path TEXT NOT NULL, docx_sha256 TEXT NOT NULL, pdf_path TEXT NOT NULL, pdf_sha256 TEXT NOT NULL,
  values_snapshot TEXT NOT NULL, UNIQUE(period_id, version));

CREATE TRIGGER tv_immutable BEFORE UPDATE OF name, version_no, sha256, path, configuration_key, field_map
  ON template_versions BEGIN SELECT RAISE(ABORT, 'template versions are immutable'); END;
CREATE TRIGGER tv_no_delete BEFORE DELETE ON template_versions
  BEGIN SELECT RAISE(ABORT, 'template versions cannot be deleted'); END;
CREATE TRIGGER doc_no_update BEFORE UPDATE ON documents
  BEGIN SELECT RAISE(ABORT, 'issued documents are immutable'); END;
CREATE TRIGGER doc_no_delete BEFORE DELETE ON documents
  BEGIN SELECT RAISE(ABORT, 'issued documents cannot be deleted'); END;
CREATE TRIGGER pt_locked_update BEFORE UPDATE ON period_therapies
  WHEN (SELECT status FROM periods WHERE id = OLD.period_id) = 'issued'
  BEGIN SELECT RAISE(ABORT, 'period is issued; reopen it to edit'); END;
CREATE TRIGGER pt_locked_delete BEFORE DELETE ON period_therapies
  WHEN (SELECT status FROM periods WHERE id = OLD.period_id) = 'issued'
  BEGIN SELECT RAISE(ABORT, 'period is issued; reopen it to edit'); END;
"""


def connect(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init_db(db: Path) -> sqlite3.Connection:
    con = connect(db)
    con.executescript(SCHEMA)
    return con


def _readonly(path: Path) -> None:
    os.chmod(path, stat.S_IREAD)


def _store_file(src: Path, dest: Path) -> Path:
    """Copy into the store once; existing content-addressed files are never overwritten."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        shutil.copyfile(src, dest)
        _readonly(dest)
    return dest


# ---------------------------------------------------------------- templates
class ActivationRefused(Exception):
    pass


def register_template(con, storage: Path, name: str, upload: Path, field_map: dict) -> int:
    """Store an uploaded file as a new immutable version (inactive until validated+activated).
    The uploaded file itself is only read."""
    sha = sha256_file(upload)
    fm = {**field_map, "sha256": sha}
    stored = _store_file(upload, storage / "templates" / f"{sha}.docx")
    ver = (con.execute("SELECT COALESCE(MAX(version_no),0)+1 FROM template_versions WHERE name=?", (name,))
           .fetchone()[0])
    cur = con.execute(
        "INSERT INTO template_versions(name,version_no,sha256,path,configuration_key,field_map) VALUES(?,?,?,?,?,?)",
        (name, ver, sha, str(stored), json.dumps(sorted(map(list, fm["configuration_key"]))),
         json.dumps(fm, ensure_ascii=False)))
    con.commit()
    return cur.lastrowid


def activate_template(con, template_version_id: int) -> None:
    row = con.execute("SELECT * FROM template_versions WHERE id=?", (template_version_id,)).fetchone()
    report = validate_template(Path(row["path"]), json.loads(row["field_map"]))
    if not report.ok:
        raise ActivationRefused("; ".join(report.errors[:5]) + (" …" if len(report.errors) > 5 else ""))
    clash = con.execute(
        "SELECT name, version_no FROM template_versions WHERE active=1 AND configuration_key=? AND id<>? "
        "AND name<>?", (row["configuration_key"], row["id"], row["name"])).fetchone()
    if clash:
        raise ActivationRefused(f"Another active template ({clash['name']} v{clash['version_no']}) "
                                "already covers this therapy/therapist combination")
    # replacing a template: the previous version of the SAME template is deactivated, never deleted
    con.execute("UPDATE template_versions SET active=0 WHERE name=? AND id<>?", (row["name"], row["id"]))
    con.execute("UPDATE template_versions SET active=1 WHERE id=?", (row["id"],))
    con.commit()


# ---------------------------------------------------------------- periods
def set_home_visit(con, period_id: int, therapy_id: str, value: bool) -> None:
    con.execute("UPDATE period_therapies SET home_visit=? WHERE period_id=? AND therapy_id=?",
                (int(value), period_id, therapy_id))
    con.commit()


def reopen_period(con, period_id: int) -> None:
    """Allow edits again. Existing issued documents are untouched; the next issue is version+1."""
    con.execute("UPDATE periods SET status='draft' WHERE id=?", (period_id,))
    con.commit()


# ---------------------------------------------------------------- issue
@dataclass
class Issued:
    document_id: int
    version: int
    docx: Path
    pdf: Path


def issue_document(con, storage: Path, period_id: int, template_version_id: int,
                   sessions: dict[str, list[date]], centre: dict, issue_date: date | None = None) -> Issued:
    period = con.execute("SELECT * FROM periods WHERE id=?", (period_id,)).fetchone()
    if period["status"] == "issued":
        raise ActivationRefused("Period already issued; reopen it to create a new version")
    patient = con.execute("SELECT * FROM patients WHERE id=?", (period["patient_id"],)).fetchone()
    tv = con.execute("SELECT * FROM template_versions WHERE id=?", (template_version_id,)).fetchone()
    fm = json.loads(tv["field_map"])
    if sha256_file(Path(tv["path"])) != tv["sha256"]:
        raise RuntimeError("Stored template file no longer matches its recorded hash")

    pts = con.execute("SELECT * FROM period_therapies WHERE period_id=?", (period_id,)).fetchall()
    home = {r["therapy_id"]: bool(r["home_visit"]) for r in pts if r["home_visit"] is not None}
    issue = issue_date or date.today()   # default: the day the document is generated
    data = RenderData(
        first_name=patient["first_name"], last_name=patient["last_name"], father_name=patient["father_name"],
        amka=patient["amka"], period_start=date.fromisoformat(period["start_date"]),
        period_end=date.fromisoformat(period["end_date"]), issue_date=issue, sessions=sessions,
        ada=centre.get("ada"), representative=centre.get("representative"), home_visit=home)

    version = con.execute("SELECT COALESCE(MAX(version),0)+1 FROM documents WHERE period_id=?",
                          (period_id,)).fetchone()[0]
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        docx = tmp / f"doc-{period_id}-v{version}.docx"
        res = render(Path(tv["path"]), fm, data, docx)          # MissingValue if ΝΑΙ/ΟΧΙ not chosen
        problems = verify_preserved(Path(tv["path"]), docx, res.changes) + \
            verify_images_untouched(Path(tv["path"]), docx)
        if problems:
            raise RuntimeError(f"Generated document failed integrity checks: {problems}")
        pdf = docx_to_pdf(docx, tmp)
        d_docx = _store_file(docx, storage / "documents" / docx.name)
        d_pdf = _store_file(pdf, storage / "documents" / pdf.name)

    snapshot = {
        "patient": {"first": patient["first_name"], "last": patient["last_name"],
                    "father": patient["father_name"], "amka": patient["amka"]},
        "period": [period["start_date"], period["end_date"]], "issue_date": issue.isoformat(),
        "sessions": {t: [d.isoformat() for d in ds] for t, ds in sessions.items()},
        "home_visit": home, "centre": centre,
        "template": {"name": tv["name"], "version": tv["version_no"], "sha256": tv["sha256"]},
        "changes": [[c.field, c.old, c.new] for c in res.changes],
    }
    cur = con.execute(
        "INSERT INTO documents(period_id,version,template_version_id,template_sha256,issue_date,docx_path,"
        "docx_sha256,pdf_path,pdf_sha256,values_snapshot) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (period_id, version, tv["id"], tv["sha256"], issue.isoformat(), str(d_docx), sha256_file(d_docx),
         str(d_pdf), sha256_file(d_pdf), json.dumps(snapshot, ensure_ascii=False)))
    con.execute("UPDATE periods SET status='issued' WHERE id=?", (period_id,))
    con.commit()
    return Issued(cur.lastrowid, version, d_docx, d_pdf)


def verify_document_integrity(con, document_id: int) -> list[str]:
    """Re-hash stored files; used for audits and tests."""
    d = con.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
    tv = con.execute("SELECT * FROM template_versions WHERE id=?", (d["template_version_id"],)).fetchone()
    bad = []
    for label, path, sha in (("docx", d["docx_path"], d["docx_sha256"]), ("pdf", d["pdf_path"], d["pdf_sha256"]),
                             ("template", tv["path"], d["template_sha256"])):
        if not Path(path).is_file() or sha256_file(Path(path)) != sha:
            bad.append(f"{label} file missing or changed: {path}")
    return bad
