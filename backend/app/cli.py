"""Command line.

    python -m app.cli db init      create a fresh database (refuses if one already exists)
    python -m app.cli db upgrade   apply pending migrations to the existing database
    python -m app.cli db status    show the database path and current / latest schema revision
    python -m app.cli db check     integrity check, foreign-key check and revision check

    python -m app.cli admin create [--username NAME] [--password-stdin]
                                   create the single administrator (refuses if one exists)
    python -m app.cli admin set-password [--password-stdin]
                                   new password; clears the lock and ends every session
    python -m app.cli admin status show the account state (never the password hash)

    python -m app.cli serve [--reload]
                                   run the application on THERAPY_HOST:THERAPY_PORT
                                   (loopback only)

The database lives at THERAPY_DATA_DIR/therapy.db (outside the repository).
The administrator can only be created or changed here, never through the web API.
Passwords are read without echo (asked twice), or as one line from standard
input with --password-stdin; they are never accepted as command-line arguments.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from datetime import UTC, datetime

from app import migrate
from app.config import get_settings


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="group", required=True)
    db = sub.add_parser("db", help="database commands")
    db.add_argument("action", choices=["init", "upgrade", "status", "check"])

    admin = sub.add_parser("admin", help="administrator account (CLI only)")
    admin_sub = admin.add_subparsers(dest="action", required=True)
    create = admin_sub.add_parser("create", help="create the administrator")
    create.add_argument("--username")
    create.add_argument("--password-stdin", action="store_true")
    set_pw = admin_sub.add_parser("set-password", help="change the password")
    set_pw.add_argument("--password-stdin", action="store_true")
    admin_sub.add_parser("status", help="show the account state")

    serve = sub.add_parser("serve", help="run the web application (loopback only)")
    serve.add_argument("--reload", action="store_true")

    args = parser.parse_args(argv)
    if args.group == "db":
        return _db(args.action)
    if args.group == "admin":
        return _admin(args)
    return _serve(args.reload)


# ---------------------------------------------------------------- db
def _db(action: str) -> int:
    path = get_settings().database_path
    if action == "init":
        if path.exists():
            print(
                f"A database already exists at {path}. Use 'db upgrade' to migrate it.",
                file=sys.stderr,
            )
            return 1
        migrate.upgrade(path)
        print(f"Created database at {path} (revision {migrate.current_revision(path)}).")
        return 0
    if action == "upgrade":
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
    if action == "status":
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


# ---------------------------------------------------------------- admin
def _read_password(from_stdin: bool) -> str | None:
    if from_stdin:
        line = sys.stdin.readline()
        return line.rstrip("\r\n")
    first = getpass.getpass("New password: ")
    second = getpass.getpass("Repeat password: ")
    if first != second:
        print("The passwords do not match.", file=sys.stderr)
        return None
    return first


def _admin(args: argparse.Namespace) -> int:
    from app.auth import passwords, service
    from app.db import make_engine, make_session_factory, session_scope

    path = get_settings().database_path
    if not path.exists() or migrate.current_revision(path) != migrate.head_revision():
        print(
            "The database is missing or not at the latest revision. "
            "Run 'db init' or 'db upgrade' first.",
            file=sys.stderr,
        )
        return 1
    engine = make_engine(path)
    factory = make_session_factory(engine)
    try:
        if args.action == "status":
            return _admin_status(factory, service, passwords)
        if args.action == "create":
            with session_scope(factory) as db:
                if service.get_account(db) is not None:
                    print("An administrator account already exists.", file=sys.stderr)
                    return 1
            username = args.username if args.username is not None else input("Username: ")
            try:
                passwords.check_username(username)
            except passwords.PolicyError as exc:
                print(f"Refused: {exc}.", file=sys.stderr)
                return 1
            password = _read_password(args.password_stdin)
            if password is None:
                return 1
            try:
                with session_scope(factory) as db:
                    service.create_account(db, username, password, _now())
            except (passwords.PolicyError, service.AccountExists) as exc:
                print(f"Refused: {exc}.", file=sys.stderr)
                return 1
            print("Administrator account created.")
            return 0
        # set-password
        with session_scope(factory) as db:
            if service.get_account(db) is None:
                print("No administrator account exists. Use 'admin create'.", file=sys.stderr)
                return 1
        password = _read_password(args.password_stdin)
        if password is None:
            return 1
        try:
            with session_scope(factory) as db:
                revoked = service.set_password(db, password, _now())
        except (passwords.PolicyError, service.NoAccount) as exc:
            print(f"Refused: {exc}.", file=sys.stderr)
            return 1
        print(f"Password changed. Sessions ended: {revoked}.")
        return 0
    finally:
        engine.dispose()


def _admin_status(factory, service, passwords) -> int:
    from app.db import session_scope

    now = _now()
    with session_scope(factory) as db:
        account = service.get_account(db)
        if account is None:
            print("administrator:   none (create one with 'admin create')")
            return 0
        locked = account.locked_until is not None and account.locked_until > now
        algorithm = "argon2id" if passwords.is_argon2id(account.password_hash) else "UNKNOWN"
        last = account.last_login_at
        print("administrator:   exists")
        print(f"username:        {account.username}")
        print(f"password hash:   {algorithm}")
        print(f"created:         {account.created_at:%Y-%m-%d %H:%M} UTC")
        print(f"last login:      {f'{last:%Y-%m-%d %H:%M} UTC' if last else 'never'}")
        print(f"failed logins:   {account.failed_login_count}")
        until = f"{account.locked_until:%Y-%m-%d %H:%M:%S} UTC" if locked else "not locked"
        print(f"locked until:    {until}")
        print(f"active sessions: {service.active_session_count(db, now)}")
    return 0


# ---------------------------------------------------------------- serve
def _serve(reload: bool) -> int:
    import uvicorn

    settings = get_settings()  # validated: host is a loopback address
    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        reload=reload,
        log_level=settings.log_level.lower(),
        proxy_headers=False,  # no proxy in front: never trust X-Forwarded-*
        server_header=False,
        access_log=False,  # request lines include query strings: never log them (M5.2)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
