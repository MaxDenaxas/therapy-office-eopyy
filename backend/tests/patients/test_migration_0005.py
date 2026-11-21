"""Migration 0005_patient_guards: one trigger, no table change, reversible, data untouched."""

from __future__ import annotations

import sqlite3

import pytest

from app import migrate
from tests.db.test_engine_and_migrations import names
from tests.patients.amka import synthetic_amka

GUARD = "patients_no_delete"


def insert_patient(path, n: int) -> None:
    con = sqlite3.connect(path)
    try:
        con.execute(
            "INSERT INTO patients (first_name, last_name, father_name, amka) VALUES (?, ?, ?, ?)",
            ("Γεώργιος", "Δοκιμαστικός", "Νικόλαος", synthetic_amka(n)),
        )
        con.commit()
    finally:
        con.close()


def test_0005_adds_exactly_one_trigger_and_no_tables(tmp_path):
    path = tmp_path / "m.db"
    migrate.upgrade(path, "0004")
    tables_before, triggers_before = names(path, "table"), names(path, "trigger")
    migrate.upgrade(path, "0005")
    assert names(path, "table") == tables_before
    assert names(path, "trigger") == triggers_before | {GUARD}
    assert migrate.current_revision(path) == "0005" == migrate.head_revision()


def test_downgrade_removes_only_the_guard_and_keeps_every_row(tmp_path):
    path = tmp_path / "m.db"
    migrate.upgrade(path)
    insert_patient(path, 1)
    migrate.downgrade(path, "0004")
    assert GUARD not in names(path, "trigger")
    migrate.upgrade(path)
    assert GUARD in names(path, "trigger")
    con = sqlite3.connect(path)
    try:
        assert con.execute("SELECT amka FROM patients").fetchall() == [(synthetic_amka(1),)]
    finally:
        con.close()
    assert migrate.check(path) == []


def test_existing_patients_upgrade_cleanly(tmp_path):
    path = tmp_path / "m.db"
    migrate.upgrade(path, "0004")
    insert_patient(path, 1)
    migrate.upgrade(path)
    assert migrate.check(path) == []


@pytest.mark.parametrize("referenced", [False, True])
def test_a_raw_delete_is_refused_with_or_without_references(tmp_path, referenced):
    path = tmp_path / "m.db"
    migrate.upgrade(path)
    insert_patient(path, 1)
    con = sqlite3.connect(path)
    try:
        con.execute("PRAGMA foreign_keys = ON")
        if referenced:
            con.execute(
                "INSERT INTO prescriptions (patient_id, valid_from, valid_to, layout) "
                "VALUES (1, '2025-09-16', '2026-03-15', 'eopyy_ekpy_monthly_v1')"
            )
        with pytest.raises(sqlite3.IntegrityError, match="patients are deactivated, never deleted"):
            con.execute("DELETE FROM patients")
        assert con.execute("SELECT count(*) FROM patients").fetchone() == (1,)
    finally:
        con.close()
