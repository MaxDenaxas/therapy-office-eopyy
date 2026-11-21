# ruff: noqa: F811  (imported fixtures are re-used as parameters, the pytest idiom)
"""Fixtures for patient tests (M5.2). All data is invented; AMKAs come from ``synthetic_amka``.

Two families, as in tests/reference:
  * service and database tests use ``session`` (a freshly migrated database);
  * API tests use ``client`` + ``logged_in`` (the application's own database; ``db`` opens a
    second connection to it).
"""

from __future__ import annotations

import pytest

from tests.auth.conftest import (  # noqa: F401
    PASSWORD,
    USERNAME,
    admin,
    app,
    auth_db_template,
    client,
    clock,
    data_dir,
    db,
    factory,
    logged_in,
    login,
    settings,
    unsafe_headers,
)
from tests.db.conftest import db_path, engine, migrated_template, session  # noqa: F401
from tests.patients.amka import synthetic_amka

PATIENTS = "/api/patients"


def person(n: int = 1, **kw) -> dict:
    """A valid create body (invented Greek names)."""
    return {
        "first_name": "Γεώργιος",
        "last_name": "Δοκιμαστικός",
        "father_name": "Νικόλαος",
        "amka": synthetic_amka(n),
        **kw,
    }


@pytest.fixture()
def create(client, logged_in):
    """``create(n, **fields)`` → the created item (asserts 201)."""

    def make(n: int = 1, **kw) -> dict:
        r = client.post(PATIENTS, json=person(n, **kw), headers=unsafe_headers(logged_in))
        assert r.status_code == 201, r.text
        return r.json()["item"]

    return make
