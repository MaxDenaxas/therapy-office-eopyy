"""Fixtures for template-library tests: a migrated database, a file store outside the
repository, and reference data matching the synthetic template (all invented)."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from app.models import CenterSettings, Therapist, TherapyType
from app.storage import FileStore
from fixtures.eopyy_builder import DEFAULT_BLOCKS, build_eopyy_docx
from tests.db.conftest import db_path, engine, migrated_template, session  # noqa: F401


@pytest.fixture()
def store(tmp_path):
    return FileStore(tmp_path / "data")


@pytest.fixture()
def ref(session):  # noqa: F811 - the fixture imported above
    """Therapy types and therapists for the fixture's sections, licence numbers as printed."""
    types, therapists = {}, {}
    for block in DEFAULT_BLOCKS:
        tt = TherapyType(
            code=block.code,
            name_el=block.label.rstrip(":"),
            template_label=block.label,
            supports_home_visit=block.has_home_visit,
        )
        th = Therapist(
            full_name=block.full_name,
            license_no=block.license_no,
            license_authority=block.authority,
            license_date=date(*reversed([int(x) for x in block.license_date.split("/")])),
        )
        session.add_all([tt, th])
        session.flush()
        types[block.code], therapists[block.code] = tt, th
    settings = session.get(CenterSettings, 1)
    settings.ada, settings.representative_name = "ΤΕΣΤ-0000", "Δοκιμαστική Εκπρόσωπος"
    session.commit()
    return SimpleNamespace(types=types, therapists=therapists)


def pairs_for(ref, codes=("speech", "occupational", "physiotherapy")):
    return [(ref.types[c].id, ref.therapists[c].id) for c in codes]


@pytest.fixture()
def upload(tmp_path):
    """The administrator's upload (a synthetic template outside the store)."""
    return build_eopyy_docx(tmp_path / "uploads" / "Λογοθεραπεία-Εργο-Φυσικο.docx")


@pytest.fixture(scope="session")
def soffice():
    from app.documents import LibreOfficeNotFound, find_soffice

    try:
        return find_soffice()
    except LibreOfficeNotFound:
        pytest.skip("LibreOffice is not installed")
