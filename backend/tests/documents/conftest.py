"""Shared fixtures for document-engine tests. Everything is synthetic and built in code."""

from __future__ import annotations

from datetime import date

import pytest

from app.documents import DocumentData, LibreOfficeNotFound, find_soffice
from fixtures.eopyy_builder import build_eopyy_docx
from fixtures.eopyy_maps import production_field_map

PERIOD = (date(2026, 10, 16), date(2026, 11, 15))
DATES = [date(2026, 10, d) for d in (16, 20, 23, 27, 30)] + [date(2026, 11, d) for d in (3, 6, 10)]

# Invented values. The AMKA is a documented fake accepted by the repository guard.
PATIENT = {
    "first_name": "ΓΕΩΡΓΙΟΣ",
    "last_name": "ΔΟΚΙΜΑΣΤΙΚΟΣ",
    "father_name": "ΝΙΚΟΛΑΟΥ",
    "amka": "12345678901",
}


def make_data(built, **overrides) -> DocumentData:
    values = {
        **PATIENT,
        "period_start": PERIOD[0],
        "period_end": PERIOD[1],
        "issue_date": date(2026, 11, 20),
        "sessions": {b.code: DATES for b in built.blocks},
        "home_visit": {b.code: True for b in built.blocks if b.has_home_visit},
        "ada": "ΨΨ00-ΤΕΣΤ",
        "representative": "Δοκιμαστική Εκπρόσωπος Κέντρου",
    }
    return DocumentData(**{**values, **overrides})


@pytest.fixture()
def built(tmp_path):
    return build_eopyy_docx(tmp_path / "template.docx")


@pytest.fixture()
def fmap(built):
    return production_field_map(built)


@pytest.fixture()
def data(built):
    return make_data(built)


@pytest.fixture(scope="session")
def soffice():
    try:
        return find_soffice()
    except LibreOfficeNotFound:
        pytest.skip("LibreOffice is not installed")
