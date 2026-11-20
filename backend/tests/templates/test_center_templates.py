"""The development center (D-28): three therapies, one therapist each, seven synthetic
TEST ONLY templates — one per non-empty combination — and exact matching."""

from __future__ import annotations

import shutil
import zipfile
from datetime import date
from types import SimpleNamespace

import pytest
from docx import Document
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db import make_engine
from app.documents import (
    MissingValue,
    generate_document,
    render,
    verify_document,
)
from app.documents.mapping import suggest_field_map
from app.models import GeneratedDocument, TemplateVersion, Therapist, TherapyType
from app.services.templates import (
    AmbiguousTemplates,
    NoMatchingTemplate,
    TemplateConflict,
    activate_template,
    add_version,
    deactivate_template,
    match_for_period,
    match_template,
    register_template,
)
from app.storage import FileStore
from fixtures.center_templates import (
    CENTER_SECTIONS,
    CENTER_THERAPIES,
    COMBINATIONS,
    NOT_PROVIDED,
    TEST_ONLY_MARK,
    build_center_template,
    licence_date,
    template_name,
)
from fixtures.eopyy_builder import FIVE_SECTIONS, build_eopyy_docx
from fixtures.eopyy_maps import production_field_map
from tests.db import factories as f
from tests.documents.conftest import make_data

DATES = [date(2026, 10, 20), date(2026, 10, 27), date(2026, 11, 3)]
IDS = {combo: "+".join(combo) for combo in COMBINATIONS}


# ---------------------------------------------------------------- the seeded center
def _seed(session: Session, store: FileStore, uploads) -> dict:
    ids = {"types": {}, "therapists": {}, "templates": {}, "versions": {}}
    for code, block in CENTER_SECTIONS.items():
        tt = TherapyType(code=code, name_el=block.label.rstrip(":"), template_label=block.label)
        th = Therapist(
            full_name=block.full_name,
            license_no=block.license_no,
            license_authority=block.authority,
            license_date=licence_date(block),
        )
        session.add_all([tt, th])
        session.flush()
        ids["types"][code], ids["therapists"][code] = tt.id, th.id
    for code, label in NOT_PROVIDED.items():  # on the form, but not offered by the center
        tt = TherapyType(code=code, name_el=label.rstrip(":"), template_label=label, active=False)
        session.add(tt)
        session.flush()
        ids["types"][code] = tt.id
    for combo in COMBINATIONS:
        built = build_center_template(uploads, combo)
        pairs = [(ids["types"][c], ids["therapists"][c]) for c in combo]
        template = register_template(session, template_name(combo), pairs).template
        result = add_version(session, store, template.id, built.path, production_field_map(built))
        assert result.valid, result.errors
        activate_template(session, store, template.id)
        ids["templates"][combo], ids["versions"][combo] = template.id, result.version.id
    return ids


@pytest.fixture(scope="module")
def center_seed(tmp_path_factory, migrated_template):
    root = tmp_path_factory.mktemp("center")
    db = root / "seed.db"
    shutil.copyfile(migrated_template, db)
    engine = make_engine(db)
    with Session(engine) as session:
        ids = _seed(session, FileStore(root / "data"), root / "uploads")
        session.commit()
    engine.dispose()
    return root, ids


@pytest.fixture()
def center(center_seed, tmp_path):
    root, ids = center_seed
    shutil.copyfile(root / "seed.db", tmp_path / "center.db")
    shutil.copytree(root / "data", tmp_path / "data")
    engine = make_engine(tmp_path / "center.db")
    with Session(engine) as session:
        yield SimpleNamespace(
            session=session, store=FileStore(tmp_path / "data"), ids=ids, uploads=root / "uploads"
        )
    engine.dispose()


def pairs(c, codes):
    return [(c.ids["types"][x], c.ids["therapists"][x]) for x in codes]


# ---------------------------------------------------------------- exact matching
@pytest.mark.parametrize("combo", COMBINATIONS, ids=IDS.get)
def test_every_combination_matches_its_own_template(center, combo):
    match = match_template(center.session, center.store, pairs(center, reversed(combo)))
    assert match.template_id == center.ids["templates"][combo]
    assert match.version_id == center.ids["versions"][combo]
    assert match.selected_therapies == tuple(sorted(combo))
    assert {b.therapy for b in match.field_map.blocks} == set(combo)


@pytest.mark.parametrize("combo", [c for c in COMBINATIONS if len(c) < 3], ids=IDS.get)
def test_a_subset_never_uses_a_larger_template(center, combo):
    deactivate_template(center.session, center.ids["templates"][combo])
    with pytest.raises(NoMatchingTemplate, match="no active template for exactly"):
        match_template(center.session, center.store, pairs(center, combo))


@pytest.mark.parametrize("combo", [c for c in COMBINATIONS if len(c) > 1], ids=IDS.get)
def test_a_superset_never_uses_a_smaller_template(center, combo):
    deactivate_template(center.session, center.ids["templates"][combo])
    with pytest.raises(NoMatchingTemplate):
        match_template(center.session, center.store, pairs(center, combo))


def test_a_different_therapist_never_matches(center):
    other = f.therapist(center.session, "Θεραπευτής ΔΟΚ-Ω", license_no="ΔΟΚ-ΛΟΓ-0099")
    request = pairs(center, ("speech", "psychotherapy"))
    request[0] = (request[0][0], other.id)
    with pytest.raises(NoMatchingTemplate):
        match_template(center.session, center.store, request)


@pytest.mark.parametrize("code", sorted(NOT_PROVIDED))
def test_therapies_the_center_does_not_provide_are_rejected(center, code):
    request = [
        *pairs(center, ("speech",)),
        (center.ids["types"][code], center.ids["therapists"]["speech"]),
    ]
    with pytest.raises(NoMatchingTemplate):
        match_template(center.session, center.store, request)
    # a template with such a section cannot become valid: the therapy is inactive
    full = build_eopyy_docx(center.uploads / f"form-{code}.docx", FIVE_SECTIONS)
    template = register_template(
        center.session,
        f"Πλήρες έντυπο {code}",
        [(center.ids["types"][code], center.ids["therapists"]["speech"])],
    )
    result = add_version(
        center.session, center.store, template.template.id, full.path, production_field_map(full)
    )
    assert not result.valid and any("is inactive" in e for e in result.errors)


def test_missing_and_ambiguous_templates(center):
    from app.services.templates import InvalidConfiguration

    s = center.session
    with pytest.raises(InvalidConfiguration):
        match_template(s, center.store, [])  # an empty request is an input error
    deactivate_template(s, center.ids["templates"][("psychotherapy",)])
    with pytest.raises(NoMatchingTemplate, match="no active template for exactly"):
        match_template(s, center.store, pairs(center, ("psychotherapy",)))  # missing template
    s.rollback()
    # a second template for an existing combination cannot be activated ...
    built = build_center_template(center.uploads / "second", ("speech",))
    second = register_template(s, "Δεύτερο λογοθεραπείας", pairs(center, ("speech",)))
    assert any("same combination" in w for w in second.warnings)
    with pytest.raises(Exception):  # noqa: B017 - same file as the seeded template
        add_version(s, center.store, second.template.id, built.path, production_field_map(built))
    s.rollback()
    other = build_eopyy_docx(
        center.uploads / "other-speech.docx",
        (CENTER_SECTIONS["speech"],),
        print_names=True,
        sample_dates=True,
    )
    second = register_template(s, "Δεύτερο λογοθεραπείας", pairs(center, ("speech",))).template
    assert add_version(s, center.store, second.id, other.path, production_field_map(other)).valid
    with pytest.raises(TemplateConflict, match="already active"):
        activate_template(s, center.store, second.id)
    # ... and if the database guard were ever bypassed, matching refuses to choose
    s.execute(text("DROP INDEX uq_document_templates_one_active_per_configuration"))
    version = s.scalars(
        select(TemplateVersion).where(TemplateVersion.template_id == second.id)
    ).one()
    s.execute(
        text("UPDATE document_templates SET current_version_id = :v, active = 1 WHERE id = :t"),
        {"v": version.id, "t": second.id},
    )
    with pytest.raises(AmbiguousTemplates):
        match_template(s, center.store, pairs(center, ("speech",)))


def test_reassignment_affects_future_periods_only(center):
    s, store = center.session, center.store
    patient = f.patient(s)
    rx = f.prescription(s, patient, date(2025, 9, 16), date(2026, 3, 15))
    item = f.item(s, rx, s.get(TherapyType, center.ids["types"]["speech"]), 8)
    old_therapist = s.get(Therapist, center.ids["therapists"]["speech"])
    october = f.period(s, patient, date(2025, 10, 16), date(2025, 11, 15))
    f.period_therapy(
        s, october, f.referral(s, item, 2, date(2025, 10, 16), date(2025, 11, 15), 8), old_therapist
    )
    old_match = match_for_period(s, store, october.id)
    f.document(s, october, s.get(TemplateVersion, old_match.version_id))
    s.commit()

    # the speech therapist changes: a new therapist with her own speech-only template
    block = CENTER_SECTIONS["speech"]
    new = Therapist(
        full_name="Θεραπευτής ΔΟΚ-Δ",
        license_no="ΔΟΚ-ΛΟΓ-0004",
        license_authority=block.authority,
        license_date=licence_date(block),
    )
    s.add(new)
    s.flush()
    from fixtures.eopyy_builder import TherapyBlock

    new_block = TherapyBlock(
        "speech",
        "Λογοθεραπεία:",
        "ΔΟΚ-Δ",
        "ΔΟΚ-ΛΟΓ-0004",
        authority=block.authority,
        license_date=block.license_date,
    )
    built = build_eopyy_docx(
        center.uploads / "new-speech.docx",
        (new_block,),
        print_names=True,
        test_marking=TEST_ONLY_MARK,
    )
    template = register_template(
        s, "ΔΟΚΙΜΑΣΤΙΚΟ Λογοθεραπεία (νέα)", [(center.ids["types"]["speech"], new.id)]
    ).template
    assert add_version(s, store, template.id, built.path, production_field_map(built)).valid
    activate_template(s, store, template.id)
    november = f.period(s, patient, date(2025, 11, 16), date(2025, 12, 15))
    f.period_therapy(
        s, november, f.referral(s, item, 3, date(2025, 11, 16), date(2025, 12, 15), 8), new
    )
    s.commit()

    assert match_for_period(s, store, november.id).template_id == template.id
    assert match_for_period(s, store, october.id).template_id == old_match.template_id
    issued = s.scalars(select(GeneratedDocument)).one()
    assert issued.template_version_id == old_match.version_id
    old_version = s.get(TemplateVersion, old_match.version_id)
    store.verify(old_version.storage_path, old_version.sha256)  # file and version unchanged


# ---------------------------------------------------------------- rendering and PDF
@pytest.mark.parametrize("combo", COMBINATIONS, ids=IDS.get)
def test_every_section_is_filled(center, combo, tmp_path):
    match = match_template(center.session, center.store, pairs(center, combo))
    data = make_data(_built(center, combo), sessions={c: DATES for c in combo}, home_visit={})
    out = tmp_path / "out.docx"
    result = render(match.path, match.field_map, data, out)
    assert verify_document(match.path, out, result, match.field_map) == []
    doc = Document(str(out))
    assert len(doc.tables) == len(combo)
    for table in doc.tables:
        assert table.rows[0].cells[0].paragraphs[0].runs[2].text == "20/10/26, 27/10/26, 3/11/26"
    assert TEST_ONLY_MARK in doc.paragraphs[0].text
    with zipfile.ZipFile(match.path) as zt, zipfile.ZipFile(out) as zo:
        media = [n for n in zt.namelist() if n.startswith("word/media/")]
        assert media and all(zt.read(n) == zo.read(n) for n in media)


def test_every_section_must_be_filled(center, tmp_path):
    combo = ("speech", "occupational", "psychotherapy")
    match = match_template(center.session, center.store, pairs(center, combo))
    data = make_data(_built(center, combo), sessions={"speech": DATES}, home_visit={})
    with pytest.raises(MissingValue, match="no session dates supplied"):
        render(match.path, match.field_map, data, tmp_path / "o.docx")


@pytest.mark.libreoffice
@pytest.mark.parametrize("combo", COMBINATIONS, ids=IDS.get)
def test_pdf_for_every_combination(center, combo, tmp_path, soffice):
    match = match_template(center.session, center.store, pairs(center, combo))
    data = make_data(_built(center, combo), sessions={c: DATES for c in combo}, home_visit={})
    result = generate_document(
        match.path, match.field_map, data, tmp_path / "w", soffice=soffice, expected_pages=1
    )
    from pypdf import PdfReader

    pdf_text = " ".join(" ".join(p.extract_text() for p in PdfReader(result.pdf).pages).split())
    assert "TEST ONLY" in pdf_text and "20/10/26" in pdf_text


def _built(center, combo):
    """A BuiltFixture describing the seeded upload (for make_data's defaults)."""
    return SimpleNamespace(blocks=tuple(CENTER_SECTIONS[c] for c in CENTER_THERAPIES if c in combo))


# ---------------------------------------------------------------- mapping assistant
LABELS = {b.label: code for code, b in CENTER_SECTIONS.items()}


@pytest.mark.parametrize("combo", COMBINATIONS, ids=IDS.get)
def test_mapping_assistant_for_one_two_or_three_sections(tmp_path, combo):
    built = build_center_template(tmp_path, combo)
    suggestion = suggest_field_map(
        built.path,
        therapy_codes=LABELS,
        ada_mode="dynamic",
        representative_mode="dynamic",
        expected_therapies=combo,
    )
    assert suggestion.notes == []
    expected = production_field_map(built).model_copy(update={"forbidden_text": ()})
    # the assistant does not look for the optional printed name; everything else is equal
    expected_blocks = tuple(
        b.model_copy(
            update={"therapist_details": b.therapist_details.model_copy(update={"name": None})}
        )
        for b in expected.blocks
    )
    assert suggestion.field_map == expected.model_copy(update={"blocks": expected_blocks})


def test_mapping_assistant_refuses_a_map_that_does_not_equal_the_configuration(tmp_path):
    built = build_center_template(tmp_path, ("speech", "psychotherapy"))
    for expected in (("speech",), ("speech", "occupational", "psychotherapy")):
        suggestion = suggest_field_map(
            built.path,
            therapy_codes=LABELS,
            ada_mode=None,
            representative_mode=None,
            expected_therapies=expected,
        )
        assert suggestion.field_map is None and suggestion.notes
