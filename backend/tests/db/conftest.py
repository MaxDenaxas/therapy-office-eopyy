"""Database fixtures. Every test gets its own copy of a freshly migrated database,
created once per session through Alembic (so triggers and seed data are always
the real ones — never ``metadata.create_all``)."""

from __future__ import annotations

import shutil
from contextlib import contextmanager

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import migrate
from app.db import make_engine


@pytest.fixture(scope="session")
def migrated_template(tmp_path_factory):
    path = tmp_path_factory.mktemp("db-template") / "template.db"
    migrate.upgrade(path)  # engine disposed afterwards: WAL checkpointed into the main file
    return path


@pytest.fixture()
def db_path(tmp_path, migrated_template):
    path = tmp_path / "therapy.db"
    shutil.copyfile(migrated_template, path)
    return path


@pytest.fixture()
def engine(db_path):
    eng = make_engine(db_path)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine):
    with Session(engine) as s:
        yield s


@contextmanager
def rejected(session: Session, match: str):
    """The block must fail with an IntegrityError whose message matches; the session is
    rolled back afterwards so the test can continue."""
    with pytest.raises(IntegrityError, match=match):
        yield
        session.flush()
    session.rollback()
