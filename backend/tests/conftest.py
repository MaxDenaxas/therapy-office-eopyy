"""Shared pytest configuration. Fixture files are built in code, never read from disk."""

import os
import sys
from pathlib import Path

import pytest

# make `fixtures.eopyy_builder` importable from any test
sys.path.insert(0, str(Path(__file__).parent))

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch, tmp_path_factory):
    """No test may read the developer's real settings or touch their real data directory."""
    for key in list(os.environ):
        if key.startswith("THERAPY_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("THERAPY_DATA_DIR", str(tmp_path_factory.mktemp("therapy-data")))
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
