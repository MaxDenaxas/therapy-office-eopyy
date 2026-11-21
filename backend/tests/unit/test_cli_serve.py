"""``python -m app.cli serve`` (M5.2 decision C): uvicorn's access log is off.

The access log writes every request line, query string included; a GET that carried a name or an
AMKA would end up in the server's output. The application never accepts personal values in URLs
(ARCHITECTURE.md §5.14), and the access log is off so that a mistake cannot leak them either.
"""

from __future__ import annotations

import logging

import pytest
import uvicorn

from app import cli


@pytest.fixture()
def captured(monkeypatch, tmp_path):
    monkeypatch.setenv("THERAPY_DATA_DIR", str(tmp_path))
    cli.get_settings.cache_clear()  # lru_cache: read the environment above
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: calls.append((a, k)))
    yield calls
    cli.get_settings.cache_clear()


@pytest.mark.parametrize("argv", [["serve"], ["serve", "--reload"]])
def test_serve_turns_the_access_log_off(captured, argv):
    assert cli.main(argv) == 0
    ((args, kwargs),) = captured
    assert kwargs["access_log"] is False
    assert kwargs["proxy_headers"] is False and kwargs["server_header"] is False  # unchanged
    assert kwargs["host"] in ("127.0.0.1", "::1", "localhost")


@pytest.fixture()
def uvicorn_loggers():
    """Constructing a uvicorn Config reconfigures its loggers process-wide: restore them."""
    loggers = [logging.getLogger(n) for n in ("uvicorn", "uvicorn.error", "uvicorn.access")]
    saved = [(lg, list(lg.handlers), lg.propagate, lg.level) for lg in loggers]
    yield
    for lg, handlers, propagate, level in saved:
        lg.handlers[:] = handlers
        lg.propagate = propagate
        lg.setLevel(level)


def test_with_the_access_log_off_uvicorn_writes_no_request_lines(uvicorn_loggers):
    """What ``access_log=False`` does inside uvicorn: the access logger loses its handlers and
    stops propagating, so the protocol's ``hasHandlers()`` check is False."""
    uvicorn.Config("app.main:create_app", factory=True, access_log=False)
    access = logging.getLogger("uvicorn.access")
    assert access.handlers == [] and access.propagate is False and not access.hasHandlers()
