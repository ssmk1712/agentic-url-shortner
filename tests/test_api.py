import os
import tempfile
from datetime import datetime, timedelta, timezone

fd, path = tempfile.mkstemp()
os.close(fd)
os.unlink(path)
os.environ["DATABASE_PATH"] = path
os.environ["BASE_URL"] = "http://testserver"

from fastapi.testclient import TestClient
from app.db import init_db
from app.main import app

init_db()
client = TestClient(app)


def test_end_to_end_redirect_and_analytics():
    response = client.post("/api/v1/urls", json={"url": "https://example.com"})
    assert response.status_code == 201
    code = response.json()["code"]

    redirect = client.get("/" + code, follow_redirects=False)
    assert redirect.status_code == 307
    assert redirect.headers["location"] == "https://example.com"

    analytics = client.get(f"/api/v1/urls/{code}/analytics")
    assert analytics.status_code == 200
    assert analytics.json()["clicks"] == 1


def test_invalid_url_scheme_is_rejected():
    assert client.post("/api/v1/urls", json={"url": "javascript:alert(1)"}).status_code == 422


def test_alias_collision():
    assert client.post(
        "/api/v1/urls", json={"url": "https://example.com", "custom_alias": "demo123"}
    ).status_code == 201
    assert client.post(
        "/api/v1/urls", json={"url": "https://openai.com", "custom_alias": "demo123"}
    ).status_code == 409


def test_expiration_is_enforced():
    future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    response = client.post(
        "/api/v1/urls",
        json={"url": "https://example.com/expiring", "custom_alias": "expire1", "expires_at": future},
    )
    assert response.status_code == 201
    assert response.json()["expires_at"] is not None


def test_past_expiration_is_rejected():
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    response = client.post("/api/v1/urls", json={"url": "https://example.com", "expires_at": past})
    assert response.status_code == 422


def test_deactivate_prevents_redirect():
    created = client.post(
        "/api/v1/urls", json={"url": "https://example.com/off", "custom_alias": "off123"}
    )
    assert created.status_code == 201
    assert client.delete("/api/v1/urls/off123").status_code == 204
    assert client.get("/off123", follow_redirects=False).status_code == 404


def test_reserved_alias_is_rejected():
    response = client.post(
        "/api/v1/urls", json={"url": "https://example.com", "custom_alias": "health"}
    )
    assert response.status_code == 422
