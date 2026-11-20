"""Tests for the document pipeline spike. Run:  ..\\.venv\\Scripts\\python -m pytest -q"""
from __future__ import annotations

import copy
import json
import shutil
import sqlite3
import zipfile
from datetime import date
from pathlib import Path

import pytest
from docx import Document
from docx.oxml.ns import qn

import store
from docx_render import (MissingValue, RenderData, TemplateMismatch, render, sha256_file,
                         verify_images_untouched, verify_preserved)
from pdf import find_soffice
from validate import validate_template

ROOT = Path(__file__).resolve().parent
TPL = ROOT / "templates"
ORIGINAL = ROOT.parent / "templates" / "eopyy-template.docx"
ORIGINAL_SHA = "e4600bbb0eb0d34a07d422eb830dddc2c3644816daa78570ac1e790d7e6ebf55"

try:
    find_soffice()
    HAVE_LO = True
except FileNotFoundError:
    HAVE_LO = False
needs_lo = pytest.mark.skipif(not HAVE_LO, reason="LibreOffice not installed")


def fm(name: str) -> dict:
    return json.loads((TPL / f"{name}.map.json").read_text(encoding="utf-8"))


def data(therapies, home=None, **kw) -> RenderData:
    return RenderData(
        first_name="ΓΕΩΡΓΙΟΣ", last_name="ΔΟΚΙΜΑΣΤΙΚΟΣ", father_name="ΝΙΚΟΛΑΟΥ", amka="12345678901",
        period_start=date(2026, 10, 1), period_end=date(2026, 10, 31), issue_date=date(2026, 11, 19),
        sessions={t: [date(2026, 10, 5), date(2026, 10, 12)] for t in therapies},
        ada="ΤΕΣΤ-ΑΔΑ", representative="Δοκιμαστική Εκπρόσωπος", home_visit=home or {}, **kw)


def box_texts(docx: Path, table: int = 1) -> tuple[str, str]:
    d = Document(str(docx))
    runs = d.tables[table].rows[0].cells[0].paragraphs[2].runs
    return runs[2].text, runs[4].text


# ------------------------------------------------------------- home visit
@pytest.mark.parametrize("value, expected", [(True, ("☒", "☐")), (False, ("☐", "☒"))])
def test_home_visit_marks_exactly_one_box(tmp_path, value, expected):
    out = tmp_path / "o.docx"
    res = render(TPL / "synthetic-C.docx", fm("synthetic-C"), data(["speech", "physiotherapy"], {"physiotherapy": value}), out)
    assert box_texts(out) == expected
    assert verify_preserved(TPL / "synthetic-C.docx", out, res.changes) == []


def test_home_visit_keeps_run_formatting(tmp_path):
    out = tmp_path / "o.docx"
    render(TPL / "synthetic-C.docx", fm("synthetic-C"), data(["speech", "physiotherapy"], {"physiotherapy": True}), out)
    before = Document(str(TPL / "synthetic-C.docx")).tables[1].rows[0].cells[0].paragraphs[2]
    after = Document(str(out)).tables[1].rows[0].cells[0].paragraphs[2]
    for rb, ra in zip(before.runs, after.runs):
        pb, pa = rb._r.find(qn("w:rPr")), ra._r.find(qn("w:rPr"))
        if ra.text == "☒":  # only the glyph font is pinned; size and everything else identical
            for el in (pb, pa):
                el.remove(el.find(qn("w:rFonts")))
        assert pb.xml == pa.xml
    assert before._p.find(qn("w:pPr")).xml == after._p.find(qn("w:pPr")).xml


def test_home_visit_must_be_chosen(tmp_path):
    with pytest.raises(MissingValue):
        render(TPL / "synthetic-C.docx", fm("synthetic-C"), data(["speech", "physiotherapy"]), tmp_path / "o.docx")


def test_home_visit_refuses_prechecked_template(tmp_path):
    bad = tmp_path / "bad.docx"
    d = Document(str(TPL / "synthetic-C.docx"))
    d.tables[1].rows[0].cells[0].paragraphs[2].runs[2]._r.find(qn("w:t")).text = "☒"
    d.save(str(bad))
    m = {**fm("synthetic-C"), "sha256": None}
    assert any("both boxes empty" in e for e in validate_template(bad, m).errors)
    with pytest.raises(TemplateMismatch):
        render(bad, m, data(["speech", "physiotherapy"], {"physiotherapy": False}), tmp_path / "o.docx")


# ------------------------------------------------------------- template validation
@pytest.mark.parametrize("name", ["synthetic-A", "synthetic-B", "synthetic-C"])
def test_good_templates_validate(name):
    rep = validate_template(TPL / f"{name}.docx", fm(name))
    assert rep.ok, rep.errors


def test_template_with_blank_therapist_details_is_rejected():
    rep = validate_template(TPL / "real-5.docx", fm("real-5"))
    assert not rep.ok
    assert any("therapist detail 'license_no' is empty" in e for e in rep.errors)


def mutate(name, fn):
    m = copy.deepcopy(fm(name))
    fn(m)
    m["sha256"] = None
    return m


@pytest.mark.parametrize("label, fn, needle", [
    ("period unmapped", lambda m: m["fields"].pop("period"), "'period' is not mapped"),
    ("issue unmapped", lambda m: m["fields"].pop("issue_date"), "'issue_date' is not mapped"),
    ("bad anchor", lambda m: m["fields"]["patient"].update(between=["ΔΕΝ ΥΠΑΡΧΕΙ ", " x"]), "anchor text"),
    ("bad locator", lambda m: m["fields"]["period"]["loc"].update(r=99), "cannot be located"),
    ("wrong label", lambda m: m["blocks"][0].update(label="Εργοθεραπεία:"), "heading"),
    ("details unmapped", lambda m: m["blocks"][0]["therapist_details"].pop("authority"), "'authority' is not mapped"),
    ("block dropped", lambda m: m["blocks"].pop(), "tables but"),
    ("key mismatch", lambda m: m.update(configuration_key=[["speech", "T9"]]), "configuration_key"),
    ("home visit unmapped", lambda m: m["blocks"][1].pop("home_visit"), "not mapped"),
])
def test_broken_mapping_is_rejected(label, fn, needle):
    rep = validate_template(TPL / "synthetic-C.docx", mutate("synthetic-C", fn))
    assert not rep.ok and any(needle in e for e in rep.errors), (label, rep.errors)


def test_changed_template_file_is_rejected(tmp_path):
    changed = tmp_path / "c.docx"
    shutil.copy(TPL / "synthetic-C.docx", changed)
    d = Document(str(changed))
    d.paragraphs[2].runs[0]._r.find(qn("w:t")).text = "X"
    d.save(str(changed))
    assert any("SHA-256" in e for e in validate_template(changed, fm("synthetic-C")).errors)
    with pytest.raises(TemplateMismatch):
        render(changed, fm("synthetic-C"), data(["speech", "physiotherapy"], {"physiotherapy": True}), tmp_path / "o.docx")


def test_unmapped_date_text_is_warned(tmp_path):
    tpl = tmp_path / "w.docx"
    d = Document(str(TPL / "synthetic-C.docx"))
    d.sections[0].footer.add_paragraph("Ισχύει από 01/01/2026")
    d.save(str(tpl))
    rep = validate_template(tpl, {**fm("synthetic-C"), "sha256": None})
    assert rep.ok
    assert any("Unmapped date-like text" in w and "01/01/2026" in w for w in rep.warnings)


# ------------------------------------------------------------- images
def test_images_untouched_by_render(tmp_path):
    out = tmp_path / "o.docx"
    render(TPL / "synthetic-C.docx", fm("synthetic-C"), data(["speech", "physiotherapy"], {"physiotherapy": True}), out)
    assert verify_images_untouched(TPL / "synthetic-C.docx", out) == []
    with zipfile.ZipFile(TPL / "synthetic-C.docx") as a, zipfile.ZipFile(out) as b:
        media = [n for n in a.namelist() if n.startswith("word/media/")]
        assert len(media) == 5 and any(n.endswith(".jpg") for n in media)  # incl. JPEG header logo
        assert all(a.read(n) == b.read(n) for n in media)


def _rewrite(src: Path, dst: Path, name: str, fn) -> None:
    with zipfile.ZipFile(src) as zi, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zo:
        for item in zi.infolist():
            blob = zi.read(item.filename)
            zo.writestr(item, fn(blob) if item.filename == name else blob)


def test_image_checks_detect_tampering(tmp_path):
    out = tmp_path / "o.docx"
    render(TPL / "synthetic-C.docx", fm("synthetic-C"), data(["speech", "physiotherapy"], {"physiotherapy": True}), out)
    # resized image
    resized = tmp_path / "resized.docx"
    _rewrite(out, resized, "word/document.xml", lambda b: b.replace(b'<wp:extent cx="', b'<wp:extent cx="1', 1))
    assert verify_images_untouched(TPL / "synthetic-C.docx", resized)
    # replaced media bytes
    swapped = tmp_path / "swapped.docx"
    _rewrite(out, swapped, "word/media/image1.png", lambda b: b + b"\x00")
    assert verify_images_untouched(TPL / "synthetic-C.docx", swapped)
    # repositioned image
    moved = tmp_path / "moved.docx"
    _rewrite(out, moved, "word/document.xml", lambda b: b.replace(b"<wp:posOffset>", b"<wp:posOffset>1", 1))
    assert verify_images_untouched(TPL / "synthetic-C.docx", moved)


def test_watermark_removed_from_output_not_template(tmp_path):
    out = tmp_path / "o.docx"
    render(TPL / "synthetic-C.docx", fm("synthetic-C"), data(["speech", "physiotherapy"], {"physiotherapy": True}), out)
    with zipfile.ZipFile(TPL / "synthetic-C.docx") as t, zipfile.ZipFile(out) as o:
        assert b"PowerPlusWaterMarkObject" in t.read("word/header1.xml")
        assert b"PowerPlusWaterMarkObject" not in o.read("word/header1.xml")


# ------------------------------------------------------------- store / safety
@pytest.fixture()
def env(tmp_path):
    storage = tmp_path / "storage"
    con = store.init_db(tmp_path / "t.db")
    con.execute("INSERT INTO patients VALUES(1,'ΓΕΩΡΓΙΟΣ','ΔΟΚΙΜΑΣΤΙΚΟΣ','ΝΙΚΟΛΑΟΥ','12345678901')")
    con.execute("INSERT INTO periods(id,patient_id,start_date,end_date) VALUES(1,1,'2026-10-01','2026-10-31')")
    con.executemany("INSERT INTO period_therapies(period_id,therapy_id,therapist_id) VALUES(1,?,?)",
                    [("speech", "T1"), ("physiotherapy", "T2")])
    con.commit()
    return con, storage, tmp_path


def test_activation_rules(env):
    con, storage, tmp = env
    bad = store.register_template(con, storage, "real", TPL / "real-5.docx", fm("real-5"))
    with pytest.raises(store.ActivationRefused):
        store.activate_template(con, bad)                       # blank therapist details
    good = store.register_template(con, storage, "C", TPL / "synthetic-C.docx", fm("synthetic-C"))
    store.activate_template(con, good)
    twin = store.register_template(con, storage, "C-twin", TPL / "synthetic-C.docx", fm("synthetic-C"))
    with pytest.raises(store.ActivationRefused, match="already covers"):
        store.activate_template(con, twin)                      # duplicate configuration_key


def test_registered_file_is_readonly_and_original_untouched(env):
    con, storage, tmp = env
    before = sha256_file(TPL / "synthetic-C.docx")
    tv = store.register_template(con, storage, "C", TPL / "synthetic-C.docx", fm("synthetic-C"))
    path = Path(con.execute("SELECT path FROM template_versions WHERE id=?", (tv,)).fetchone()[0])
    assert sha256_file(path) == before == sha256_file(TPL / "synthetic-C.docx")
    with pytest.raises(PermissionError):
        path.open("ab")
    assert sha256_file(ORIGINAL) == ORIGINAL_SHA


def test_db_blocks_template_edits_and_deletes(env):
    con, storage, tmp = env
    tv = store.register_template(con, storage, "C", TPL / "synthetic-C.docx", fm("synthetic-C"))
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        con.execute("UPDATE template_versions SET sha256='x' WHERE id=?", (tv,))
    with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
        con.execute("DELETE FROM template_versions WHERE id=?", (tv,))


@needs_lo
def test_issue_edit_reissue_and_template_replacement(env):
    con, storage, tmp = env
    centre = {"ada": "ΤΕΣΤ-ΑΔΑ", "representative": "Δοκιμαστική Εκπρόσωπος"}
    sessions = {t: [date(2026, 10, 5), date(2026, 10, 12)] for t in ("speech", "physiotherapy")}
    t1 = store.register_template(con, storage, "C", TPL / "synthetic-C.docx", fm("synthetic-C"))
    store.activate_template(con, t1)

    # ΝΑΙ/ΟΧΙ not chosen yet -> refuse to generate, nothing issued
    with pytest.raises(MissingValue):
        store.issue_document(con, storage, 1, t1, sessions, centre)
    assert con.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0

    # choose ΝΑΙ, issue v1
    store.set_home_visit(con, 1, "physiotherapy", True)
    v1 = store.issue_document(con, storage, 1, t1, sessions, centre)
    assert box_texts(v1.docx) == ("☒", "☐")

    # after issue the value is locked at DB level
    with pytest.raises(sqlite3.IntegrityError, match="issued"):
        store.set_home_visit(con, 1, "physiotherapy", False)
    # issued documents can't be edited or deleted
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE documents SET issue_date='2000-01-01' WHERE id=?", (v1.document_id,))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("DELETE FROM documents WHERE id=?", (v1.document_id,))
    with pytest.raises(store.ActivationRefused):
        store.issue_document(con, storage, 1, t1, sessions, centre)   # not twice without reopen

    # replace the template (new version) and re-issue with ΟΧΙ
    new_file = tmp / "C-v2.docx"
    d = Document(str(TPL / "synthetic-C.docx"))
    d.paragraphs[2].runs[0]._r.find(qn("w:t")).text += " (v2)"
    d.save(str(new_file))
    t2 = store.register_template(con, storage, "C", new_file, fm("synthetic-C"))
    store.activate_template(con, t2)                            # deactivates v1, keeps it
    store.reopen_period(con, 1)
    store.set_home_visit(con, 1, "physiotherapy", False)
    v2 = store.issue_document(con, storage, 1, t2, sessions, centre)
    assert (v1.version, v2.version) == (1, 2)
    assert box_texts(v2.docx) == ("☐", "☒")

    # v1 is unchanged and still tied to its exact template version
    assert store.verify_document_integrity(con, v1.document_id) == []
    assert store.verify_document_integrity(con, v2.document_id) == []
    assert box_texts(v1.docx) == ("☒", "☐")
    rows = con.execute("SELECT id, template_version_id FROM documents ORDER BY version").fetchall()
    assert [r["template_version_id"] for r in rows] == [t1, t2]
    snap1 = json.loads(con.execute("SELECT values_snapshot FROM documents WHERE id=?", (v1.document_id,)).fetchone()[0])
    assert snap1["home_visit"] == {"physiotherapy": True}
    assert con.execute("SELECT active FROM template_versions WHERE id=?", (t1,)).fetchone()[0] == 0
