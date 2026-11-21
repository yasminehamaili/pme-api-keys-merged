"""
enforce_limits: the FastAPI dependency that protects /v1/* endpoints.

Per request:  parse Bearer key -> look up HMAC hash -> per-minute burst limit
              -> per-customer daily limit -> record usage in Postgres.

Counters live in Redis (atomic, fast). Postgres usage_log is the durable record,
and is used to re-seed today's counter if Redis loses it (restart/flush).
"""
import logging
import os
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request, Response
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from db import get_session
from redis_client import redis
from security import authenticate_key, looks_like_api_key, parse_bearer

log = logging.getLogger("ratelimit")

# Atomic "increment unless already at the limit". Returns {allowed, count}.
_LUA = """
local cur = tonumber(redis.call('GET', KEYS[1]) or '0')
if cur >= tonumber(ARGV[1]) then return {0, cur} end
cur = redis.call('INCR', KEYS[1])
if redis.call('TTL', KEYS[1]) < 0 then redis.call('EXPIRE', KEYS[1], ARGV[2]) end
return {1, cur}
"""
_script = redis.register_script(_LUA)


def per_minute_limit() -> int:
    return int(os.environ.get("RATE_LIMIT_PER_MINUTE", "60"))


async def hit(key: str, limit: int, ttl_seconds: int) -> tuple[bool, int]:
    allowed, count = await _script(keys=[key], args=[limit, ttl_seconds])
    return bool(allowed), int(count)


def _unauthorized() -> HTTPException:
    return HTTPException(401, "Missing, invalid or revoked API key",
                         headers={"WWW-Authenticate": "Bearer"})


def _seconds_until_utc_midnight(now: datetime) -> int:
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(int((midnight - now).total_seconds()), 1)


async def _seed_daily(session: AsyncSession, customer_id, key: str, day) -> None:
    """If Redis has no counter for today, start it from what Postgres recorded."""
    if await redis.exists(key):
        return
    used = await session.scalar(
        text(
            "SELECT COALESCE(SUM(u.request_count), 0) FROM usage_log u "
            "JOIN api_keys k ON k.id = u.api_key_id "
            "WHERE k.customer_id = :c AND u.day = :d"
        ),
        {"c": customer_id, "d": day},
    )
    await redis.set(key, int(used or 0), ex=172800, nx=True)


async def enforce_limits(
    request: Request, response: Response, session: AsyncSession = Depends(get_session)
) -> dict:
    raw = parse_bearer(request.headers.get("authorization"))
    if not raw or not looks_like_api_key(raw):
        raise _unauthorized()
    auth = await authenticate_key(session, raw)
    if auth is None:
        raise _unauthorized()

    now = datetime.now(timezone.utc)
    day = now.date()
    daily_limit = auth["daily_limit"]
    try:
        sec = int(now.timestamp())
        ok, _ = await hit(f"rl:min:{auth['key_id']}:{sec // 60}", per_minute_limit(), 60)
        if not ok:
            raise HTTPException(429, "Too many requests per minute",
                                headers={"Retry-After": str(60 - sec % 60)})

        dkey = f"rl:day:{auth['customer_id']}:{day.isoformat()}"
        await _seed_daily(session, auth["customer_id"], dkey, day)
        ok, used = await hit(dkey, daily_limit, 172800)
        if not ok:
            raise HTTPException(
                429, "Daily request limit reached",
                headers={"Retry-After": str(_seconds_until_utc_midnight(now)),
                         "X-RateLimit-Limit": str(daily_limit), "X-RateLimit-Remaining": "0"},
            )
    except RedisError:
        log.exception("redis unavailable")
        raise HTTPException(503, "Rate limiter unavailable, try again shortly")

    # Durable usage record. last_used_at is only touched once a minute to avoid a hot row.
    await session.execute(
        text(
            "INSERT INTO usage_log (api_key_id, day, request_count) VALUES (:k, :d, 1) "
            "ON CONFLICT (api_key_id, day) DO UPDATE SET request_count = usage_log.request_count + 1"
        ),
        {"k": auth["key_id"], "d": day},
    )
    await session.execute(
        text(
            "UPDATE api_keys SET last_used_at = now() WHERE id = :k "
            "AND (last_used_at IS NULL OR last_used_at < now() - interval '60 seconds')"
        ),
        {"k": auth["key_id"]},
    )
    await session.commit()

    response.headers["X-RateLimit-Limit"] = str(daily_limit)
    response.headers["X-RateLimit-Remaining"] = str(max(daily_limit - used, 0))
    return {"customer_id": str(auth["customer_id"]), "key_id": str(auth["key_id"])}
