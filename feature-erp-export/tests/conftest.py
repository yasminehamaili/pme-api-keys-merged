"""
Tests run against REAL Postgres + Redis, but only against throwaway ones:
  TEST_DATABASE_URL  must point to a database whose name ends in "_test"
  TEST_REDIS_URL     defaults to redis db 15 (it is flushed between tests)
"""
import os

import pytest

_db = os.environ.get("TEST_DATABASE_URL")
if not _db or not _db.split("?")[0].rstrip("/").endswith("_test"):
    pytest.exit("Set TEST_DATABASE_URL to a database named *_test "
                "(e.g. postgresql+asyncpg://user:pw@localhost/pme_test)", returncode=2)

os.environ["DATABASE_URL"] = _db
os.environ["REDIS_URL"] = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")
os.environ["KEY_PEPPER"] = "test-pepper-" + "p" * 40
os.environ["SESSION_SECRET"] = "test-session-" + "s" * 40
os.environ["RATE_LIMIT_PER_MINUTE"] = "60"

import httpx  # noqa: E402
from sqlalchemy import text  # noqa: E402

from db import SessionLocal  # noqa: E402
from main import app  # noqa: E402
from migrate import apply  # noqa: E402
from redis_client import redis  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
async def _schema():
    await apply()


@pytest.fixture(autouse=True)
async def _clean():
    async with SessionLocal() as s:
        await s.execute(text("TRUNCATE usage_log, api_keys, customers CASCADE"))
        await s.commit()
    await redis.flushdb()
    yield


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def signup(client, email="a@example.com", password="correct-horse-1"):
    r = await client.post("/api/auth/register", json={"email": email, "password": password})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


async def new_key(client, headers, name="Prod"):
    r = await client.post("/api/keys", json={"name": name}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()
