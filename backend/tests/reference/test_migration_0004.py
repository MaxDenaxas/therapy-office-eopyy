"""Migration 0004_reference_data_guards: triggers only, reversible, data untouched."""

from __future__ import annotations

import sqlite3

from app import migrate
from tests.db.test_engine_and_migrations import names

GUARDS = {
    "therapy_types_code_frozen",
    "therapy_types_keep_active_templates",
    "therapists_keep_active_templates",
    "therapists_identity_frozen",
}


def test_0004_adds_exactly_four_triggers_and_no_tables(tmp_path):
    path = tmp_path / "m.db"
    migrate.upgrade(path, "0003")
    tables_before, triggers_before = names(path, "table"), names(path, "trigger")
    migrate.upgrade(path, "0004")
    assert names(path, "table") == tables_before  # no table was added or rebuilt
    assert names(path, "trigger") == triggers_before | GUARDS
    assert migrate.current_revision(path) == "0004"


def test_downgrade_removes_only_the_guards_and_keeps_every_row(tmp_path):
    path = tmp_path / "m.db"
    migrate.upgrade(path)
    con = sqlite3.connect(path)
    con.execute("INSERT INTO therapy_types (code, name_el, template_label) VALUES ('a', 'Α', 'Α:')")
    con.execute("INSERT INTO therapists (full_name) VALUES ('Θεραπεύτρια Δοκιμής')")
    con.commit()
    con.close()

    migrate.downgrade(path, "0003")
    assert not (names(path, "trigger") & GUARDS)
    migrate.upgrade(path)  # and back again
    assert GUARDS <= names(path, "trigger")
    con = sqlite3.connect(path)
    try:
        assert con.execute("SELECT code FROM therapy_types").fetchall() == [("a",)]
        assert con.execute("SELECT full_name FROM therapists").fetchall() == [
            ("Θεραπεύτρια Δοκιμής",)
        ]
    finally:
        con.close()
    assert migrate.check(path) == []


def test_a_database_with_active_data_upgrades_cleanly(tmp_path):
    """Existing rows are never re-validated by the new triggers (they only fire on UPDATE)."""
    path = tmp_path / "m.db"
    migrate.upgrade(path, "0003")
    con = sqlite3.connect(path)
    con.execute("INSERT INTO therapy_types (code, name_el, template_label) VALUES ('a', 'Α', 'Α:')")
    con.execute("INSERT INTO therapists (full_name, active) VALUES ('Θεραπεύτρια Δοκιμής', 0)")
    con.commit()
    con.close()
    migrate.upgrade(path, "0004")
    assert migrate.check(path) == []
