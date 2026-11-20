"""The development center: three fictional therapies/therapists and seven synthetic templates.

The center provides speech therapy, psychotherapy and occupational therapy, one therapist
each, and keeps one template per non-empty combination (7 templates). Every name, licence
number, authority, date, signature and stamp here is invented; each template carries an
unmistakable TEST ONLY marking (static text that survives into generated documents) and
crossed-out stamps. They exist only to exercise the workflow until the center's real
templates replace them. Nothing here is center configuration in the application: tests
seed it into the database like an administrator would.
"""

from __future__ import annotations

from datetime import date
from itertools import combinations
from pathlib import Path

from .eopyy_builder import BuiltFixture, TherapyBlock, build_eopyy_docx

TEST_ONLY_MARK = "ΔΟΚΙΜΑΣΤΙΚΟ ΠΡΟΤΥΠΟ — TEST ONLY — ΔΕΝ ΙΣΧΥΕΙ ΓΙΑ ΥΠΟΒΟΛΗ ΣΤΟΝ ΕΟΠΥΥ"

# section order as printed on the EOPYY form
CENTER_SECTIONS: dict[str, TherapyBlock] = {
    "speech": TherapyBlock(
        "speech",
        "Λογοθεραπεία:",
        "ΔΟΚ-Α",
        "ΔΟΚ-ΛΟΓ-0001",
        authority="Δοκιμαστική Περιφέρεια Άλφα",
        license_date="15/03/2012",
    ),
    "occupational": TherapyBlock(
        "occupational",
        "Εργοθεραπεία:",
        "ΔΟΚ-Γ",
        "ΔΟΚ-ΕΡΓ-0003",
        authority="Δοκιμαστική Περιφέρεια Γάμμα",
        license_date="02/09/2015",
    ),
    "psychotherapy": TherapyBlock(
        "psychotherapy",
        "Ψυχοθεραπεία:",
        "ΔΟΚ-Β",
        "ΔΟΚ-ΨΥΧ-0002",
        authority="Δοκιμαστική Περιφέρεια Βήτα",
        license_date="20/01/2018",
    ),
}
CENTER_THERAPIES = tuple(CENTER_SECTIONS)

# the seven non-empty combinations, each in form order
COMBINATIONS: tuple[tuple[str, ...], ...] = tuple(
    combo for size in (1, 2, 3) for combo in combinations(CENTER_THERAPIES, size)
)

# therapies of the EOPYY form that this center does not provide
NOT_PROVIDED = {
    "special_education": "Ειδική διαπαιδαγώγηση:",
    "physiotherapy": "Φυσικοθεραπεία:",
}


def template_name(codes: tuple[str, ...]) -> str:
    return "ΔΟΚΙΜΑΣΤΙΚΟ " + " + ".join(CENTER_SECTIONS[c].label.rstrip(":") for c in codes)


def licence_date(block: TherapyBlock) -> date:
    day, month, year = (int(x) for x in block.license_date.split("/"))
    return date(year, month, day)


def build_center_template(directory: Path, codes: tuple[str, ...]) -> BuiltFixture:
    blocks = tuple(CENTER_SECTIONS[c] for c in CENTER_THERAPIES if c in codes)
    name = "-".join(codes) + ".docx"
    return build_eopyy_docx(
        Path(directory) / name, blocks, print_names=True, test_marking=TEST_ONLY_MARK
    )
