"""Login, logout and session status.

There is deliberately no endpoint that creates the administrator or changes the
password: both are CLI-only (``python -m app.cli admin ...``).

POST /api/auth/login    public. 200 + session cookie, 401 ``invalid_credentials``
                        (same answer for an unknown username, a wrong password or
                        no administrator at all), 429 ``login_locked`` + Retry-After.
GET  /api/auth/session  public. ``{"authenticated": false}`` or the session's
                        CSRF token and expiry (lets the frontend recover after reload).
POST /api/auth/logout   protected (session + CSRF). Revokes the session.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import DbSession, get_clock
from app.auth import service
from app.auth.dependencies import SESSION_COOKIE, Admin, current_session, protected_router
from app.errors import error_response

public = APIRouter(prefix="/auth", tags=["auth"])
protected = protected_router(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


def _iso_utc(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat() + "Z"


def _set_cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
        path="/",
        # the browser forgets it at the absolute limit; the server enforces both limits anyway
        max_age=int(service.ABSOLUTE_LIFETIME.total_seconds()),
    )


def _clear_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(
        SESSION_COOKIE,
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
        path="/",
    )


@public.post("/login")
def login(
    body: LoginRequest, request: Request, db: DbSession, clock=Depends(get_clock)
) -> JSONResponse:
    now = clock()
    result = service.login(
        db, body.username, body.password, now, request.cookies.get(SESSION_COOKIE)
    )
    # Failures are *returned*, not raised, so the failure counter is committed. A failed or
    # locked attempt leaves any session the browser already holds untouched.
    if isinstance(result, service.LoginLocked):
        seconds = result.retry_after_seconds
        return error_response(
            429, "login_locked", headers={"Retry-After": str(seconds)}, retry_after=seconds
        )
    if isinstance(result, service.LoginFailed):
        return error_response(401, "invalid_credentials")
    response = JSONResponse(
        {
            "csrf_token": result.csrf_token,
            "expires_at": _iso_utc(result.expires_at),
            "idle_timeout_seconds": int(service.IDLE_TIMEOUT.total_seconds()),
        }
    )
    _set_cookie(response, request, result.session_token)
    return response


@public.get("/session")
def session_status(request: Request) -> JSONResponse:
    context = current_session(request)
    if context is None:
        response = JSONResponse({"authenticated": False})
        if SESSION_COOKIE in request.cookies:
            _clear_cookie(response, request)
        return response
    token = request.cookies[SESSION_COOKIE]
    return JSONResponse(
        {
            "authenticated": True,
            "csrf_token": service.csrf_token_for(token),
            "expires_at": _iso_utc(context.expires_at),
            "idle_timeout_seconds": int(service.IDLE_TIMEOUT.total_seconds()),
        }
    )


@protected.post("/logout", status_code=204)
def logout(admin: Admin, request: Request, db: DbSession, clock=Depends(get_clock)) -> Response:
    now = clock()
    service.logout(db, admin.session_id, now)
    response = Response(status_code=204)
    _clear_cookie(response, request)
    return response
