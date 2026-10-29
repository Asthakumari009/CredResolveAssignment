from fastapi.testclient import TestClient

from mock_provider.app import app


def test_dial_is_idempotent_and_status_by_key():
    with TestClient(app) as c:
        c.post("/control/reset")
        c.post("/a/control", json={"auto_events": False, "error_rate": 0.0, "latency_ms": [0, 1]})
        r1 = c.post("/a/dial", json={"idempotency_key": "k1", "phone": "+91"}).json()
        r2 = c.post("/a/dial", json={"idempotency_key": "k1", "phone": "+91"}).json()
        assert r1["provider_call_id"] == r2["provider_call_id"] and r2["duplicate_dial"]
        assert c.get("/a/calls/k1").json()["status"] == "INITIATED"
        assert c.get("/a/calls/nope").status_code == 404
        c.post("/control/reset")


def test_explicit_error_means_not_placed():
    with TestClient(app) as c:
        c.post("/control/reset")
        c.post("/a/control", json={"auto_events": False, "error_rate": 1.0, "latency_ms": [0, 1]})
        assert c.post("/a/dial", json={"idempotency_key": "k2", "phone": "+91"}).status_code == 503
        assert c.get("/a/calls/k2").status_code == 404
        c.post("/control/reset")
