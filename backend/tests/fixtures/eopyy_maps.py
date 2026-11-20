"""Field maps for synthetic fixtures (production schema and the spike's JSON format).

In the application an administrator configures a map once per template version;
for tests we derive it from the builder's recorded layout.
"""

from __future__ import annotations

from app.documents.field_map import (
    Block,
    CenterField,
    FieldMap,
    HomeVisit,
    LabeledValue,
    Locator,
    TextField,
    TherapistDetails,
)
from app.documents.package import sha256_file

from .eopyy_builder import DETAIL_LABELS, NAME_LABEL, BuiltFixture


def _cell(
    table: int, row: int, cell: int, paragraph: int, start: int, end: int | None = None
) -> Locator:
    return Locator(
        table=table,
        row=row,
        cell=cell,
        paragraph=paragraph,
        runs=(start, start if end is None else end),
    )


def production_field_map(
    built: BuiltFixture, center_mode: str = "dynamic", **overrides
) -> FieldMap:
    """``center_mode`` applies to both center fields (ΑΔΑ, representative)."""
    lay = built.layout
    if lay.ada_header_paragraph >= 0:
        ada_loc = Locator(part="header", paragraph=lay.ada_header_paragraph, runs=(0, 0))
    else:
        ada_loc = Locator(paragraph=lay.ada_paragraph, runs=(0, 0))
    if lay.representative_footer_paragraph >= 0:
        rep_loc = Locator(part="footer", paragraph=lay.representative_footer_paragraph, runs=(0, 0))
    else:
        rep_loc = Locator(paragraph=lay.representative_paragraph, runs=(0, 0))
    blocks = []
    for b in built.blocks:
        t = lay.block_tables[b.code]
        details = TherapistDetails(
            **{
                key: LabeledValue(
                    label=DETAIL_LABELS[i],
                    label_loc=_cell(t, 1, 1, i, 0),
                    value_loc=_cell(t, 1, 1, i, 1),
                )
                for i, key in enumerate(("license_no", "authority", "license_date"))
            },
            name=LabeledValue(
                label=NAME_LABEL, label_loc=_cell(t, 1, 1, 3, 0), value_loc=_cell(t, 1, 1, 3, 1)
            )
            if built.names_printed
            else None,
        )
        home = None
        if b.has_home_visit:
            home = HomeVisit(
                yes_label_loc=_cell(t, 0, 0, 2, 1),
                yes_box_loc=_cell(t, 0, 0, 2, 2),
                no_label_loc=_cell(t, 0, 0, 2, 3),
                no_box_loc=_cell(t, 0, 0, 2, 4),
            )
        blocks.append(
            Block(
                therapy=b.code,
                label=b.label,
                label_loc=_cell(t, 0, 0, 0, 0),
                dates_loc=_cell(t, 0, 0, 0, 2),
                therapist_details=details,
                home_visit=home,
            )
        )
    values = {
        "template_sha256": sha256_file(built.path),
        "patient": TextField(
            loc=Locator(paragraph=lay.intro_paragraph, runs=lay.patient_runs),
            between=("ο/η ", " παρακολούθησε"),
        ),
        "period": TextField(
            loc=Locator(paragraph=lay.intro_paragraph, runs=lay.period_runs), expect="date_range"
        ),
        "issue_date": TextField(
            loc=Locator(paragraph=lay.issue_date_paragraph, runs=(0, 0)), expect="date"
        ),
        "ada": CenterField(loc=ada_loc, between=("ΑΔΑ: ", ""), mode=center_mode),
        "representative": CenterField(loc=rep_loc, expect="text", mode=center_mode),
        "blocks": tuple(blocks),
        "forbidden_text": ("ΠΛΑΣΤΟ",),
    }
    return FieldMap(**{**values, **overrides})


def spike_field_map(built: BuiltFixture) -> dict:
    """The same mapping in the spike's JSON format (default fixture layout only)."""
    lay = built.layout
    assert lay.ada_paragraph >= 0 and lay.representative_paragraph >= 0
    assert lay.patient_runs == (0, 0) and lay.period_runs == (1, 1)

    def cell(t, row, c, p, r):
        return {"table": t, "row": row, "cell": c, "p": p, "r": r}

    blocks = []
    for b in built.blocks:
        t = lay.block_tables[b.code]
        block = {
            "therapy": b.code,
            "therapist": b.therapist,
            "label": b.label,
            "label_loc": cell(t, 0, 0, 0, 0),
            "dates_loc": cell(t, 0, 0, 0, 2),
            "therapist_details": {
                key: {
                    "label": DETAIL_LABELS[i].strip(),
                    "label_loc": cell(t, 1, 1, i, 0),
                    "value_loc": cell(t, 1, 1, i, 1),
                }
                for i, key in enumerate(("license_no", "authority", "license_date"))
            },
        }
        if b.has_home_visit:
            block["home_visit"] = {
                "yes_label_loc": cell(t, 0, 0, 2, 1),
                "yes_box_loc": cell(t, 0, 0, 2, 2),
                "no_label_loc": cell(t, 0, 0, 2, 3),
                "no_box_loc": cell(t, 0, 0, 2, 4),
            }
        blocks.append(block)
    return {
        "name": built.path.stem,
        "sha256": sha256_file(built.path),
        "configuration_key": sorted([b.code, b.therapist] for b in built.blocks),
        "patient_format": "{first} {last} του {father} με ΑΜΚΑ {amka}",
        "fields": {
            "patient": {
                "loc": {"p": lay.intro_paragraph, "r": 0},
                "between": ["ο/η ", " παρακολούθησε"],
            },
            "period": {"loc": {"p": lay.intro_paragraph, "r": 1}, "expect": "period"},
            "issue_date": {"loc": {"p": lay.issue_date_paragraph, "r": 0}, "expect": "date"},
            "ada": {"loc": {"p": lay.ada_paragraph, "r": 0}, "between": ["ΑΔΑ: ", ""]},
            "representative": {
                "loc": {"p": lay.representative_paragraph, "r": 0},
                "expect": "name",
            },
        },
        "remove_watermark": True,
        "blocks": blocks,
    }
