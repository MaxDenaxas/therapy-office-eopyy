"""Build spike templates from the real EOPYY template.

Outputs (spike/templates/):
  real-5.docx          real template with only the patient name/AMKA replaced by fake data
  synthetic-A.docx     Psychotherapy (T1) with inline signature + floating stamp + logo
  synthetic-B.docx     Speech (T1) + Occupational (T2) + Psychotherapy (T3), same extras
  <name>.map.json      manual-style field maps (explicit indices + anchor text)

The original templates/eopyy-template.docx is only ever read.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from docx import Document
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls, qn
from docx.shared import Mm
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
SRC = ROOT.parent / "templates" / "eopyy-template.docx"
OUT = ROOT / "templates"
ASSETS = ROOT / "assets"

PLACEHOLDER_PATIENT = "ΠΛΑΣΤΟ ΟΝΟΜΑ του ΠΛΑΣΤΟ με ΑΜΚΑ 00000000000"

LABEL_TO_THERAPY = {
    "Λογοθεραπεία:": "speech",
    "Εργοθεραπεία:": "occupational",
    "Ψυχοθεραπεία:": "psychotherapy",
    "Ειδική διαπαιδαγώγηση:": "special_education",
    "Φυσικοθεραπεία:": "physiotherapy",
}


# ------------------------------------------------------------------ images
def _font(size: int) -> ImageFont.FreeTypeFont:
    for name in ("arial.ttf", "segoeui.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def make_assets() -> dict[str, Path]:
    ASSETS.mkdir(exist_ok=True)
    paths = {}
    # logo (opaque)
    logo = Image.new("RGB", (300, 120), (30, 90, 160))
    d = ImageDraw.Draw(logo)
    d.text((20, 40), "LOGO ΚΕΝΤΡΟΥ", fill="white", font=_font(32))
    paths["logo"] = ASSETS / "logo.png"
    logo.save(paths["logo"])
    paths["logo_jpg"] = ASSETS / "logo.jpg"
    logo.save(paths["logo_jpg"], quality=90)
    for tag, color in (("T1", (160, 20, 20, 235)), ("T2", (20, 110, 40, 235)), ("T3", (80, 30, 140, 235))):
        # stamp: transparent ring with text
        st = Image.new("RGBA", (360, 360), (0, 0, 0, 0))
        d = ImageDraw.Draw(st)
        d.ellipse((8, 8, 352, 352), outline=color, width=10)
        d.ellipse((50, 50, 310, 310), outline=color, width=4)
        d.text((95, 120), "ΣΦΡΑΓΙΔΑ", fill=color, font=_font(44))
        d.text((120, 185), f"ΘΕΡ. {tag}", fill=color, font=_font(40))
        paths[f"stamp{tag}"] = ASSETS / f"stamp{tag}.png"
        st.save(paths[f"stamp{tag}"])
        # signature: transparent squiggle
        sg = Image.new("RGBA", (420, 150), (0, 0, 0, 0))
        d = ImageDraw.Draw(sg)
        pts = [(10, 100), (60, 30), (100, 120), (150, 20), (200, 110), (260, 40), (310, 100), (410, 60)]
        d.line(pts, fill=(10, 30, 120, 255), width=6, joint="curve")
        paths[f"sig{tag}"] = ASSETS / f"sig{tag}.png"
        sg.save(paths[f"sig{tag}"])
    return paths


# ------------------------------------------------------------------ docx helpers
def set_t(run, text: str) -> None:
    t = run._r.find(qn("w:t"))
    t.text = text
    t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")


def remove_block(doc, table) -> None:
    tbl = table._tbl
    nxt = tbl.getnext()
    tbl.getparent().remove(tbl)
    if nxt is not None and nxt.tag == qn("w:p") and not "".join(nxt.itertext()).strip():
        nxt.getparent().remove(nxt)


def add_floating_picture(paragraph, doc, img: Path, w_mm: float, h_mm: float, x_mm: float, y_mm: float, pid: int,
                         behind: bool = False, rot: int = 0, crop: bool = False):
    rid, _ = doc.part.get_or_add_image(str(img))
    cx, cy = int(Mm(w_mm)), int(Mm(h_mm))
    xml = f"""
<w:drawing {nsdecls('w', 'wp', 'a', 'pic', 'r')}>
 <wp:anchor distT="0" distB="0" distL="0" distR="0" simplePos="0" relativeHeight="251659264"
            behindDoc="{1 if behind else 0}" locked="0" layoutInCell="1" allowOverlap="1">
  <wp:simplePos x="0" y="0"/>
  <wp:positionH relativeFrom="column"><wp:posOffset>{int(Mm(x_mm))}</wp:posOffset></wp:positionH>
  <wp:positionV relativeFrom="paragraph"><wp:posOffset>{int(Mm(y_mm))}</wp:posOffset></wp:positionV>
  <wp:extent cx="{cx}" cy="{cy}"/>
  <wp:effectExtent l="0" t="0" r="0" b="0"/>
  <wp:wrapNone/>
  <wp:docPr id="{pid}" name="Stamp {pid}"/>
  <wp:cNvGraphicFramePr/>
  <a:graphic>
   <a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">
    <pic:pic>
     <pic:nvPicPr><pic:cNvPr id="{pid}" name="{img.name}"/><pic:cNvPicPr/></pic:nvPicPr>
     <pic:blipFill><a:blip r:embed="{rid}"/>{'<a:srcRect l="8000" t="8000" r="8000" b="8000"/>' if crop else ''}<a:stretch><a:fillRect/></a:stretch></pic:blipFill>
     <pic:spPr><a:xfrm rot="{rot}"><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>
      <a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>
    </pic:pic>
   </a:graphicData>
  </a:graphic>
 </wp:anchor>
</w:drawing>"""
    run = paragraph.add_run()
    run._r.append(parse_xml(xml))


def table_label(table) -> str:
    return table.rows[0].cells[0].paragraphs[0].runs[0]._r.find(qn("w:t")).text.strip()


# ------------------------------------------------------------------ transformations
def sanitize_patient(doc) -> None:
    for p in doc.paragraphs:
        if "Βεβαιώνεται" in p.text:
            run = p.runs[0]
            text = run._r.find(qn("w:t")).text
            new = re.sub(r"(ο/η ).*?( παρακολούθησε)", rf"\g<1>{PLACEHOLDER_PATIENT}\g<2>", text)
            assert new != text
            set_t(run, new)
            return
    raise RuntimeError("intro paragraph not found")


def build_field_map(docx_path: Path, assignments: dict[str, str], name: str) -> dict:
    """Emulates the one-time manual mapping: explicit indices + anchors."""
    doc = Document(str(docx_path))
    paras = doc.paragraphs
    intro = next(i for i, p in enumerate(paras) if "Βεβαιώνεται" in p.text)
    period_run = next(j for j, r in enumerate(paras[intro].runs) if "έως" in r.text)
    sig_idx = next(i for i, p in enumerate(paras) if p.text.strip() == "Ημερομηνία / Σφραγίδα / Υπογραφή")
    ada_idx = next(i for i, p in enumerate(paras) if p.text.startswith("ΑΔΑ:"))
    blocks = []
    for ti, table in enumerate(doc.tables):
        label = table_label(table)
        therapy = LABEL_TO_THERAPY[label]
        runs = table.rows[0].cells[0].paragraphs[0].runs
        base = {"table": ti, "row": 0, "cell": 0}
        block = {
            "therapy": therapy,
            "therapist": assignments[therapy],
            "label": label,
            "label_loc": {**base, "p": 0, "r": 0},
            "dates_loc": {**base, "p": 0, "r": len(runs) - 1},
            # fixed (baked-in) therapist details: validated as present, never modified
            "therapist_details": {
                key: {
                    "label": lbl,
                    "label_loc": {"table": ti, "row": 1, "cell": 1, "p": pi, "r": 0},
                    "value_loc": {"table": ti, "row": 1, "cell": 1, "p": pi, "r": 1},
                }
                for pi, (key, lbl) in enumerate([
                    ("license_no", "Αρ. άδειας άσκησης επαγγέλματος:"),
                    ("authority", "Αρμόδια εκδούσα αρχή:"),
                    ("license_date", "Ημερομηνία:"),
                ])
            },
        }
        hv = next((pi for pi, hp in enumerate(table.rows[0].cells[0].paragraphs)
                   if hp.runs and hp.runs[0].text.startswith("Κατ’ οίκον")), None)
        if hv is not None:
            block["home_visit"] = {
                "yes_label_loc": {**base, "p": hv, "r": 1}, "yes_box_loc": {**base, "p": hv, "r": 2},
                "no_label_loc": {**base, "p": hv, "r": 3}, "no_box_loc": {**base, "p": hv, "r": 4},
            }
        blocks.append(block)
    return {
        "name": name,
        "sha256": None,  # filled by caller after the file is final
        "configuration_key": sorted([b["therapy"], b["therapist"]] for b in blocks),
        "patient_format": "{first} {last} του {father} με ΑΜΚΑ {amka}",
        "fields": {
            "patient": {"loc": {"p": intro, "r": 0}, "between": ["ο/η ", " παρακολούθησε"]},
            "period": {"loc": {"p": intro, "r": period_run}, "expect": "period"},
            "issue_date": {"loc": {"p": sig_idx + 1, "r": 0}, "expect": "date"},
            # centre-level settings
            "ada": {"loc": {"p": ada_idx, "r": 0}, "between": ["ΑΔΑ: ", ""]},
            "representative": {"loc": {"p": len(paras) - 1, "r": 0}, "expect": "name"},
        },
        "remove_watermark": True,
        "blocks": blocks,
    }


def finalize(name: str, doc, assignments: dict[str, str]) -> None:
    from docx_render import sha256_file

    path = OUT / f"{name}.docx"
    doc.save(str(path))
    fm = build_field_map(path, assignments, name)
    fm["sha256"] = sha256_file(path)
    (OUT / f"{name}.map.json").write_text(json.dumps(fm, ensure_ascii=False, indent=2), encoding="utf-8")


def make_synthetic(name: str, keep: dict[str, str], assets: dict[str, Path], advanced: bool = False) -> None:
    """keep: therapy_id -> therapist tag (T1..T3). Adds fake therapist data, signature, stamp, logo."""
    doc = Document(str(SRC))
    sanitize_patient(doc)

    for table in list(doc.tables):
        if LABEL_TO_THERAPY[table_label(table)] not in keep:
            remove_block(doc, table)

    pid = 100
    for table in doc.tables:
        tag = keep[LABEL_TO_THERAPY[table_label(table)]]
        # fixed therapist details (fake)
        details = table.rows[1].cells[1].paragraphs
        set_t(details[0].runs[-1], f"{500 + int(tag[1])}/1{tag[1]}")
        set_t(details[1].runs[-1], "Νομαρχία Δοκιμής")
        set_t(details[2].runs[-1], "01/01/2010")
        left = table.rows[1].cells[0]
        sigp = left.add_paragraph()
        sigp.add_run().add_picture(str(assets[f"sig{tag}"]), width=Mm(32))
        pid += 1
        add_floating_picture(sigp, doc, assets[f"stamp{tag}"], 24, 24, 24, -8, pid,
                             behind=advanced, rot=1200000 if advanced else 0, crop=advanced)

    # logo at the top
    first = doc.paragraphs[0]
    logo_p = first.insert_paragraph_before()
    logo_p.add_run().add_picture(str(assets["logo"]), width=Mm(30))

    if advanced:
        # JPEG logo in the header and an image in the footer, as real templates often have
        sec = doc.sections[0]
        sec.header.add_paragraph().add_run().add_picture(str(assets["logo_jpg"]), width=Mm(28))
        sec.footer.paragraphs[0].add_run().add_picture(str(assets["logo"]), width=Mm(20))

    # fake ADA and representative name (centre-level values baked into the template)
    set_t(next(p for p in doc.paragraphs if p.text.startswith("ΑΔΑ:")).runs[0], "ΑΔΑ: ΔΟΚΙΜΗ-ΑΒΓ")
    last = doc.paragraphs[-1]
    set_t(last.runs[0], "Δοκιμαστική Εκπρόσωπος")

    finalize(name, doc, {t: v for t, v in keep.items()})


def make_real_sanitized() -> None:
    doc = Document(str(SRC))
    sanitize_patient(doc)
    # therapist assignment is an admin decision when registering the template
    finalize("real-5", doc, {
        "speech": "T1", "occupational": "T2", "psychotherapy": "T3",
        "special_education": "T1", "physiotherapy": "T4",
    })


def main() -> None:
    OUT.mkdir(exist_ok=True)
    assets = make_assets()
    make_real_sanitized()
    make_synthetic("synthetic-A", {"psychotherapy": "T1"}, assets)
    make_synthetic("synthetic-B", {"speech": "T1", "occupational": "T2", "psychotherapy": "T3"}, assets)
    # C: has the physiotherapy block (home-visit checkbox) + header/footer images,
    #    a rotated/cropped stamp behind the text
    make_synthetic("synthetic-C", {"speech": "T1", "physiotherapy": "T2"}, assets, advanced=True)
    print("templates written to", OUT)


if __name__ == "__main__":
    main()
