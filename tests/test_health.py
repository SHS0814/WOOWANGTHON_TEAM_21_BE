from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_health_reports_mock_reader():
    client = TestClient(create_app(Settings(_env_file=None)))
    res = client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["reader"] == {"layout": "mock", "cells": "mock", "is_mock": True}
