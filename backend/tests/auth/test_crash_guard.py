"""Unexpected exceptions: generic 500, and nothing sensitive in the server's own output.

The decisive tests run a *real uvicorn server in a subprocess* and read everything it prints,
because the leak (Starlette re-raising after its 500 handler, uvicorn logging the traceback
with the exception message) only exists on that path — the in-process TestClient hides it.

Reliability: the server is stopped through a test-only route that sets ``server.should_exit``
and the process is waited for (bounded), so every line it writes *after* the client got its
response — the leaking traceback is written then — is in the captured output. A control test
runs the same harness against the old behaviour and requires the leak to be visible, so these
tests cannot pass vacuously.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import subprocess
import sys
import time
import traceback
from pathlib import Path

import httpx2 as httpx
import pytest

from app.config import Settings
from app.errors import (
    CrashGuard,
    ResponseAborted,
    exception_type_names,
    process_control_exception,
)
from app.main import create_app
from tests.local_http import local_client

BACKEND = Path(__file__).resolve().parents[2]
# Synthetic, built at run time so no AMKA-like literal sits in the source (11 digits).
SECRET = "7" * 11

# argv: data_dir, port, mode.  mode "fixed" = the application as it is;
# mode "old" = the pre-fix behaviour (guard bypassed, plain JSON 500 handler), used only by the
# control test to prove that this harness can see the leak.
SERVER = """
import sys
import uvicorn
from fastapi.responses import JSONResponse, StreamingResponse
from app import errors
from app.config import Settings

mode = sys.argv[3]
if mode == "old":
    async def passthrough(self, scope, receive, send):
        await self.app(scope, receive, send)
    errors.CrashGuard.__call__ = passthrough

from app.main import create_app

secret = "7" * 11
app = create_app(Settings(data_dir=sys.argv[1]))
if mode == "old":
    async def old_handler(request, exc):
        return JSONResponse({"code": "internal_error", "request_id": "old"}, 500)
    app.add_exception_handler(Exception, old_handler)

@app.get("/api/boom")
def boom():
    raise ValueError(f"patient AMKA {secret} must never be logged")

@app.get("/api/boom-chained")
def boom_chained():
    try:
        raise KeyError(secret)
    except KeyError as exc:
        raise RuntimeError(f"wrapped {secret}") from exc

@app.get("/api/boom-async")
async def boom_async():
    raise LookupError(f"async {secret}")

@app.get("/api/boom-stream")
def boom_stream():
    def body():
        yield b"first chunk"
        raise ValueError(f"mid-stream {secret}")
    return StreamingResponse(body())

server = uvicorn.Server(uvicorn.Config(
    app, host="127.0.0.1", port=int(sys.argv[2]), log_level="debug",
    timeout_graceful_shutdown=5,
))

@app.get("/api/test-stop")
def stop():
    server.should_exit = True
    return {"stopping": True}

server.run()
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    def __init__(self, tmp_path, mode: str) -> None:
        self.port = free_port()
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
        env["THERAPY_DATA_DIR"] = str(tmp_path / "data")
        self.proc = subprocess.Popen(
            [sys.executable, "-c", SERVER, str(tmp_path / "data"), str(self.port), mode],
            cwd=BACKEND,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
        )
        self.base = f"http://127.0.0.1:{self.port}"
        self._wait_until_ready()

    def _wait_until_ready(self) -> None:
        deadline = time.monotonic() + 30
        while True:
            try:
                if httpx.get(f"{self.base}/api/health", timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            if self.proc.poll() is not None or time.monotonic() > deadline:
                self.kill()
                raise RuntimeError("test server did not start")
            time.sleep(0.2)

    def kill(self) -> str:
        if self.proc.poll() is None:
            self.proc.kill()
        return self.proc.communicate(timeout=15)[0]

    def stop_and_collect(self) -> str:
        """Ask the server to exit, wait (bounded) until it has, then return all its output."""
        try:
            httpx.get(f"{self.base}/api/test-stop", timeout=3)
        except httpx.HTTPError:
            pass  # the old behaviour can hang a request; the stop flag is set regardless
        try:
            return self.proc.communicate(timeout=20)[0]
        except subprocess.TimeoutExpired:
            self.kill()
            pytest.fail("the test server did not shut down gracefully within 20 s")


@pytest.fixture()
def uvicorn_server(tmp_path):
    server = Server(tmp_path, "fixed")
    try:
        yield server
    finally:
        if server.proc.poll() is None:
            server.kill()  # always reap the process, even when a test failed


@pytest.mark.parametrize("path", ["/api/boom", "/api/boom-chained", "/api/boom-async"])
def test_real_server_returns_a_generic_500_and_logs_no_exception_message(uvicorn_server, path):
    server = uvicorn_server
    r = httpx.get(server.base + path, timeout=10)
    assert r.status_code == 500
    body = r.json()
    assert set(body) == {"code", "request_id"} and body["code"] == "internal_error"
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert SECRET not in r.text
    # the server stays healthy afterwards
    assert httpx.get(server.base + "/api/health", timeout=10).status_code == 200

    out = server.stop_and_collect()
    assert SECRET not in out, "the exception message reached the server output"
    assert "Traceback" not in out and "must never be logged" not in out
    # useful diagnostics survive: the exception *type* and the id that is also in the response
    assert f"request_id={body['request_id']}" in out
    assert "unhandled" in out
    assert "Shutting down" in out  # the whole run was captured, up to a clean exit


def test_real_server_mid_stream_failure_logs_no_message(uvicorn_server):
    """The page had already started, so no error page is possible. (Starlette's
    BaseHTTPMiddleware, used by the security headers, ends the stream normally before the error
    surfaces: see docs/current-status.md, known limitations.)"""
    server = uvicorn_server
    httpx.get(server.base + "/api/boom-stream", timeout=10)
    assert httpx.get(server.base + "/api/health", timeout=10).status_code == 200
    out = server.stop_and_collect()
    assert SECRET not in out and "mid-stream" not in out
    assert "ResponseAborted" in out  # a message-free marker an operator can search for
    assert "unhandled ValueError request_id=" in out


def test_control_the_harness_sees_the_old_leak(tmp_path):
    """The same server with the pre-fix behaviour MUST show the message in its output.
    If this fails, the tests above would pass vacuously (e.g. uvicorn stopped logging it)."""
    server = Server(tmp_path, "old")
    try:
        try:
            httpx.get(server.base + "/api/boom", timeout=3)
        except httpx.HTTPError:
            pass  # the old path may leave the request hanging; the log line is what matters
        out = server.stop_and_collect()
    finally:
        if server.proc.poll() is None:
            server.kill()
    assert SECRET in out, "the old leaking behaviour was not visible: the harness is blind"
    assert "Traceback" in out


# ---------------------------------------------------------------- in-process behaviour
@pytest.fixture()
def failing_app(tmp_path):
    app = create_app(Settings(data_dir=tmp_path))

    @app.get("/api/boom")
    def boom():
        raise ValueError(f"patient AMKA {SECRET}")

    return app


def test_response_is_generic_and_the_exception_does_not_propagate(failing_app, caplog):
    # raise_server_exceptions=True: the TestClient re-raises anything that escapes the app
    with local_client(failing_app, raise_server_exceptions=True) as c:
        r = c.get("/api/boom")
    assert r.status_code == 500
    assert r.json()["code"] == "internal_error"
    assert SECRET not in r.text and SECRET not in caplog.text
    assert f"unhandled ValueError request_id={r.json()['request_id']}" in caplog.text


def test_the_guard_is_outermost(failing_app):
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.middleware.errors import ServerErrorMiddleware

    stack = failing_app.build_middleware_stack()
    assert isinstance(stack, ServerErrorMiddleware)
    chain, layer = [], stack.app
    while layer is not None and not isinstance(layer, type(failing_app.router)):
        chain.append(type(layer))
        layer = getattr(layer, "app", None)
    assert CrashGuard in chain
    # outside every application middleware that could let an exception escape
    assert chain.index(CrashGuard) < min(
        i for i, t in enumerate(chain) if issubclass(t, BaseHTTPMiddleware)
    )


def test_request_ids_are_unique(failing_app):
    with local_client(failing_app) as c:
        ids = {c.get("/api/boom").json()["request_id"] for _ in range(5)}
    assert len(ids) == 5


def run_guard(raised: BaseException, started: bool = False):
    """Run CrashGuard around an app that raises ``raised``; return (messages sent, outcome)."""
    sent: list[dict] = []

    async def inner(scope, receive, send):
        if started:
            await send({"type": "http.response.start", "status": 200, "headers": []})
        raise raised

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request"}

    async def go():
        await CrashGuard(inner)({"type": "http", "method": "GET"}, receive, send)

    try:
        asyncio.run(go())
    except BaseException as exc:  # the outcome is what the test inspects
        return sent, exc
    return sent, None


def rendered(exc: BaseException) -> str:
    return "".join(traceback.format_exception(exc))


# ---- exception groups
def test_exception_groups_are_logged_by_leaf_type_only():
    group = ExceptionGroup("g", [ValueError(SECRET), ExceptionGroup("n", [KeyError(SECRET)])])
    assert exception_type_names(group) == "ValueError,KeyError"
    assert exception_type_names(RuntimeError(SECRET)) == "RuntimeError"
    mixed = BaseExceptionGroup("m", [ValueError(SECRET), KeyboardInterrupt()])
    assert exception_type_names(mixed) == "ValueError,KeyboardInterrupt"


def test_an_ordinary_group_gets_a_generic_500(caplog):
    group = ExceptionGroup("tasks failed", [ValueError(SECRET), KeyError(SECRET)])
    sent, outcome = run_guard(group)
    assert outcome is None
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert sent[0]["status"] == 500
    assert SECRET not in repr(sent) and SECRET not in caplog.text
    assert "unhandled ValueError,KeyError request_id=" in caplog.text


def test_a_nested_ordinary_group_gets_a_generic_500(caplog):
    nested = ExceptionGroup(
        "outer", [ExceptionGroup("inner", [ValueError(SECRET)]), LookupError(SECRET)]
    )
    sent, outcome = run_guard(nested)
    assert outcome is None and sent[0]["status"] == 500
    assert SECRET not in caplog.text and "unhandled ValueError,LookupError" in caplog.text


def test_an_ordinary_group_after_the_response_started_is_aborted_without_details(caplog):
    sent, outcome = run_guard(ExceptionGroup("g", [ValueError(SECRET)]), started=True)
    assert isinstance(outcome, ResponseAborted) and str(outcome) == ""
    assert outcome.__cause__ is None and outcome.__suppress_context__ is True
    assert outcome.__context__ is None  # nothing to walk to the original message
    assert [m["type"] for m in sent] == ["http.response.start"]
    assert SECRET not in rendered(outcome) and SECRET not in caplog.text


# ---- process control
@pytest.mark.parametrize(
    "exc",
    [KeyboardInterrupt(), SystemExit(3), asyncio.CancelledError(), GeneratorExit()],
    ids=["KeyboardInterrupt", "SystemExit", "CancelledError", "GeneratorExit"],
)
def test_bare_process_control_exceptions_propagate_untouched(exc):
    sent, outcome = run_guard(exc)
    assert outcome is exc and sent == []


def test_a_mixed_group_never_reaches_the_server_with_its_message(caplog):
    mixed = BaseExceptionGroup(
        f"AMKA {SECRET} in the group message",
        [ValueError(f"AMKA {SECRET}"), KeyboardInterrupt()],
    )
    sent, outcome = run_guard(mixed)
    assert isinstance(outcome, KeyboardInterrupt)  # process control preserved, not downgraded
    assert not isinstance(outcome, BaseExceptionGroup)
    assert sent == []  # being interrupted: no error page is attempted
    assert SECRET not in rendered(outcome) and SECRET not in repr(outcome.args)
    assert outcome.__context__ is None and outcome.__cause__ is None
    assert SECRET not in caplog.text
    assert "unhandled ValueError,KeyboardInterrupt request_id=" in caplog.text


def test_a_mixed_group_keeps_the_exit_code_of_system_exit():
    mixed = BaseExceptionGroup("m", [RuntimeError(SECRET), SystemExit(7)])
    _, outcome = run_guard(mixed)
    assert isinstance(outcome, SystemExit) and outcome.code == 7
    assert SECRET not in rendered(outcome)


def test_a_nested_mixed_group_is_flattened_to_its_process_control_leaf():
    nested = BaseExceptionGroup(
        SECRET,
        [
            ExceptionGroup("a", [ValueError(SECRET)]),
            BaseExceptionGroup("b", [KeyError(SECRET), asyncio.CancelledError()]),
        ],
    )
    _, outcome = run_guard(nested)
    assert isinstance(outcome, asyncio.CancelledError)
    assert SECRET not in rendered(outcome)


def test_a_pure_cancellation_group_is_not_swallowed():
    group = BaseExceptionGroup(SECRET, [asyncio.CancelledError(), asyncio.CancelledError()])
    sent, outcome = run_guard(group)
    assert isinstance(outcome, asyncio.CancelledError) and sent == []
    assert SECRET not in rendered(outcome)


def test_shutdown_signals_win_over_cancellation_inside_a_group():
    for leaves, expected in [
        ([asyncio.CancelledError(), KeyboardInterrupt()], KeyboardInterrupt),
        ([asyncio.CancelledError(), SystemExit(1)], SystemExit),
        ([SystemExit(1), KeyboardInterrupt()], KeyboardInterrupt),
        ([asyncio.CancelledError(), GeneratorExit()], asyncio.CancelledError),
    ]:
        outcome = process_control_exception(BaseExceptionGroup("g", leaves))
        assert type(outcome) is expected, leaves


def test_non_http_scopes_pass_through_untouched():
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])
        raise RuntimeError("lifespan failure")  # not ours to translate

    async def noop(*_):
        return None

    with pytest.raises(RuntimeError):
        asyncio.run(CrashGuard(inner)({"type": "lifespan"}, noop, noop))
    assert seen == ["lifespan"]


def test_after_the_response_started_the_connection_is_aborted_with_no_details(caplog):
    sent, outcome = run_guard(ValueError(f"mid-stream {SECRET}"), started=True)
    assert isinstance(outcome, ResponseAborted) and str(outcome) == ""
    assert outcome.__cause__ is None
    assert outcome.__suppress_context__ is True and outcome.__context__ is None
    assert [m["type"] for m in sent] == ["http.response.start"]  # no second response
    assert SECRET not in caplog.text and SECRET not in rendered(outcome)


def test_the_logger_never_receives_messages_or_tracebacks(caplog):
    caplog.set_level(logging.DEBUG)
    run_guard(ExceptionGroup("g", [ValueError(SECRET)]))
    run_guard(BaseExceptionGroup(SECRET, [ValueError(SECRET), KeyboardInterrupt()]))
    assert SECRET not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
