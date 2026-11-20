"""CLI-only administrator setup: create, set-password, status."""

from __future__ import annotations

import io
import json

import pytest
from sqlalchemy import select, text

from app import cli, migrate
from app.auth import passwords, service
from app.models import AdminAccount, AdminSession, AuditLog
from tests.auth.conftest import PASSWORD, USERNAME, login


@pytest.fixture()
def env(monkeypatch, data_dir):
    monkeypatch.setenv("THERAPY_DATA_DIR", str(data_dir))
    from app.config import get_settings

    get_settings.cache_clear()
    return data_dir


def stdin(monkeypatch, value: str) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(value))


def prompts(monkeypatch, *answers: str) -> None:
    it = iter(answers)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(it))


def account(db):
    return db(lambda s: s.get(AdminAccount, 1))


def test_create_with_password_from_stdin(env, monkeypatch, capsys, db):
    stdin(monkeypatch, PASSWORD + "\n")
    assert cli.main(["admin", "create", "--username", USERNAME, "--password-stdin"]) == 0
    out = capsys.readouterr()
    assert PASSWORD not in out.out + out.err
    acc = account(db)
    assert acc.username == USERNAME
    assert passwords.is_argon2id(acc.password_hash)
    assert passwords.verify_password(acc.password_hash, PASSWORD)
    assert acc.failed_login_count == 0 and acc.locked_until is None


def test_create_interactively_asks_twice(env, monkeypatch, db):
    monkeypatch.setattr("builtins.input", lambda prompt="": USERNAME)
    prompts(monkeypatch, PASSWORD, PASSWORD)
    assert cli.main(["admin", "create"]) == 0
    assert account(db).username == USERNAME


def test_create_refuses_mismatched_passwords(env, monkeypatch, capsys, db):
    prompts(monkeypatch, PASSWORD, PASSWORD + "x")
    assert cli.main(["admin", "create", "--username", USERNAME]) == 1
    assert "do not match" in capsys.readouterr().err
    assert account(db) is None


@pytest.mark.parametrize(
    "username, password, message",
    [
        (USERNAME, "too short", "at least 12"),
        (USERNAME, USERNAME, "username"),
        ("bad name", PASSWORD, "username must be"),
    ],
)
def test_create_enforces_the_policy(env, monkeypatch, capsys, db, username, password, message):
    stdin(monkeypatch, password + "\n")
    assert cli.main(["admin", "create", "--username", username, "--password-stdin"]) == 1
    err = capsys.readouterr().err
    assert message in err and password not in err
    assert account(db) is None


def test_only_one_administrator_can_ever_be_created(env, monkeypatch, capsys, admin, db):
    stdin(monkeypatch, "another long password\n")
    assert cli.main(["admin", "create", "--username", "second", "--password-stdin"]) == 1
    assert "already exists" in capsys.readouterr().err
    assert account(db).username == USERNAME


def test_password_is_never_a_command_line_argument(env):
    with pytest.raises(SystemExit):
        cli.main(["admin", "create", "--username", USERNAME, "--password", PASSWORD])


def test_commands_need_a_migrated_database(env, monkeypatch, capsys):
    (env / "therapy.db").unlink()
    for argv in (["admin", "status"], ["admin", "create", "--username", USERNAME]):
        assert cli.main(argv) == 1
        assert "db init" in capsys.readouterr().err
    assert not (env / "therapy.db").exists()  # never created as a side effect


def test_commands_refuse_an_outdated_schema(env, capsys):
    migrate.downgrade(env / "therapy.db", "0002")
    assert cli.main(["admin", "status"]) == 1
    assert "latest revision" in capsys.readouterr().err


def test_set_password_revokes_sessions_and_clears_the_lock(
    env, monkeypatch, capsys, client, admin, db, clock
):
    assert login(client).status_code == 200
    db(lambda s: s.execute(text("UPDATE admin_account SET failed_login_count = 7")))
    new = "a different long passphrase"
    stdin(monkeypatch, new + "\n")
    assert cli.main(["admin", "set-password", "--password-stdin"]) == 0
    out = capsys.readouterr().out
    assert "Sessions ended: 1" in out and new not in out

    acc = account(db)
    assert passwords.verify_password(acc.password_hash, new)
    assert not passwords.verify_password(acc.password_hash, PASSWORD)
    assert acc.failed_login_count == 0 and acc.locked_until is None
    live = db(lambda s: service.active_session_count(s, clock()))
    assert live == 0
    assert client.get("/api/auth/session").json() == {"authenticated": False}
    assert login(client).status_code == 401
    assert login(client, password=new).status_code == 200

    actions = db(lambda s: s.execute(select(AuditLog.action, AuditLog.details)).all())
    assert ("admin.password_changed", None) in actions
    assert ("auth.sessions_revoked", json.dumps({"count": 1})) in actions


def test_set_password_enforces_the_policy(env, monkeypatch, capsys, admin, db):
    stdin(monkeypatch, USERNAME + "\n")
    assert cli.main(["admin", "set-password", "--password-stdin"]) == 1
    assert passwords.verify_password(account(db).password_hash, PASSWORD)


def test_set_password_without_an_account(env, monkeypatch, capsys):
    stdin(monkeypatch, PASSWORD + "\n")
    assert cli.main(["admin", "set-password", "--password-stdin"]) == 1
    assert "admin create" in capsys.readouterr().err


def test_status_never_shows_the_hash(env, capsys, client, admin, db):
    assert cli.main(["admin", "status"]) == 0
    out = capsys.readouterr().out
    assert "administrator:   exists" in out and "argon2id" in out
    assert account(db).password_hash not in out
    assert "$argon2" not in out and PASSWORD not in out


def test_status_without_an_account(env, capsys):
    assert cli.main(["admin", "status"]) == 0
    assert "none" in capsys.readouterr().out


def test_no_default_account_after_init(monkeypatch, tmp_path):
    monkeypatch.setenv("THERAPY_DATA_DIR", str(tmp_path / "fresh"))
    from app.config import get_settings

    get_settings.cache_clear()
    assert cli.main(["db", "init"]) == 0
    from app.db import make_engine

    engine = make_engine(tmp_path / "fresh" / "therapy.db")
    try:
        with engine.connect() as conn:
            assert conn.execute(select(AdminAccount)).first() is None
            assert conn.execute(select(AdminSession)).first() is None
    finally:
        engine.dispose()
