import pytest
from pydantic import ValidationError

from app.config import Settings, default_data_dir
from tests.conftest import REPO_ROOT


def test_defaults_are_local_only(tmp_path):
    s = Settings(data_dir=tmp_path)
    assert s.host == "127.0.0.1"
    assert s.port == 8000
    assert s.environment == "development"
    assert s.frontend_dist is None


def test_environment_variables_are_read(monkeypatch, tmp_path):
    monkeypatch.setenv("THERAPY_PORT", "9123")
    monkeypatch.setenv("THERAPY_ENVIRONMENT", "production")
    monkeypatch.setenv("THERAPY_DATA_DIR", str(tmp_path))
    s = Settings()
    assert (s.port, s.environment, s.data_dir) == (9123, "production", tmp_path.resolve())


def test_invalid_values_are_rejected(tmp_path):
    with pytest.raises(ValidationError):
        Settings(data_dir=tmp_path, port=0)
    with pytest.raises(ValidationError):
        Settings(data_dir=tmp_path, environment="staging")


def test_data_dir_inside_repository_is_refused():
    with pytest.raises(ValidationError, match="inside a Git repository"):
        Settings(data_dir=REPO_ROOT / "backend" / "data")


def test_default_data_dir_is_outside_any_repository():
    # the default must itself satisfy the rule it enforces
    Settings(data_dir=default_data_dir())
