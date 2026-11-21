# ruff: noqa: F811  (imported fixtures are re-used as parameters, the pytest idiom)
"""Fixtures for therapy-type / therapist tests (M5.1).

Two families, both synthetic:
  * service/database tests use ``session`` (a migrated database) with the template fixtures
    ``ref``, ``store`` and ``upload`` from tests/templates;
  * API tests use ``client`` + ``logged_in`` (the application's own database, ``db`` / ``factory``
    open a second connection to it) and ``active_center``, which registers and activates a real
    synthetic template there.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from app.models import CenterSettings, Therapist, TherapyType
from app.services.templates import activate_template, add_version, register_template
from app.storage import FileStore
from fixtures.eopyy_builder import DEFAULT_BLOCKS, build_eopyy_docx
from fixtures.eopyy_maps import production_field_map
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
from tests.templates.conftest import ref, soffice, store, upload  # noqa: F401


def seed_reference(session) -> SimpleNamespace:
    """The fixture template's therapies and therapists (all invented), like ``ref``."""
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
    settings_row = session.get(CenterSettings, 1)
    settings_row.ada, settings_row.representative_name = "ΤΕΣΤ-0000", "Δοκιμαστική Εκπρόσωπος"
    session.flush()
    return SimpleNamespace(types=types, therapists=therapists)


def activate_center_template(
    session, store, upload, ref, codes=("speech", "occupational", "physiotherapy")
) -> int:
    """Register, version and activate one template; returns its id. The fixture upload has three
    sections, so the template must be configured for exactly these three therapies."""
    pairs = [(ref.types[c].id, ref.therapists[c].id) for c in codes]
    template = register_template(session, "Δοκιμαστικό πρότυπο " + "+".join(codes), pairs).template
    add_version(session, store, template.id, upload.path, production_field_map(upload))
    activate_template(session, store, template.id)
    return template.id


@pytest.fixture()
def active_center(factory, data_dir, tmp_path):
    """In the application's database: three therapy types and three therapists, and one active
    template that uses all three pairs. Returns their ids."""
    from app.db import session_scope

    upload_file = build_eopyy_docx(tmp_path / "uploads" / "template.docx")
    file_store = FileStore(data_dir)
    with session_scope(factory) as s:
        center = seed_reference(s)
        template_id = activate_center_template(s, file_store, upload_file, center)
        return SimpleNamespace(
            template_id=template_id,
            type_ids={c: t.id for c, t in center.types.items()},
            therapist_ids={c: t.id for c, t in center.therapists.items()},
        )
