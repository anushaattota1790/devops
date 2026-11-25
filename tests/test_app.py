import os
import stat

from fastapi.testclient import TestClient

from app.main import app
from app.tls import generate_self_signed_cert

client = TestClient(app)


def test_healthz_returns_ok_and_version(monkeypatch):
    monkeypatch.setenv("APP_VERSION", "abc123")
    monkeypatch.setenv("PREVIOUS_APP_VERSION", "old123")
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "abc123", "previous_version": "old123"}


def test_healthz_defaults_to_dev_version(monkeypatch):
    monkeypatch.delenv("APP_VERSION", raising=False)
    monkeypatch.delenv("PREVIOUS_APP_VERSION", raising=False)
    assert client.get("/healthz").json() == {
        "status": "ok",
        "version": "dev",
        "previous_version": None,
    }


def test_healthz_empty_previous_version_is_unknown(monkeypatch):
    monkeypatch.setenv("PREVIOUS_APP_VERSION", "")
    assert client.get("/healthz").json()["previous_version"] is None


def test_readiness_follows_application_lifecycle():
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json() == {"status": "not ready"}

    with TestClient(app) as running_client:
        response = running_client.get("/readyz")
        assert response.status_code == 200
        assert response.json() == {"status": "ready"}

    assert client.get("/readyz").status_code == 503


def test_unknown_route_is_404():
    assert client.get("/nope").status_code == 404


def test_self_signed_cert_is_generated_with_private_key_permissions():
    cert_path, key_path = generate_self_signed_cert()
    assert os.path.getsize(cert_path) > 0
    assert os.path.getsize(key_path) > 0
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(key_path).st_mode) == 0o600
