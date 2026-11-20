"""Reuse the migrated-database fixtures of the M1 tests."""

from tests.db.conftest import db_path, engine, migrated_template, session  # noqa: F401
