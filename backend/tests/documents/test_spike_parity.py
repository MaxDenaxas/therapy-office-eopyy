"""Parity with the frozen spike (``spike/docx_render.py``, ``spike/validate.py``).

The spike's modules are imported read-only from the repository and run on the same
synthetic fixtures. Outputs must be identical part by part; each engine's
verification must accept the other's output. Intentional differences are tested
explicitly at the end (see docs/DECISIONS.md P-33).
"""

from __future__ import annotations

import sys
import zipfile
from datetime import date
from types import SimpleNamespace

import pytest

from app.documents import InvalidDocumentData, render, validate_template, verify_document
from fixtures import eopyy_builder as fx
from fixtures.eopyy_maps import production_field_map, spike_field_map
from tests.conftest import REPO_ROOT
from tests.documents.conftest import make_data

SPIKE_DIR = REPO_ROOT / "spike"


@pytest.fixture(scope="module")
def spike():
    sys.path.insert(0, str(SPIKE_DIR))
    try:
        import docx_render  # noqa: PLC0415 - the spike is not a package
        import validate
    finally:
        sys.path.remove(str(SPIKE_DIR))
    assert docx_render.__file__.startswith(str(SPIKE_DIR))
    return SimpleNamespace(render=docx_render, validate=validate)


def spike_data(spike, data):
    return spike.render.RenderData(
        first_name=data.first_name,
        last_name=data.last_name,
        father_name=data.father_name,
        amka=data.amka,
        period_start=data.period_start,
        period_end=data.period_end,
        issue_date=data.issue_date,
        sessions={k: list(v) for k, v in data.sessions.items()},
        ada=data.ada,
        representative=data.representative,
        home_visit=dict(data.home_visit),
    )


def word_like(built):
    """Give every text node xml:space="preserve", as Word itself writes (and as the real
    template has). The spike's structure check assumes this; see the difference test below."""
    from docx import Document
    from docx.oxml.ns import qn

    doc = Document(str(built.path))
    for part in (
        doc.part,
        *[s.header.part for s in doc.sections],
        *[s.footer.part for s in doc.sections],
    ):
        for t in part.element.iter(qn("w:t")):
            t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    doc.save(str(built.path))
    return built


def parts(path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as z:
        return {n: z.read(n) for n in z.namelist()}


def both(tmp_path, spike, built, data):
    # the spike keeps template text whenever no value is given; production requires the
    # field to be mapped as static for that (D-25)
    static = data.ada is None and data.representative is None
    fmap = production_field_map(built, center_mode="static" if static else "dynamic")
    out_spike, out_prod = tmp_path / "spike.docx", tmp_path / "production.docx"
    spike_result = spike.render.render(
        built.path, spike_field_map(built), spike_data(spike, data), out_spike
    )
    prod_result = render(built.path, fmap, data, out_prod)
    return fmap, (out_spike, spike_result), (out_prod, prod_result)


SCENARIOS = {
    "home visit ΝΑΙ": {"home_visit": {"physiotherapy": True}},
    "home visit ΟΧΙ": {"home_visit": {"physiotherapy": False}},
    "center fields kept": {"ada": None, "representative": None},
}


@pytest.mark.parametrize("scenario", SCENARIOS)
@pytest.mark.parametrize("sample_dates", [False, True])
def test_outputs_are_identical_part_by_part(tmp_path, spike, scenario, sample_dates):
    built = fx.build_eopyy_docx(tmp_path / "t.docx", sample_dates=sample_dates)
    data = make_data(built, **SCENARIOS[scenario])
    _, (out_s, _), (out_p, _) = both(tmp_path, spike, built, data)
    spike_parts, prod_parts = parts(out_s), parts(out_p)
    assert spike_parts.keys() == prod_parts.keys()
    differing = [n for n in spike_parts if spike_parts[n] != prod_parts[n]]
    assert differing == []


def test_change_logs_agree(tmp_path, spike, built, data):
    word_like(built)
    _, (_, spike_result), (_, prod_result) = both(tmp_path, spike, built, data)
    spike_changes = [
        (c.field, c.old, c.new) for c in spike_result.changes if c.field != "watermark"
    ]
    prod_changes = [(c.field, c.old, c.new) for c in prod_result.changes]
    assert spike_changes == prod_changes
    assert any(c.field == "watermark" for c in spike_result.changes)
    assert prod_result.watermark_runs_removed == 1


def test_each_engine_verifies_the_others_output(tmp_path, spike, built, data):
    word_like(built)
    fmap, (out_s, spike_result), (out_p, prod_result) = both(tmp_path, spike, built, data)
    # the spike's checks accept the production output ...
    assert spike.render.verify_preserved(built.path, out_p, spike_result.changes) == []
    assert spike.render.verify_images_untouched(built.path, out_p) == []
    # ... and the production checks accept the spike's output
    assert verify_document(built.path, out_s, prod_result, fmap) == []


def test_template_validation_agrees(tmp_path, spike):
    good = fx.build_eopyy_docx(tmp_path / "good.docx")
    blank = fx.build_eopyy_docx(
        tmp_path / "blank.docx",
        (
            fx.TherapyBlock(
                "speech", "Λογοθεραπεία:", "T1", "\t", authority="\t", license_date="\t"
            ),
        ),
    )
    for built, ok in ((word_like(good), True), (word_like(blank), False)):
        spike_report = spike.validate.validate_template(built.path, spike_field_map(built))
        prod_report = validate_template(built.path, production_field_map(built))
        assert spike_report.ok is ok and prod_report.ok is ok, (
            spike_report.errors,
            prod_report.errors,
        )
    spike_empty = [
        e
        for e in spike.validate.validate_template(blank.path, spike_field_map(blank)).errors
        if "is empty in the template" in e
    ]
    prod_empty = [
        e
        for e in validate_template(blank.path, production_field_map(blank)).errors
        if "is empty in the template" in e
    ]
    assert len(spike_empty) == len(prod_empty) == 3


# ---------------------------------------------------------------- intentional differences
def test_difference_spike_check_trips_over_missing_xml_space(tmp_path, spike, built, data):
    """Spike bug fixed in production: when a mapped text node lacks xml:space="preserve",
    writing a value adds it, and the spike reports its own correct output as a structural
    change (so its dry-run validation would reject such a template). Production ignores
    that attribute, which has no visual effect."""
    fmap, (out_s, spike_result), (out_p, prod_result) = both(tmp_path, spike, built, data)
    assert parts(out_s) == parts(out_p)  # identical output either way
    assert spike.render.verify_preserved(built.path, out_s, spike_result.changes) == [
        "document.xml: structure/formatting differs outside w:t text"
    ]
    assert verify_document(built.path, out_p, prod_result, fmap) == []


def test_difference_dynamic_center_fields_are_required(tmp_path, spike, built):
    """D-25: the spike silently kept the template's ΑΔΑ when none was supplied; production
    only does that for fields mapped as static and otherwise refuses."""
    from app.documents import MissingValue

    data = make_data(built, ada=None)
    spike.render.render(
        built.path, spike_field_map(built), spike_data(spike, data), tmp_path / "spike.docx"
    )
    with pytest.raises(MissingValue, match="ada is a dynamic field"):
        render(built.path, production_field_map(built), data, tmp_path / "production.docx")


def test_difference_production_refuses_dates_outside_the_period(tmp_path, spike, built):
    data = make_data(
        built, sessions={c: [date(2027, 1, 4)] for c in ("speech", "occupational", "physiotherapy")}
    )
    spike.render.render(
        built.path, spike_field_map(built), spike_data(spike, data), tmp_path / "spike.docx"
    )  # the spike prints them
    with pytest.raises(InvalidDocumentData, match="outside the period"):
        render(built.path, production_field_map(built), data, tmp_path / "production.docx")


def test_difference_production_refuses_therapies_missing_from_the_template(tmp_path, spike, built):
    sessions = {
        c: [date(2026, 10, 20)]
        for c in ("speech", "occupational", "physiotherapy", "psychotherapy")
    }
    data = make_data(built, sessions=sessions)
    spike.render.render(
        built.path, spike_field_map(built), spike_data(spike, data), tmp_path / "spike.docx"
    )  # the spike silently ignores psychotherapy
    with pytest.raises(InvalidDocumentData, match="wrong template"):
        render(built.path, production_field_map(built), data, tmp_path / "production.docx")


def test_difference_production_handles_split_runs_and_headers(tmp_path, spike):
    """The spike can only address single runs in the body; production also handles
    fields split across runs and fields in headers/footers."""
    built = fx.build_eopyy_docx(
        tmp_path / "t.docx", split_runs=True, ada_in_header=True, representative_in_footer=True
    )
    out = tmp_path / "production.docx"
    fmap = production_field_map(built)
    result = render(built.path, fmap, make_data(built), out)
    assert verify_document(built.path, out, result, fmap) == []
    with pytest.raises(AssertionError):
        spike_field_map(built)  # not expressible in the spike's map format
