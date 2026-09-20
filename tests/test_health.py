"""Health endpoint tests — liveness probe + pattern listing."""

from fastapi.testclient import TestClient


def test_health_ok():
    import main  # noqa: F401 -- importing it completes discover_builtin_tools/patterns

    client = TestClient(main.app)
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == "0" and body["status"] is True
    assert body["data"]["status"] == "ok"
    # builtin patterns should be discoverable through the probe
    assert "customer_agent" in body["data"]["patterns"]
