"""Programmatic database initialisation and migration (used by the CLI and tests)."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from app.db import make_engine

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"


def alembic_config(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.attributes["db_path"] = Path(db_path)
    cfg.attributes["skip_logging_config"] = True
    return cfg


def head_revision() -> str:
    head = ScriptDirectory.from_config(alembic_config(Path("unused.db"))).get_current_head()
    assert head is not None, "no migrations found"
    return head


def current_revision(db_path: Path) -> str | None:
    if not Path(db_path).exists():
        return None
    engine = make_engine(db_path)
    try:
        with engine.connect() as conn:
            return MigrationContext.configure(conn).get_current_revision()
    finally:
        engine.dispose()


def upgrade(db_path: Path, revision: str = "head") -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(db_path), revision)


def downgrade(db_path: Path, revision: str) -> None:
    command.downgrade(alembic_config(db_path), revision)


def check(db_path: Path) -> list[str]:
    """Integrity problems (empty list = healthy): SQLite integrity + foreign keys + revision."""
    problems: list[str] = []
    if not Path(db_path).exists():
        return [f"database not found: {db_path}"]
    engine = make_engine(db_path)
    try:
        with engine.connect() as conn:
            result = [r[0] for r in conn.exec_driver_sql("PRAGMA integrity_check").fetchall()]
            if result != ["ok"]:
                problems += [f"integrity_check: {r}" for r in result]
            for row in conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall():
                problems.append(f"foreign key violation: {tuple(row)}")
            current = MigrationContext.configure(conn).get_current_revision()
    finally:
        engine.dispose()
    if current != head_revision():
        problems.append(f"schema revision {current} is not the latest ({head_revision()})")
    return problems
