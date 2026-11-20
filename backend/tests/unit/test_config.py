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


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "[::1]", "LOCALHOST"])
def test_loopback_hosts_are_accepted(tmp_path, host):
    assert Settings(data_dir=tmp_path, host=host).host in {"127.0.0.1", "localhost", "::1"}


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "10.0.0.2", "office-pc", ""])
def test_non_loopback_hosts_are_refused(tmp_path, host):
    """Loopback only: LAN access needs a validated TLS setup, which does not exist yet."""
    with pytest.raises(ValidationError, match="loopback"):
        Settings(data_dir=tmp_path, host=host)


def test_non_loopback_host_from_the_environment_refuses_startup(monkeypatch, tmp_path):
    monkeypatch.setenv("THERAPY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("THERAPY_HOST", "0.0.0.0")
    from app import cli
    from app.config import get_settings

    get_settings.cache_clear()
    with pytest.raises(ValidationError, match="loopback"):
        cli.main(["serve"])


def test_serve_binds_to_the_configured_loopback_address(monkeypatch, tmp_path):
    monkeypatch.setenv("THERAPY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("THERAPY_PORT", "8123")
    calls = {}
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.update(app=app, **kw))
    from app import cli
    from app.config import get_settings

    get_settings.cache_clear()
    assert cli.main(["serve"]) == 0
    assert calls["host"] == "127.0.0.1" and calls["port"] == 8123
    assert calls["factory"] is True and calls["proxy_headers"] is False
