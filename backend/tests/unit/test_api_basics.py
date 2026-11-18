"""API behaviour that needs no database."""

import httpx


async def test_health(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
    assert len(r.headers["x-request-id"]) >= 8
    assert r.headers["x-content-type-options"] == "nosniff"


async def test_request_id_propagated(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/v1/health", headers={"X-Request-ID": "trace-12345678"})
    assert r.headers["x-request-id"] == "trace-12345678"


async def test_unauthenticated_error_envelope(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/v1/auth/me", headers={"X-Request-ID": "trace-abcdefgh"})
    assert r.status_code == 401
    body = r.json()
    assert body["error"]["code"] == "unauthenticated"
    assert body["error"]["request_id"] == "trace-abcdefgh"


async def test_garbage_token_rejected(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/v1/users", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401
    assert r.json()["error"]["message"] == "Invalid access token"


async def test_validation_error_envelope(client: httpx.AsyncClient) -> None:
    r = await client.post("/api/v1/auth/login", json={"username": "x"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


async def test_unknown_route(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/v1/does-not-exist")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


async def test_refresh_without_cookie(client: httpx.AsyncClient) -> None:
    r = await client.post("/api/v1/auth/refresh")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_refresh_token"


async def test_metrics_exposed(client: httpx.AsyncClient) -> None:
    await client.get("/api/v1/health")
    r = await client.get("/metrics")
    assert r.status_code == 200
    assert "localops_http_requests_total" in r.text
