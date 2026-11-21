import json
import logging

from sqlalchemy import text

from conftest import new_key, signup
from db import SessionLocal
from redis_client import redis


def bearer(key):
    return {"Authorization": f"Bearer {key}"}


# ------------------------------------------------------------- accounts
async def test_register_login_and_wrong_password(client):
    await signup(client)
    ok = await client.post("/api/auth/login", json={"email": "A@Example.com", "password": "correct-horse-1"})
    assert ok.status_code == 200 and ok.json()["token"].startswith("pmes.")
    bad = await client.post("/api/auth/login", json={"email": "a@example.com", "password": "nope-nope-nope"})
    assert bad.status_code == 401
    ghost = await client.post("/api/auth/login", json={"email": "x@example.com", "password": "nope-nope-nope"})
    assert ghost.status_code == 401 and ghost.json() == bad.json()


async def test_duplicate_email_and_weak_password(client):
    await signup(client)
    r = await client.post("/api/auth/register", json={"email": "a@example.com", "password": "correct-horse-1"})
    assert r.status_code == 409
    r = await client.post("/api/auth/register", json={"email": "b@example.com", "password": "short"})
    assert r.status_code == 422


async def test_login_bruteforce_is_limited(client):
    await signup(client)
    codes = [(await client.post("/api/auth/login",
             json={"email": "a@example.com", "password": f"wrong-pass-{i}"})).status_code for i in range(12)]
    assert codes[:10] == [401] * 10 and codes[10] == 429


async def test_management_requires_real_session(client):
    assert (await client.get("/api/keys")).status_code == 401
    # the old spoofable header must no longer work
    assert (await client.get("/api/keys", headers={"X-Customer-Id": "x"})).status_code == 401
    assert (await client.get("/api/keys", headers={"Authorization": "Bearer pmes.fake.fake"})).status_code == 401


# --------------------------------------------------------- key lifecycle
async def test_create_list_rename(client):
    h = await signup(client)
    created = await new_key(client, h)
    assert created["key"].startswith("pme_")
    rows = (await client.get("/api/keys", headers=h)).json()
    assert len(rows) == 1 and rows[0]["is_active"] is True and rows[0]["last_used_at"] is None
    assert "key_hash" not in rows[0] and created["key"] not in json.dumps(rows)
    r = await client.patch(f"/api/keys/{created['id']}", json={"name": "Staging"}, headers=h)
    assert r.status_code == 200
    assert (await client.get("/api/keys", headers=h)).json()[0]["name"] == "Staging"
    assert (await client.patch(f"/api/keys/{created['id']}", json={"name": "  "}, headers=h)).status_code == 422


async def test_stored_hash_is_hmac_not_plain(client):
    import hashlib
    h = await signup(client)
    k = await new_key(client, h)
    async with SessionLocal() as s:
        stored = await s.scalar(text("SELECT key_hash FROM api_keys"))
    assert stored != hashlib.sha256(k["key"].encode()).hexdigest()
    assert k["key"] not in stored


async def test_customer_isolation(client):
    a = await signup(client, "a@example.com")
    b = await signup(client, "b@example.com")
    k = await new_key(client, a)
    assert (await client.get("/api/keys", headers=b)).json() == []
    assert (await client.patch(f"/api/keys/{k['id']}", json={"name": "x"}, headers=b)).status_code == 404
    assert (await client.post(f"/api/keys/{k['id']}/revoke", headers=b)).status_code == 404
    assert (await client.get("/v1/ping", headers=bearer(k["key"]))).status_code == 200  # still alive


async def test_max_active_keys(client):
    h = await signup(client)
    for i in range(10):
        await new_key(client, h, f"k{i}")
    assert (await client.post("/api/keys", json={"name": "one-too-many"}, headers=h)).status_code == 400


# ------------------------------------------------------------ protected API
async def test_ping_auth(client):
    h = await signup(client)
    k = await new_key(client, h)
    ok = await client.get("/v1/ping", headers=bearer(k["key"]))
    assert ok.status_code == 200 and ok.json()["ok"] is True
    assert ok.headers["X-RateLimit-Limit"] == "1000" and ok.headers["X-RateLimit-Remaining"] == "999"
    for bad in ({}, bearer("pme_nonsense"), bearer("garbage"), {"Authorization": "Basic abc"}):
        r = await client.get("/v1/ping", headers=bad)
        assert r.status_code == 401 and r.headers["WWW-Authenticate"] == "Bearer"
    # a dashboard session token is not an API key
    assert (await client.get("/v1/ping", headers=h)).status_code == 401


async def test_revoked_key_rejected_immediately(client):
    h = await signup(client)
    k = await new_key(client, h)
    assert (await client.get("/v1/ping", headers=bearer(k["key"]))).status_code == 200
    assert (await client.post(f"/api/keys/{k['id']}/revoke", headers=h)).status_code == 200
    assert (await client.get("/v1/ping", headers=bearer(k["key"]))).status_code == 401
    assert (await client.get("/api/keys", headers=h)).json()[0]["is_active"] is False


async def test_usage_and_last_used(client):
    h = await signup(client)
    k = await new_key(client, h)
    for _ in range(3):
        await client.get("/v1/ping", headers=bearer(k["key"]))
    u = (await client.get("/api/usage", headers=h)).json()
    assert u == {"used": 3, "limit": 1000, "remaining": 997, "percent": 0.3}
    assert (await client.get("/api/keys", headers=h)).json()[0]["last_used_at"] is not None


async def test_daily_limit_returns_429(client):
    h = await signup(client)
    k = await new_key(client, h)
    async with SessionLocal() as s:
        await s.execute(text("UPDATE customers SET daily_limit = 3"))
        await s.commit()
    codes = [(await client.get("/v1/ping", headers=bearer(k["key"]))).status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    r = await client.get("/v1/ping", headers=bearer(k["key"]))
    assert int(r.headers["Retry-After"]) > 0 and r.headers["X-RateLimit-Remaining"] == "0"
    # rejected requests are not counted
    assert (await client.get("/api/usage", headers=h)).json()["used"] == 3


async def test_daily_limit_is_per_customer_across_keys(client):
    h = await signup(client)
    k1, k2 = await new_key(client, h, "one"), await new_key(client, h, "two")
    async with SessionLocal() as s:
        await s.execute(text("UPDATE customers SET daily_limit = 2"))
        await s.commit()
    assert (await client.get("/v1/ping", headers=bearer(k1["key"]))).status_code == 200
    assert (await client.get("/v1/ping", headers=bearer(k2["key"]))).status_code == 200
    assert (await client.get("/v1/ping", headers=bearer(k1["key"]))).status_code == 429


async def test_limit_survives_redis_flush(client):
    h = await signup(client)
    k = await new_key(client, h)
    async with SessionLocal() as s:
        await s.execute(text("UPDATE customers SET daily_limit = 3"))
        await s.commit()
    for _ in range(2):
        await client.get("/v1/ping", headers=bearer(k["key"]))
    await redis.flushdb()                      # simulate a Redis restart
    assert (await client.get("/v1/ping", headers=bearer(k["key"]))).status_code == 200
    assert (await client.get("/v1/ping", headers=bearer(k["key"]))).status_code == 429


async def test_per_minute_burst_limit(client, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "2")
    h = await signup(client)
    k = await new_key(client, h)
    codes = [(await client.get("/v1/ping", headers=bearer(k["key"]))).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


async def test_logs_never_contain_the_key(client, caplog):
    h = await signup(client)
    k = await new_key(client, h)
    with caplog.at_level(logging.DEBUG):
        await client.get("/v1/ping", headers=bearer(k["key"]))
    assert k["key"] not in caplog.text


async def test_health_and_dashboard(client):
    assert (await client.get("/health")).json() == {"ok": True}
    page = await client.get("/")
    assert "Generate Key" in page.text and page.headers["X-Frame-Options"] == "DENY"
