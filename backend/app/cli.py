"""Command line.

    python -m app.cli db init      create a fresh database (refuses if one already exists)
    python -m app.cli db upgrade   apply pending migrations to the existing database
    python -m app.cli db status    show the database path and current / latest schema revision
    python -m app.cli db check     integrity check, foreign-key check and revision check

The database lives at THERAPY_DATA_DIR/therapy.db (outside the repository).
"""

from __future__ import annotations

import argparse
import sys

from app import migrate
from app.config import get_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="group", required=True)
    db = sub.add_parser("db", help="database commands")
    db.add_argument("action", choices=["init", "upgrade", "status", "check"])
    args = parser.parse_args(argv)

    path = get_settings().database_path
    if args.action == "init":
        if path.exists():
            print(
                f"A database already exists at {path}. Use 'db upgrade' to migrate it.",
                file=sys.stderr,
            )
            return 1
        migrate.upgrade(path)
        print(f"Created database at {path} (revision {migrate.current_revision(path)}).")
        return 0
    if args.action == "upgrade":
        if not path.exists():
            print(f"No database at {path}. Use 'db init' first.", file=sys.stderr)
            return 1
        before = migrate.current_revision(path)
        migrate.upgrade(path)
        after = migrate.current_revision(path)
        print(
            f"Upgraded {path}: {before} -> {after}"
            if before != after
            else f"{path} is already at the latest revision ({after})."
        )
        return 0
    if args.action == "status":
        print(f"database: {path}")
        print(f"exists:   {path.exists()}")
        print(f"current:  {migrate.current_revision(path)}")
        print(f"latest:   {migrate.head_revision()}")
        return 0
    problems = migrate.check(path)
    if problems:
        print("Database check FAILED:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print(f"Database check passed: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
