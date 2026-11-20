"""Privacy-safe API errors.

Every error body is ``{"code": "..."}`` (plus fixed, non-personal fields). Nothing
from the request is echoed: validation errors list only where and what (location
and message), never the submitted value; unexpected errors carry a request id and
are logged by exception type only, because exception messages may contain data.
"""

from __future__ import annotations

import logging
import secrets

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("app.errors")

_DEFAULT_CODES = {
    400: "bad_request",
    401: "not_authenticated",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "validation_error",
    429: "too_many_requests",
    503: "service_unavailable",
}


class ApiError(StarletteHTTPException):
    """An HTTP error with a stable machine-readable code (and optional extra fields)."""

    def __init__(
        self,
        status_code: int,
        code: str,
        headers: dict[str, str] | None = None,
        **extra: object,
    ) -> None:
        super().__init__(status_code, detail={"code": code, **extra}, headers=headers)


def error_response(
    status_code: int, code: str, headers: dict[str, str] | None = None, **extra
) -> JSONResponse:
    return JSONResponse({"code": code, **extra}, status_code=status_code, headers=headers)


async def _http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
    if isinstance(exc.detail, dict) and "code" in exc.detail:
        body = exc.detail
    else:  # framework errors (404, 405 …): never pass their text through
        body = {"code": _DEFAULT_CODES.get(exc.status_code, "error")}
    return JSONResponse(body, status_code=exc.status_code, headers=exc.headers)


async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = [
        {"loc": [str(part) for part in err.get("loc", ())][:6], "msg": str(err.get("msg", ""))}
        for err in exc.errors()
    ]
    return JSONResponse({"code": "validation_error", "errors": errors[:20]}, status_code=422)


def exception_type_names(exc: BaseException) -> str:
    """Type names only (never messages); exception groups, which anyio and Starlette's
    BaseHTTPMiddleware wrap errors in, are unpacked to their leaves."""
    if isinstance(exc, BaseExceptionGroup):
        names = [exception_type_names(sub) for sub in exc.exceptions]
        return ",".join(dict.fromkeys(names)) or type(exc).__name__
    return type(exc).__name__


class ResponseAborted(Exception):
    """Raised (without message or cause) when a response had already started and cannot be
    replaced by an error page; the server closes the connection."""


def _leaves(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for sub in exc.exceptions for leaf in _leaves(sub)]
    return [exc]


def process_control_exception(control: BaseExceptionGroup | BaseException) -> BaseException:
    """The one process-control exception to re-raise for a group of them, stripped of context.

    Only the *leaf* is raised, never the group: a group carries a free-text message and its
    ordinary members. Shutdown signals win over cancellation (KeyboardInterrupt, then
    SystemExit, then the rest in order of appearance), so an interrupt is never downgraded
    to a mere task cancellation. The leaf's own cause/context chain is cut so that nothing
    that was raised alongside it is printed with it.
    """
    leaves = _leaves(control)
    for kind in (KeyboardInterrupt, SystemExit):
        for leaf in leaves:
            if isinstance(leaf, kind):
                chosen = leaf
                break
        else:
            continue
        break
    else:
        chosen = leaves[0]
    chosen.__cause__ = None
    chosen.__context__ = None
    chosen.__suppress_context__ = True
    return chosen


class CrashGuard:
    """Outermost application middleware: no unexpected exception ever leaves the application.

    Starlette's own 500 handler always re-raises after responding, and uvicorn then logs the
    traceback with the exception *message*, which may contain patient data. This guard
    answers 500 itself and logs only the exception type and a request id (the same id is in
    the response body, so an operator can correlate them).

    Strategy, by what was raised:

    * ``Exception`` (including an ``ExceptionGroup`` of them, nested or not): generic 500,
      or ``ResponseAborted`` if the response had already started.
    * A bare process-control ``BaseException`` (KeyboardInterrupt, SystemExit,
      CancelledError, ...): propagates untouched. It is not caught, logged or translated.
    * A ``BaseExceptionGroup`` that contains *any* process-control exception: the group is
      split into its ordinary and process-control parts. The ordinary part is *dropped*
      (its types are logged, never its messages) and a single process-control leaf is
      re-raised without context (see ``process_control_exception``). The ordinary errors are
      not answered with a 500, because the process is being interrupted; and the group, whose
      text and members could carry sensitive messages, is never handed to the server.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def tracking_send(message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        failure: BaseException | None = None
        interrupt: BaseException | None = None
        try:
            await self.app(scope, receive, tracking_send)
        except BaseExceptionGroup as group:
            _ordinary, control = group.split(Exception)
            if control is None:
                failure = group
            else:
                logger.error(
                    "unhandled %s request_id=%s (interrupted)",
                    exception_type_names(group),
                    secrets.token_hex(8),
                )
                interrupt = process_control_exception(control)
        except Exception as exc:
            failure = exc
        # Both re-raises happen here, *outside* the except block: raising inside it would make
        # Python attach the original (message-carrying) exception as ``__context__``.
        if interrupt is not None:
            raise interrupt
        if failure is not None:
            await self._fail(failure, started, scope, receive, send)

    @staticmethod
    async def _fail(exc: BaseException, started: bool, scope, receive, send) -> None:
        request_id = secrets.token_hex(8)
        logger.error("unhandled %s request_id=%s", exception_type_names(exc), request_id)
        if started:  # too late for an error page: abort the connection, no details
            raise ResponseAborted from None
        from app.security import apply_security_headers

        response = JSONResponse({"code": "internal_error", "request_id": request_id}, 500)
        apply_security_headers(response.headers, api=True)
        await response(scope, receive, send)


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)


def install_crash_guard(app: FastAPI) -> None:
    """Call last: the most recently added middleware is the outermost one."""
    app.add_middleware(CrashGuard)
