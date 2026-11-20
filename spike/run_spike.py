"""Run the DOCX -> PDF validation and write PDFs, images and a report to spike/out/."""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import date
from pathlib import Path

import pymupdf
from PIL import Image, ImageChops, ImageDraw

from docx_render import (RenderData, render, sha256_file, verify_images_untouched,
                         verify_preserved)
from matching import (AmbiguousTemplates, NoMatchingTemplate, TemplateEntry,
                      configuration_key, find_template)
from pdf import docx_to_pdf
from validate import validate_template

ROOT = Path(__file__).resolve().parent
TPL = ROOT / "templates"
OUT = ROOT / "review"
ORIGINAL = ROOT.parent / "templates" / "eopyy-template.docx"

FAKE = dict(first_name="ΓΕΩΡΓΙΟΣ", last_name="ΔΟΚΙΜΑΣΤΙΚΟΣ", father_name="ΝΙΚΟΛΑΟΥ", amka="12345678901")
PERIOD = (date(2026, 10, 1), date(2026, 10, 31))
ISSUE = date.today()
CENTRE = dict(ada="ΨΨ00-ΤΕΣΤ", representative="Δοκιμαστική Εκπρόσωπος Κέντρου")
SESSIONS = {
    "speech": [date(2026, 10, d) for d in (1, 5, 8, 12, 15, 19, 22, 26)],
    "occupational": [date(2026, 10, d) for d in (2, 6, 9, 13, 16, 20, 23, 27)],
    "psychotherapy": [date(2026, 10, d) for d in (1, 6, 8, 13, 15, 20, 22, 29)],
    "special_education": [date(2026, 10, d) for d in (2, 7, 9, 14, 16, 21, 23, 30)],
    "physiotherapy": [date(2026, 10, d) for d in (5, 7, 12, 14, 19, 21, 26, 29)],
}
TEMPLATES = ("real-5", "synthetic-A", "synthetic-B", "synthetic-C")


def load_maps() -> dict[str, dict]:
    return {t: json.loads((TPL / f"{t}.map.json").read_text(encoding="utf-8")) for t in TEMPLATES}


def pdf_text(pdf: Path) -> str:
    return " ".join(" ".join(p.get_text() for p in pymupdf.open(pdf)).split())


def box_glyphs(pdf: Path) -> list[tuple[str, tuple]]:
    """(char, bbox) of every checkbox glyph on page 1, in reading order."""
    out = []
    page = pymupdf.open(pdf)[0]
    for b in page.get_text("rawdict")["blocks"]:
        for ln in b.get("lines", []):
            for sp in ln["spans"]:
                for ch in sp["chars"]:
                    if ch["c"] in "☐☒":
                        out.append((ch["c"], tuple(round(v, 1) for v in ch["bbox"])))
    return sorted(out, key=lambda t: t[1][0])  # one line, left to right


def word_boxes(pdf: Path, words=("ΝΑΙ", "ΟΧΙ")) -> dict[str, tuple]:
    page = pymupdf.open(pdf)[0]
    return {r[4]: tuple(round(v, 1) for v in r[:4]) for r in page.get_text("words") if r[4] in words}


def image_facts(pdf: Path) -> tuple[list, set]:
    doc = pymupdf.open(pdf)
    page = doc[0]
    boxes = sorted(tuple(round(v, 1) for v in i["bbox"]) for i in page.get_image_info())
    hashes = {hashlib.sha256(doc.extract_image(x[0])["image"]).hexdigest() for x in page.get_images(full=True)}
    return boxes, hashes


def rasterize(pdf: Path, dest: Path, dpi=110) -> Path:
    pymupdf.open(pdf)[0].get_pixmap(dpi=dpi).save(dest)
    return dest


def compare_png(tpl_png: Path, gen_png: Path, dest: Path) -> None:
    a, b = Image.open(tpl_png).convert("RGB"), Image.open(gen_png).convert("RGB")
    d = ImageChops.difference(a, b).convert("L").point(lambda v: 255 if v > 40 else 0)
    ov = b.copy()
    ov.paste((255, 0, 0), mask=d)
    w, h = a.size
    sheet = Image.new("RGB", (w * 3 + 40, h + 30), "white")
    dr = ImageDraw.Draw(sheet)
    for i, (img, title) in enumerate(((a, "TEMPLATE"), (b, "GENERATED"), (ov, "DIFF (red = changed pixels)"))):
        sheet.paste(img, (i * (w + 20), 28))
        dr.text((i * (w + 20) + 5, 8), title, fill="black")
    sheet.save(dest)


def home_visit_strip(pdfs: dict[str, Path], dest: Path) -> None:
    """Stack a close-up of the home-visit line from the template and each generated variant."""
    rows = []
    tmp = dest.parent / "_tmp.png"
    for title, pdf in pdfs.items():
        glyphs = box_glyphs(pdf)
        y0 = min(g[1][1] for g in glyphs) - 6
        y1 = max(g[1][3] for g in glyphs) + 6
        pymupdf.open(pdf)[0].get_pixmap(dpi=220, clip=pymupdf.Rect(60, y0, 420, y1)).save(tmp)
        rows.append((title, Image.open(tmp).convert("RGB")))
    w = max(r[1].width for r in rows) + 170
    h = sum(r[1].height + 14 for r in rows)
    sheet = Image.new("RGB", (w, h), "white")
    dr, y = ImageDraw.Draw(sheet), 0
    for title, img in rows:
        dr.text((8, y + img.height // 2 - 5), title, fill="black")
        sheet.paste(img, (160, y))
        y += img.height + 14
    sheet.save(dest)
    tmp.unlink()


def main() -> int:
    (OUT / "template-pdf").mkdir(parents=True, exist_ok=True)
    for old in list(OUT.glob("*.*")) + list((OUT / "template-pdf").glob("*.*")):  # best effort: skip locked files
        try:
            old.unlink()
        except OSError:
            pass
    lines: list[str] = []
    ok = True

    def check(cond: bool, msg: str) -> None:
        nonlocal ok
        ok &= bool(cond)
        lines.append(f"- [{'PASS' if cond else 'FAIL'}] {msg}")

    orig_sha = sha256_file(ORIGINAL)
    maps = load_maps()
    shas = {t: sha256_file(TPL / f"{t}.docx") for t in TEMPLATES}

    lines.append("## Template validation (activation gate)")
    for t in TEMPLATES:
        rep = validate_template(TPL / f"{t}.docx", maps[t])
        if t == "real-5":
            expected = (not rep.ok) and all("empty in the template" in e for e in rep.errors)
            check(expected, f"{t}: REJECTED, as expected. {len(rep.errors)} errors, all 'therapist detail is empty' "
                            "(your supplied form leaves 4 of 5 therapist sections blank). It is still rendered below "
                            "for the layout test, but could not be activated.")
        else:
            check(rep.ok, f"{t}: valid (every mapped field found, therapist details present, dry-run render preserves "
                          "everything, images untouched)" + ("" if rep.ok else f": {rep.errors}"))
        for w in rep.warnings:
            lines.append(f"  - warning: {w}")

    lines.append("\n## Template selection")
    entries = [TemplateEntry(t, t, frozenset(map(tuple, m["configuration_key"]))) for t, m in maps.items()]
    for label, enr, expected in (
        ("psychotherapy(T1)", [("psychotherapy", "T1")], "synthetic-A"),
        ("speech(T1)+occupational(T2)+psychotherapy(T3)",
         [("speech", "T1"), ("occupational", "T2"), ("psychotherapy", "T3")], "synthetic-B"),
        ("speech(T1)+physiotherapy(T2)", [("speech", "T1"), ("physiotherapy", "T2")], "synthetic-C"),
        ("all five", [("speech", "T1"), ("occupational", "T2"), ("psychotherapy", "T3"),
                      ("special_education", "T1"), ("physiotherapy", "T4")], "real-5"),
    ):
        got = find_template(configuration_key(enr), entries).template_id
        check(got == expected, f"{label} -> {got}")
    try:
        find_template(configuration_key([("speech", "T1"), ("psychotherapy", "T1")]), entries)
        check(False, "unmatched configuration should raise")
    except NoMatchingTemplate as e:
        check(True, f"no match reported, not guessed: \"{e}\"")
    try:
        find_template(entries[1].key, entries + [TemplateEntry("dup", "copy", entries[1].key)])
        check(False, "duplicate should raise")
    except AmbiguousTemplates as e:
        check(True, f"ambiguity reported: \"{e}\"")

    for tid in TEMPLATES:
        fm = maps[tid]
        template = TPL / f"{tid}.docx"
        therapies = [b["therapy"] for b in fm["blocks"]]
        has_hv = any("home_visit" in b for b in fm["blocks"])
        variants = [("YES", True), ("NO", False)] if has_hv else [("", None)]
        base_pdf = docx_to_pdf(template, OUT / "template-pdf")
        base_png = rasterize(base_pdf, OUT / f"{tid}-template.png")
        hv_pdfs = {"template (empty)": base_pdf} if has_hv else {}

        for vname, hv in variants:
            tag = tid + (f"-home-{vname}" if vname else "")
            lines.append(f"\n## {tag}")
            data = RenderData(**FAKE, period_start=PERIOD[0], period_end=PERIOD[1], issue_date=ISSUE, **CENTRE,
                              sessions={t: SESSIONS[t] for t in therapies},
                              home_visit={"physiotherapy": hv} if has_hv else {})
            out_docx = OUT / f"{tag}-generated.docx"
            res = render(template, fm, data, out_docx)
            check(sha256_file(template) == shas[tid] == fm["sha256"], "template file unchanged after render")
            problems = verify_preserved(template, out_docx, res.changes)
            check(not problems, "DOCX identical to the template outside the mapped runs" + (f": {problems}" if problems else ""))
            ip = verify_images_untouched(template, out_docx)
            check(not ip, "DOCX images byte-identical; size/position/crop/rotation XML identical" + (f": {ip}" if ip else ""))

            gen_pdf = docx_to_pdf(out_docx, OUT)
            gen_png = rasterize(gen_pdf, OUT / f"{tag}-generated.png")
            compare_png(base_png, gen_png, OUT / f"{tag}-comparison.png")
            gt = pdf_text(gen_pdf)
            check(len(pymupdf.open(gen_pdf)) == len(pymupdf.open(base_pdf)) == 1, "single page, same as the template")

            bb, bh = image_facts(base_pdf)
            gb, gh = image_facts(gen_pdf)
            check(bb == gb and bh == gh, f"PDF: {len(gb)} image(s), same pixel data and same on-page position/size as the template")
            patient = f"{FAKE['first_name']} {FAKE['last_name']} του {FAKE['father_name']} με ΑΜΚΑ {FAKE['amka']}"
            check(patient in gt and "ΠΛΑΣΤΟ" not in gt, "patient name / father's name / AMKA inserted")
            check("1/10/2026 έως 31/10/2026" in gt, "period dates")
            check(f"{ISSUE.day}/{ISSUE.month:02d}/{ISSUE.year}" in gt, "issue date = generation date")
            check(all(f"{SESSIONS[t][0].day}/10/26" in gt and f"{SESSIONS[t][-1].day}/10/26" in gt for t in therapies),
                  "session dates for every therapy")
            check("ΑΔΑ: " + CENTRE["ada"] in gt and CENTRE["representative"] in gt, "ΑΔΑ and representative")
            check("3/9/26" not in gt and "2/09/2026" not in gt, "old sample dates gone")
            check("ΟΧΙ ΓΙΑ ΚΑΤΑΘΕΣΗ" not in gt and "ΔΕΙΓΜΑ" not in gt, "watermark text absent")

            if has_hv:
                g = box_glyphs(gen_pdf)
                want = ["☒", "☐"] if hv else ["☐", "☒"]
                check([c for c, _ in g] == want,
                      f"home visit {'ΝΑΙ' if hv else 'ΟΧΙ'}: PDF boxes (left=ΝΑΙ, right=ΟΧΙ) = {[c for c, _ in g]}, expected {want}")
                tb = box_glyphs(base_pdf)
                wb, wg = word_boxes(base_pdf), word_boxes(gen_pdf)
                # LibreOffice renders ☒ with a fallback font (OpenSymbol) that is 1.2 pt narrower and sits
                # 1.1 pt lower than the template's ☐ (MS Gothic fallback). The run formatting is NOT changed.
                dev = {k: (round(wg[k][0] - wb[k][0], 1), round(wg[k][1] - wb[k][1], 1)) for k in wb}
                bdev = [(round(g_[1][0] - t_[1][0], 1), round(g_[1][1] - t_[1][1], 1)) for t_, g_ in zip(tb, g)]
                check(len(wb) == 2 and all(abs(dy) <= 0.1 for _, dy in dev.values()),
                      f"ΝΑΙ / ΟΧΙ labels stay on the same line (vertical deviation 0); horizontal deviation (dx, dy) in pt: {dev}")
                check(all(abs(dx) <= 1.5 for dx, _ in dev.values()),
                      "horizontal label deviation <= 1.5 pt (known: ΟΧΙ moves 1.2 pt left when ΝΑΙ is ticked)")
                check(all(abs(bx) <= 1.5 and abs(by) <= 1.5 for bx, by in bdev) and len(tb) == len(g),
                      f"checkbox glyph deviation from the template (dx, dy) in pt: {bdev}")
                hv_pdfs["YES selected" if hv else "NO selected"] = gen_pdf
            lines.append("  - changes applied:")
            for c in res.changes:
                lines.append(f"    - {c.field}: `{c.old.strip()[:50]}` -> `{c.new.strip()[:50]}`")
        if has_hv:
            home_visit_strip(hv_pdfs, OUT / f"{tid}-home-visit-closeup.png")

    lines.append("\n## Originals")
    check(sha256_file(ORIGINAL) == orig_sha, f"templates/eopyy-template.docx unchanged (sha256 {orig_sha[:16]}…)")
    check(all(sha256_file(TPL / f"{t}.docx") == shas[t] for t in TEMPLATES), "all spike templates unchanged")
    report = "# Validation report\n\n" + "\n".join(lines) + f"\n\nOVERALL: {'PASS' if ok else 'FAIL'}\n"
    (OUT / "report.md").write_text(report, encoding="utf-8")
    print(report)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
