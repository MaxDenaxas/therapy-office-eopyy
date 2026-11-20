"""Administrator account, login with progressive backoff, and server-side sessions.

Pure database logic: no HTTP here. The caller owns the transaction and passes
``now`` (naive UTC) so every time-dependent rule is testable.

Tokens
  session token  32 random bytes, base64url (43 characters); lives only in the cookie
  CSRF token     HMAC-SHA256(session token, label), base64url; derived, so the session
                 endpoint can hand it out again after a page reload
  stored         SHA-256 hex of each, never the tokens themselves

Backoff (counted on the single account, whatever username was typed, so a
wrong username and a wrong password are indistinguishable):
  failures 1–4  no delay
  failure n ≥ 5 locked for min(30 s × 2^(n−5), 15 min)
  while locked  refused without checking the password; success resets everything
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from app.models import AdminAccount, AdminSession, AuditLog

from . import passwords

ADMIN_ID = 1
IDLE_TIMEOUT = timedelta(minutes=30)
ABSOLUTE_LIFETIME = timedelta(hours=12)
TOUCH_INTERVAL = timedelta(seconds=60)  # last_seen_at is written at most once a minute
MAX_ACTIVE_SESSIONS = 10
LOCK_THRESHOLD = 5
LOCK_BASE_SECONDS = 30
LOCK_MAX_SECONDS = 15 * 60

_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")
_CSRF_LABEL = b"therapy-office csrf v1"


class AccountExists(Exception):
    pass


class NoAccount(Exception):
    pass


# ---------------------------------------------------------------- tokens
def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_token_for(session_token: str) -> str:
    mac = hmac.new(session_token.encode("ascii"), _CSRF_LABEL, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode("ascii")


def well_formed(token: str | None) -> bool:
    return token is not None and _TOKEN_RE.fullmatch(token) is not None


def csrf_matches(stored_hash: str, presented: str | None) -> bool:
    if not well_formed(presented):
        return False
    return hmac.compare_digest(sha256_hex(presented), stored_hash)


# ---------------------------------------------------------------- audit (ids only)
def _audit(db: Session, action: str, **details) -> None:
    db.add(
        AuditLog(
            action=action,
            entity="admin_account",
            entity_id=ADMIN_ID,
            details=json.dumps(details, sort_keys=True) if details else None,
        )
    )


# ---------------------------------------------------------------- account (CLI only)
def get_account(db: Session) -> AdminAccount | None:
    return db.get(AdminAccount, ADMIN_ID)


def create_account(db: Session, username: str, password: str, now: datetime) -> AdminAccount:
    """The only way an administrator comes into existence (called by the CLI)."""
    username = passwords.check_username(username)
    password = passwords.check_password(password, username)
    if get_account(db) is not None:
        raise AccountExists("an administrator account already exists")
    account = AdminAccount(
        id=ADMIN_ID,
        username=username,
        password_hash=passwords.hash_password(password),
        failed_login_count=0,
        created_at=now,
    )
    db.add(account)
    db.flush()
    _audit(db, "admin.created")
    return account


def set_password(db: Session, password: str, now: datetime) -> int:
    """New password, lock cleared, every session revoked. Returns the revoked count."""
    account = get_account(db)
    if account is None:
        raise NoAccount("no administrator account exists")
    password = passwords.check_password(password, account.username)
    account.password_hash = passwords.hash_password(password)
    account.failed_login_count = 0
    account.locked_until = None
    revoked = revoke_all_sessions(db, now)
    _audit(db, "admin.password_changed")
    if revoked:
        _audit(db, "auth.sessions_revoked", count=revoked)
    cleanup_sessions(db, now)
    return revoked


# ---------------------------------------------------------------- login
@dataclass(frozen=True)
class LoginSuccess:
    session_token: str
    csrf_token: str
    expires_at: datetime
    session_id: int


@dataclass(frozen=True)
class LoginFailed:
    pass


@dataclass(frozen=True)
class LoginLocked:
    retry_after_seconds: int


LoginResult = LoginSuccess | LoginFailed | LoginLocked


def lock_seconds(failures: int) -> int:
    if failures < LOCK_THRESHOLD:
        return 0
    return min(LOCK_BASE_SECONDS * 2 ** min(failures - LOCK_THRESHOLD, 16), LOCK_MAX_SECONDS)


def login(
    db: Session,
    username: str,
    password: str,
    now: datetime,
    presented_token: str | None = None,
) -> LoginResult:
    """Check the credentials; on success replace ``presented_token``'s session by a new one.

    Everything happens in the caller's single transaction, and the presented session is
    touched only *after* the password was verified: a failed or locked attempt leaves it
    alone, and if anything below fails the whole transaction (revocation included) rolls
    back. The presented session is revoked *before* the session cap is enforced, so a
    re-login replaces it instead of also evicting an unrelated live session."""
    account = get_account(db)
    if account is None:  # no administrator yet: nothing can ever succeed
        passwords.dummy_verify(password)
        return LoginFailed()
    if account.locked_until is not None and now < account.locked_until:
        remaining = (account.locked_until - now).total_seconds()
        return LoginLocked(retry_after_seconds=max(1, math.ceil(remaining)))

    name_ok = hmac.compare_digest(username.encode("utf-8"), account.username.encode("utf-8"))
    if name_ok:
        password_ok = passwords.verify_password(account.password_hash, password)
    else:
        passwords.dummy_verify(password)  # same cost as a real check
        password_ok = False

    if not password_ok:
        account.failed_login_count += 1
        seconds = lock_seconds(account.failed_login_count)
        if seconds:
            account.locked_until = now + timedelta(seconds=seconds)
            _audit(
                db,
                "auth.lockout",
                failed_login_count=account.failed_login_count,
                locked_seconds=seconds,
            )
        return LoginFailed()

    account.failed_login_count = 0
    account.locked_until = None
    account.last_login_at = now
    if passwords.needs_rehash(account.password_hash):
        account.password_hash = passwords.hash_password(password)
    revoke_token(db, presented_token, now)  # no session fixation; frees its slot in the cap
    cleanup_sessions(db, now)
    _enforce_session_cap(db, now)
    token = new_session_token()
    csrf = csrf_token_for(token)
    row = AdminSession(
        admin_id=ADMIN_ID,
        token_hash=sha256_hex(token),
        csrf_hash=sha256_hex(csrf),
        created_at=now,
        last_seen_at=now,
        expires_at=now + ABSOLUTE_LIFETIME,
    )
    db.add(row)
    db.flush()
    _audit(db, "auth.login", session_id=row.id)
    return LoginSuccess(token, csrf, row.expires_at, row.id)


# ---------------------------------------------------------------- sessions
@dataclass(frozen=True)
class AuthContext:
    admin_id: int
    session_id: int
    csrf_hash: str
    expires_at: datetime


def _active(now: datetime):
    return (
        AdminSession.revoked_at.is_(None),
        AdminSession.expires_at > now,
        AdminSession.last_seen_at > now - IDLE_TIMEOUT,
    )


def check_session(db: Session, token: str | None, now: datetime) -> tuple[AuthContext, bool] | None:
    """Read-only: the live session for ``token`` and whether its ``last_seen_at`` is due for
    a refresh, or None. Every check (revocation, absolute and idle expiry, owner) happens
    here; this function never writes."""
    if not well_formed(token):
        return None
    row = db.execute(
        select(AdminSession).where(AdminSession.token_hash == sha256_hex(token))
    ).scalar_one_or_none()
    if row is None or row.revoked_at is not None:
        return None
    if now >= row.expires_at:  # absolute lifetime
        return None
    if now >= row.last_seen_at + IDLE_TIMEOUT:  # idle timeout, checked independently
        return None
    if now < row.created_at:  # clock went backwards: do not trust the row
        return None
    if get_account(db) is None or row.admin_id != ADMIN_ID:
        return None
    context = AuthContext(row.admin_id, row.id, row.csrf_hash, row.expires_at)
    return context, now - row.last_seen_at >= TOUCH_INTERVAL


def touch_session(db: Session, session_id: int, now: datetime) -> None:
    """Move the idle deadline forward. Conditional, so it can never undo a revocation or
    move ``last_seen_at`` backwards when two requests race."""
    db.execute(
        update(AdminSession)
        .where(
            AdminSession.id == session_id,
            AdminSession.revoked_at.is_(None),
            AdminSession.last_seen_at < now,
        )
        .values(last_seen_at=now)
    )


def validate_session(db: Session, token: str | None, now: datetime) -> AuthContext | None:
    """Check and touch in one (write) session. Used by tests and tools; requests use
    ``check_session`` and ``touch_session`` in separate transactions (auth/dependencies.py)."""
    checked = check_session(db, token, now)
    if checked is None:
        return None
    context, due = checked
    if due:
        touch_session(db, context.session_id, now)
    return context


def revoke_session(db: Session, session_id: int, now: datetime) -> None:
    db.execute(
        update(AdminSession)
        .where(AdminSession.id == session_id, AdminSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )


def logout(db: Session, session_id: int, now: datetime) -> None:
    revoke_session(db, session_id, now)
    _audit(db, "auth.logout", session_id=session_id)
    cleanup_sessions(db, now)


def revoke_token(db: Session, token: str | None, now: datetime) -> None:
    if well_formed(token):
        db.execute(
            update(AdminSession)
            .where(AdminSession.token_hash == sha256_hex(token), AdminSession.revoked_at.is_(None))
            .values(revoked_at=now)
        )


def revoke_all_sessions(db: Session, now: datetime) -> int:
    result = db.execute(
        update(AdminSession).where(AdminSession.revoked_at.is_(None)).values(revoked_at=now)
    )
    return result.rowcount or 0


def cleanup_sessions(db: Session, now: datetime) -> int:
    """Delete revoked, expired and idle sessions (they can never become valid again)."""
    result = db.execute(
        delete(AdminSession).where(
            or_(
                AdminSession.revoked_at.is_not(None),
                AdminSession.expires_at <= now,
                AdminSession.last_seen_at <= now - IDLE_TIMEOUT,
            )
        )
    )
    return result.rowcount or 0


def active_session_count(db: Session, now: datetime) -> int:
    query = select(func.count()).select_from(AdminSession).where(*_active(now))
    return db.execute(query).scalar_one()


def _enforce_session_cap(db: Session, now: datetime) -> None:
    """Make room for one more session by revoking the least recently used ones."""
    ids = (
        db.execute(
            select(AdminSession.id)
            .where(*_active(now))
            .order_by(AdminSession.last_seen_at.desc(), AdminSession.id.desc())
        )
        .scalars()
        .all()
    )
    for session_id in ids[MAX_ACTIVE_SESSIONS - 1 :]:
        revoke_session(db, session_id, now)
