import pytest

from app import __version__
from app.config import Settings
from app.main import create_app
from tests.local_http import local_client


def test_health_ok(tmp_path):
    client = local_client(create_app(Settings(data_dir=tmp_path)))
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "version": __version__, "environment": "development"}


def test_health_exposes_no_paths(tmp_path):
    client = local_client(create_app(Settings(data_dir=tmp_path)))
    assert str(tmp_path) not in client.get("/api/health").text


def test_docs_only_in_development(tmp_path):
    dev = local_client(create_app(Settings(data_dir=tmp_path, environment="development")))
    prod = local_client(create_app(Settings(data_dir=tmp_path, environment="production")))
    assert dev.get("/api/docs").status_code == 200
    assert prod.get("/api/docs").status_code == 404
    assert prod.get("/api/openapi.json").status_code == 404


def test_serves_built_frontend_without_shadowing_api(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html><body>shell</body></html>", encoding="utf-8")
    client = local_client(create_app(Settings(data_dir=tmp_path / "data", frontend_dist=dist)))
    assert "shell" in client.get("/").text
    assert client.get("/api/health").json()["status"] == "ok"


def test_missing_frontend_dist_is_a_clear_error(tmp_path):
    with pytest.raises(RuntimeError, match="frontend build"):
        create_app(Settings(data_dir=tmp_path / "data", frontend_dist=tmp_path / "nope"))


def test_health_is_public_but_only_from_this_machine(tmp_path):
    app = create_app(Settings(data_dir=tmp_path))
    from fastapi.testclient import TestClient

    remote = TestClient(app, base_url="http://127.0.0.1:8000", client=("192.168.1.20", 5000))
    assert remote.get("/api/health").status_code == 403
    assert local_client(app).get("/api/health").status_code == 200
