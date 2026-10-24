def test_health_reports_mock_reader(client):
    res = client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["reader"] == {"layout": "mock", "cells": "mock", "is_mock": True}
