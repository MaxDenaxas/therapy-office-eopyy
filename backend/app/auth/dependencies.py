"""Authentication dependencies. Both fail closed.

``require_admin``  a live server-side session (cookie) — else 401. If the database
                   is missing or not migrated — 503. If no administrator exists,
                   no session can exist, so this is always 401.
``require_csrf``   ``require_admin`` plus, for unsafe methods, the session's CSRF
                   token in the ``X-CSRF-Token`` header — else 403.

``protected_router()`` is how every non-public route is declared: the dependency
sits on the router, so a route added to it cannot forget it. The structural test
(tests/auth/test_route_protection.py) walks every route's effective dependency
tree and fails for any API route outside the explicit public allow-list.

Session validation runs before the route's own transaction and never waits for the
SQLite write lock: every check happens in a read-only transaction; the optional
``last_seen_at`` refresh (at most once a minute) is a separate short write that is
skipped, not failed, when the database is busy.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.exc import OperationalError

from app.api.deps import get_clock, get_database
from app.errors import ApiError

from . import service
from .service import AuthContext

logger = logging.getLogger("app.auth")

SESSION_COOKIE = "therapy_session"
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = frozenset({"GET", "HEAD"})


def current_session(request: Request) -> AuthContext | None:
    """The live session of this request, or None. Raises 503 if the database is unusable."""
    token = request.cookies.get(SESSION_COOKIE)
    if not service.well_formed(token):
        return None
    database = get_database(request)
    now = get_clock(request)()
    # 1. every check, in a read-only transaction: never waits for a writer
    with database.transaction(read_only=True) as db:
        checked = service.check_session(db, token, now)
    if checked is None:
        return None
    context, touch_due = checked
    # 2. the activity timestamp is optional: at most once a minute, fail fast if the database
    #    is locked (the next request will try again)
    if touch_due:
        try:
            with database.best_effort_write() as db:
                service.touch_session(db, context.session_id, now)
        except OperationalError as exc:
            if not _is_busy(exc):
                raise
            logger.info("session activity update skipped: database busy")
    return context


def _is_busy(exc: OperationalError) -> bool:
    message = str(exc.orig).lower()
    return "locked" in message or "busy" in message


def require_admin(request: Request) -> AuthContext:
    context = current_session(request)
    if context is None:
        raise ApiError(401, "not_authenticated")
    return context


def require_csrf(
    request: Request, context: Annotated[AuthContext, Depends(require_admin)]
) -> AuthContext:
    if request.method not in SAFE_METHODS and not service.csrf_matches(
        context.csrf_hash, request.headers.get(CSRF_HEADER)
    ):
        raise ApiError(403, "csrf_failed")
    return context


Admin = Annotated[AuthContext, Depends(require_csrf)]


def protected_router(**kwargs) -> APIRouter:
    """An APIRouter whose every route requires the administrator session and CSRF."""
    dependencies = [Depends(require_csrf), *kwargs.pop("dependencies", [])]
    return APIRouter(dependencies=dependencies, **kwargs)
