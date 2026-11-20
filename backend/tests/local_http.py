"""A TestClient that behaves like the local browser: loopback peer, loopback Host.

The default TestClient connects as client "testclient" to host "testserver";
the security middleware refuses both (see app/security.py)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

BASE_URL = "http://127.0.0.1:8000"
ORIGIN = {"Origin": BASE_URL}


def local_client(app: FastAPI, **kwargs) -> TestClient:
    kwargs.setdefault("base_url", BASE_URL)
    kwargs.setdefault("client", ("127.0.0.1", 50000))
    return TestClient(app, **kwargs)
