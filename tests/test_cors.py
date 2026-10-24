import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def _client(settings: Settings, push, reader) -> TestClient:
    settings.jobs_enabled = False
    return TestClient(create_app(settings, push=push, cells_reader=reader))


def test_preflight_allows_dev_origin(client):
    res = client.options(
        "/api/v1/me",
        headers={
            "Origin": "http://localhost:8081",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert res.status_code == 200
    assert res.headers["access-control-allow-origin"] == "http://localhost:8081"
    assert "authorization" in res.headers["access-control-allow-headers"].lower()


def test_unknown_origin_is_not_allowed(client):
    res = client.get("/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in res.headers


def test_origins_from_env(settings, push, reader, now):
    settings.cors_origins = "https://app.woowang.example, https://other.example/"
    c = _client(settings, push, reader)
    res = c.get("/health", headers={"Origin": "https://other.example"})
    assert res.headers["access-control-allow-origin"] == "https://other.example"
    res = c.get("/health", headers={"Origin": "http://localhost:8081"})
    assert "access-control-allow-origin" not in res.headers


def test_wildcard_only_in_dev(settings, push, reader, now):
    settings.cors_origins = "*"
    settings.env = "prod"
    with pytest.raises(ValueError):
        _client(settings, push, reader)
    settings.env = "dev"
    c = _client(settings, push, reader)
    assert (
        c.get("/health", headers={"Origin": "https://x.example"}).headers[
            "access-control-allow-origin"
        ]
        == "*"
    )
