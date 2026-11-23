import os
import stat

from fastapi.testclient import TestClient

from app.main import app
from app.tls import generate_self_signed_cert

client = TestClient(app)


def test_healthz_returns_ok_and_version(monkeypatch):
    monkeypatch.setenv("APP_VERSION", "abc123")
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "abc123"}


def test_healthz_defaults_to_dev_version(monkeypatch):
    monkeypatch.delenv("APP_VERSION", raising=False)
    assert client.get("/healthz").json()["version"] == "dev"


def test_unknown_route_is_404():
    assert client.get("/nope").status_code == 404


def test_self_signed_cert_is_generated_with_private_key_permissions():
    cert_path, key_path = generate_self_signed_cert()
    assert os.path.getsize(cert_path) > 0
    assert os.path.getsize(key_path) > 0
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(key_path).st_mode) == 0o600
