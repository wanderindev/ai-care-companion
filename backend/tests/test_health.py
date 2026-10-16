def test_health_returns_healthy(client):
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert body["service"] == "n16chat API"
    assert body["version"]


def test_unknown_api_route_is_404(client):
    assert client.get("/api/v1/does-not-exist").status_code == 404
