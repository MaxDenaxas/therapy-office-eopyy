"""Mapping assistant: propose a field map for a template of the EOPYY attestation form family.

It recognises the form by its printed labels and suggests a location for every field.
It never decides for the administrator: which therapy each section is (by heading)
and whether ΑΔΑ / representative are dynamic or static (D-25) are inputs, and every
suggestion must still pass ``validate_template`` before a version can be activated.
Templates that differ from the form family get notes instead of guesses.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from docx import Document

from .field_map import (
    EXPECTATIONS,
    Block,
    CenterField,
    FieldMap,
    HomeVisit,
    LabeledValue,
    Locator,
    TextField,
    TherapistDetails,
)
from .package import inspect_docx

INTRO_MARK = "Βεβαιώνεται ότι ο/η "
INTRO_END = " παρακολούθησε"
SIGNATURE_LINE = "Ημερομηνία / Σφραγίδα / Υπογραφή"
ADA_LABEL = "ΑΔΑ: "
HOME_VISIT_MARKERS = ("Κατ’ οίκον", "Κατ' οίκον")
DETAIL_LABELS = (
    ("license_no", "Αρ. άδειας άσκησης επαγγέλματος:"),
    ("authority", "Αρμόδια εκδούσα αρχή:"),
    ("license_date", "Ημερομηνία:"),
)
Mode = Literal["dynamic", "static"]


@dataclass
class Suggestion:
    field_map: FieldMap | None
    notes: list[str] = field(default_factory=list)  # what could not be recognised


def _cell(t: int, row: int, c: int, p: int, start: int, end: int | None = None) -> Locator:
    return Locator(
        table=t, row=row, cell=c, paragraph=p, runs=(start, start if end is None else end)
    )


def suggest_field_map(
    template: Path,
    *,
    therapy_codes: Mapping[str, str],
    ada_mode: Mode | None,
    representative_mode: Mode | None,
    expected_therapies: Iterable[str] | None = None,
) -> Suggestion:
    """``therapy_codes`` maps a section heading as printed (e.g. "Λογοθεραπεία:") to a
    therapy_types.code. A mode of None means the field is not mapped at all.

    A template may contain any number of sections (one, two, three, ... of the form's
    five). ``expected_therapies`` — the template's configured therapy codes — makes the
    suggestion exact: if the recognised sections are not exactly those therapies, or a
    heading appears twice, no map is suggested, only notes to review."""
    info = inspect_docx(template)
    doc = Document(str(template))
    notes: list[str] = []
    paras = doc.paragraphs

    def find_body(predicate) -> int | None:
        return next((i for i, p in enumerate(paras) if predicate(p.text)), None)

    intro = find_body(lambda t: INTRO_MARK in t and INTRO_END in t)
    patient = period = None
    if intro is None:
        notes.append(f"intro sentence ('{INTRO_MARK.strip()} … {INTRO_END.strip()}') not found")
    else:
        runs = [r.text for r in paras[intro].runs]
        p_run = next((i for i, t in enumerate(runs) if INTRO_MARK in t and INTRO_END in t), None)
        d_run = next(
            (
                i
                for i, t in enumerate(runs)
                if t.strip() and re.match(EXPECTATIONS["date_range"], t)
            ),
            None,
        )
        if p_run is None:
            notes.append("the patient text is split over several runs; map it by hand")
        else:
            patient = TextField(
                loc=Locator(paragraph=intro, runs=(p_run, p_run)),
                between=(INTRO_MARK[-4:], INTRO_END),
            )
        if d_run is None:
            notes.append("the period (a date range in the intro sentence) was not found in one run")
        else:
            period = TextField(
                loc=Locator(paragraph=intro, runs=(d_run, d_run)), expect="date_range"
            )

    sign = find_body(lambda t: t.strip() == SIGNATURE_LINE)
    issue = None
    if sign is None or sign + 1 >= len(paras):
        notes.append(f"'{SIGNATURE_LINE}' line (followed by the issue date) not found")
    else:
        issue = TextField(loc=Locator(paragraph=sign + 1, runs=(0, 0)), expect="date")

    ada = None
    if ada_mode is not None:
        loc = None
        body = find_body(lambda t: t.startswith(ADA_LABEL.strip()))
        if body is not None:
            loc = Locator(paragraph=body, runs=(0, 0))
        else:
            header = doc.sections[0].header
            if not header.is_linked_to_previous:
                hp = next(
                    (
                        i
                        for i, p in enumerate(header.paragraphs)
                        if p.text.startswith(ADA_LABEL.strip())
                    ),
                    None,
                )
                if hp is not None:
                    loc = Locator(part="header", paragraph=hp, runs=(0, 0))
        if loc is None:
            notes.append("ΑΔΑ line not found; leave it unmapped or map it by hand")
        else:
            ada = CenterField(loc=loc, between=(ADA_LABEL, ""), mode=ada_mode)

    representative = None
    if representative_mode is not None:
        last = next((i for i in range(len(paras) - 1, -1, -1) if paras[i].text.strip()), None)
        if sign is None or last is None or last <= sign + 1:
            notes.append("representative name (after the issue date) not found")
        else:
            representative = CenterField(
                loc=Locator(paragraph=last, runs=(0, 0)), expect="text", mode=representative_mode
            )

    blocks = []
    for t, table in enumerate(doc.tables):
        try:
            top = table.rows[0].cells[0]
            label = top.paragraphs[0].runs[0].text.strip()
        except IndexError:
            notes.append(f"table {t}: no section heading found")
            continue
        code = therapy_codes.get(label)
        if code is None:
            notes.append(f"table {t}: heading {label!r} is not linked to a therapy")
            continue
        dates_run = len(top.paragraphs[0].runs) - 1
        try:
            right = table.rows[1].cells[1]
            details = {}
            for i, (key, text) in enumerate(DETAIL_LABELS):
                if not right.paragraphs[i].runs[0].text.strip().startswith(text):
                    raise IndexError
                details[key] = LabeledValue(
                    label=right.paragraphs[i].runs[0].text,
                    label_loc=_cell(t, 1, 1, i, 0),
                    value_loc=_cell(t, 1, 1, i, 1),
                )
        except IndexError:
            notes.append(f"table {t} ({label}): therapist details not in the expected place")
            continue
        home = None
        for p_index, paragraph in enumerate(top.paragraphs):
            texts = [r.text for r in paragraph.runs]
            if texts and any(texts[0].startswith(m) for m in HOME_VISIT_MARKERS):
                try:
                    yes = next(i for i, x in enumerate(texts) if "ΝΑΙ" in x)
                    no = next(i for i, x in enumerate(texts) if "ΟΧΙ" in x)
                    home = HomeVisit(
                        yes_label_loc=_cell(t, 0, 0, p_index, yes),
                        yes_box_loc=_cell(t, 0, 0, p_index, yes + 1),
                        no_label_loc=_cell(t, 0, 0, p_index, no),
                        no_box_loc=_cell(t, 0, 0, p_index, no + 1),
                    )
                except StopIteration:
                    notes.append(f"table {t} ({label}): home-visit line not recognised")
        blocks.append(
            Block(
                therapy=code,
                label=label,
                label_loc=_cell(t, 0, 0, 0, 0),
                dates_loc=_cell(t, 0, 0, 0, dates_run),
                therapist_details=TherapistDetails(**details),
                home_visit=home,
            )
        )

    found = [b.therapy for b in blocks]
    for code in sorted({c for c in found if found.count(c) > 1}):
        notes.append(f"therapy {code!r} has more than one section; map it by hand")
    if expected_therapies is not None:
        expected = set(expected_therapies)
        for code in sorted(expected - set(found)):
            notes.append(f"configured therapy {code!r} has no section in this document")
        for code in sorted(set(found) - expected):
            notes.append(f"the document has a section for {code!r}, which is not configured")
        if expected != set(found):
            return Suggestion(None, notes)
    if len(found) != len(set(found)):
        return Suggestion(None, notes)
    if patient is None or period is None or issue is None or not blocks:
        return Suggestion(None, notes or ["no therapy sections recognised"])
    fmap = FieldMap(
        template_sha256=info.sha256,
        patient=patient,
        period=period,
        issue_date=issue,
        ada=ada,
        representative=representative,
        blocks=tuple(blocks),
    )
    return Suggestion(fmap, notes)
