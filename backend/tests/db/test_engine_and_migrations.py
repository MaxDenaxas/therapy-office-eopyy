"""SQLite configuration, Alembic migrations, initialisation commands."""

from __future__ import annotations

import sqlite3

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app import cli, migrate
from app.db import make_engine
from app.models import Base

EXPECTED_TABLES = {
    "admin_account",
    "audit_log",
    "center_settings",
    "document_templates",
    "generated_documents",
    "holiday_overrides",
    "patients",
    "period_therapies",
    "periods",
    "prescription_items",
    "prescriptions",
    "referral_periods",
    "scheduled_sessions",
    "template_configuration_items",
    "template_versions",
    "therapist_assignments",
    "therapists",
    "therapy_types",
}

EXPECTED_TRIGGERS = {
    "referral_periods_inside_validity_insert",
    "referral_periods_inside_validity_update",
    "referral_periods_no_overlap_insert",
    "referral_periods_no_overlap_update",
    "referral_periods_locked_when_used",
    "prescription_items_locked_when_used",
    "prescriptions_locked_when_used",
    "prescriptions_validity_covers_referrals",
    "period_therapies_check_insert",
    "period_therapies_links_fixed",
    "period_therapies_locked_update",
    "period_therapies_locked_delete",
    "scheduled_sessions_check_insert",
    "scheduled_sessions_check_update",
    "scheduled_sessions_locked_delete",
    "periods_window_fixed_when_used",
    "periods_insert_not_issued",
    "periods_issue_requires_document",
    "generated_documents_check_insert",
    "generated_documents_no_update",
    "generated_documents_no_delete",
    "template_versions_no_update",
    "template_versions_no_delete",
    "document_templates_current_version_insert",
    "document_templates_current_version_update",
    "document_templates_active_needs_version_insert",
    "document_templates_active_needs_version_update",
    "document_templates_configuration_frozen",
    "template_configuration_items_frozen_insert",
    "template_configuration_items_frozen_update",
    "template_configuration_items_frozen_delete",
    "audit_log_no_update",
    "audit_log_no_delete",
    "center_settings_no_delete",
}


def names(path, kind: str) -> set[str]:
    con = sqlite3.connect(path)
    try:
        rows = con.execute(
            "SELECT name FROM sqlite_master WHERE type = ? AND name NOT LIKE 'sqlite_%'", (kind,)
        ).fetchall()
    finally:
        con.close()
    return {r[0] for r in rows}


# ---------------------------------------------------------------- pragmas / transactions
def test_every_connection_gets_the_pragmas(engine):
    for _ in range(2):  # two pooled connections, same settings
        with engine.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
            assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
            assert conn.exec_driver_sql("PRAGMA synchronous").scalar() == 2  # FULL
            assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar() == 5000


def test_transactions_take_the_write_lock_immediately(engine, db_path):
    with engine.connect() as conn:
        conn.begin()  # BEGIN IMMEDIATE
        conn.execute(text("SELECT 1"))
        other = make_engine(db_path)
        try:
            with other.connect() as conn2:
                conn2.connection.driver_connection.execute("PRAGMA busy_timeout = 0")
                with pytest.raises(OperationalError, match="locked"):
                    conn2.begin()
                    conn2.execute(text("SELECT 1"))
        finally:
            other.dispose()
        conn.rollback()


def test_rollback_on_error_leaves_no_partial_writes(session):
    from tests.db import factories as f

    f.patient(session, amka="11111111111")
    session.rollback()
    assert session.execute(text("SELECT count(*) FROM patients")).scalar() == 0


# ---------------------------------------------------------------- migrations
def test_fresh_database_has_full_schema(db_path):
    assert names(db_path, "table") == EXPECTED_TABLES | {"alembic_version"}
    assert names(db_path, "trigger") == EXPECTED_TRIGGERS
    assert migrate.current_revision(db_path) == migrate.head_revision()


def test_settings_row_is_seeded(session):
    row = session.execute(text("SELECT * FROM center_settings")).mappings().one()
    assert row["id"] == 1
    assert (
        row["max_therapists_per_day"],
        row["max_sessions_per_therapy_per_day"],
        row["prefer_gap_days"],
    ) == (2, 1, 1)
    assert row["ada"] is None and row["representative_name"] is None


def test_models_and_migration_do_not_drift(engine):
    with engine.connect() as conn:
        diffs = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diffs == []


def test_every_model_check_constraint_exists_in_the_database(engine):
    """Alembic's drift check ignores CHECK constraints, so compare them explicitly."""
    from sqlalchemy import CheckConstraint

    with engine.connect() as conn:
        ddl = dict(
            conn.exec_driver_sql(
                "SELECT name, sql FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        )
    missing = []
    for table in Base.metadata.tables.values():
        for c in table.constraints:
            if isinstance(c, CheckConstraint) and str(c.sqltext) not in ddl[table.name]:
                missing.append(f"{table.name}: {c.sqltext}")
    assert missing == []


def test_downgrade_and_upgrade_round_trip(tmp_path):
    path = tmp_path / "rt.db"
    migrate.upgrade(path)
    migrate.downgrade(path, "base")
    assert names(path, "table") == {"alembic_version"}
    assert names(path, "trigger") == set()
    migrate.upgrade(path)
    assert names(path, "trigger") == EXPECTED_TRIGGERS
    assert migrate.check(path) == []


def test_upgrade_is_idempotent(tmp_path):
    path = tmp_path / "idem.db"
    migrate.upgrade(path)
    migrate.upgrade(path)
    assert migrate.check(path) == []


def test_check_reports_problems(tmp_path):
    assert migrate.check(tmp_path / "missing.db") == [
        f"database not found: {tmp_path / 'missing.db'}"
    ]
    path = tmp_path / "old.db"
    migrate.upgrade(path)
    migrate.downgrade(path, "base")
    assert any("not the latest" in p for p in migrate.check(path))


# ---------------------------------------------------------------- CLI
def test_cli_init_status_check_upgrade(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("THERAPY_DATA_DIR", str(tmp_path / "data"))
    from app.config import get_settings

    get_settings.cache_clear()
    db = tmp_path / "data" / "therapy.db"

    assert cli.main(["db", "upgrade"]) == 1  # nothing to upgrade yet
    assert cli.main(["db", "init"]) == 0
    assert db.exists() and "Created database" in capsys.readouterr().out
    assert cli.main(["db", "init"]) == 1  # refuses to touch an existing database
    assert "already exists" in capsys.readouterr().err
    assert cli.main(["db", "status"]) == 0
    out = capsys.readouterr().out
    assert f"current:  {migrate.head_revision()}" in out and "exists:   True" in out
    assert cli.main(["db", "check"]) == 0
    assert cli.main(["db", "upgrade"]) == 0
    assert "already at the latest revision" in capsys.readouterr().out


def test_cli_refuses_data_dir_inside_repository(monkeypatch):
    from pydantic import ValidationError

    from app.config import get_settings
    from tests.conftest import REPO_ROOT

    monkeypatch.setenv("THERAPY_DATA_DIR", str(REPO_ROOT / "data"))
    get_settings.cache_clear()
    with pytest.raises(ValidationError, match="inside a Git repository"):
        cli.main(["db", "init"])
    assert not (REPO_ROOT / "data").exists()


def test_failed_migration_rolls_back_completely(tmp_path):
    """A migration that fails at its very last statement must leave nothing behind."""
    import shutil

    from alembic import command

    scripts = tmp_path / "migrations"
    shutil.copytree(migrate.MIGRATIONS_DIR, scripts, ignore=shutil.ignore_patterns("__pycache__"))
    version_file = scripts / "versions" / "0001_initial_schema.py"
    source = version_file.read_text(encoding="utf-8")
    marker = 'op.execute("INSERT INTO center_settings (id) VALUES (1)")'
    assert marker in source
    broken = source.replace(marker, marker + '\n    op.execute("THIS IS NOT SQL")')
    version_file.write_text(broken, encoding="utf-8")
    path = tmp_path / "fail.db"
    cfg = migrate.alembic_config(path)
    cfg.set_main_option("script_location", str(scripts))
    with pytest.raises(OperationalError, match="syntax error"):
        command.upgrade(cfg, "head")
    assert names(path, "table") == set()  # not even alembic_version
    assert names(path, "trigger") == set()
