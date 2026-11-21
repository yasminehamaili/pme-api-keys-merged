"""
PME API Keys backend  (FastAPI + Postgres + Redis)

Run:        python migrate.py && uvicorn main:app --reload
Docs UI:    http://127.0.0.1:8000/docs
Dashboard:  http://127.0.0.1:8000/

Two kinds of credentials:
  * Dashboard session token  (Authorization: Bearer pmes.…)  -> /api/*  (manage your keys)
  * API key                  (Authorization: Bearer pme_…)   -> /v1/*   (what customers' apps call)
"""
import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, StringConstraints
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

import security
from db import engine, get_session
from erp_client import ERPError, close_erp_client, get_erp_client
from erp_config import ERPNotConfigured, get_settings
from erp_export import ExportTooLarge, open_rows
from erp_routes import http_error
from erp_routes import router as erp_router
from erp_schemas import RESOURCES
from logging_config import setup_logging
from ratelimit import enforce_limits, hit
from redis_client import redis
from report import SalesAccumulator, resolve_period

log = logging.getLogger("api")

MAX_ACTIVE_KEYS = 10


@asynccontextmanager
async def lifespan(_: FastAPI):
    setup_logging()
    security.check_config()          # fail at startup if a secret is missing
    yield
    await close_erp_client()
    await redis.aclose()
    await engine.dispose()


app = FastAPI(title="PME API Keys", lifespan=lifespan)
app.include_router(erp_router)      # /v1/erp/*  (API-key auth + rate limits)


@app.middleware("http")
async def access_log(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    log.info("request", extra={"method": request.method, "path": request.url.path,
                               "status": response.status_code,
                               "ms": round((time.perf_counter() - start) * 1000, 1)})
    return response


# ------------------------------------------------------------ request bodies
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
Email = Annotated[str, StringConstraints(strip_whitespace=True, to_lower=True, max_length=254,
                                         pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
Password = Annotated[str, StringConstraints(min_length=10, max_length=128)]


class KeyName(BaseModel):
    name: Name


class Credentials(BaseModel):
    email: Email
    password: Password


# --------------------------------------------------- who is the customer?
async def current_customer(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> dict:
    token = security.parse_bearer(authorization)
    cid = security.verify_session_token(token) if token else None
    row = None
    if cid:
        row = (await session.execute(
            text("SELECT id, daily_limit FROM customers WHERE id = :id"), {"id": cid}
        )).first()
    if row is None:
        raise HTTPException(401, "Not signed in", headers={"WWW-Authenticate": "Bearer"})
    return {"id": row.id, "daily_limit": row.daily_limit}


Customer = Annotated[dict, Depends(current_customer)]
Session = Annotated[AsyncSession, Depends(get_session)]


# ================================ ACCOUNTS ================================
@app.post("/api/auth/register", status_code=201)
async def register(body: Credentials, session: Session):
    try:
        cid = await session.scalar(
            text("INSERT INTO customers (email, password_hash) VALUES (:e, :p) RETURNING id"),
            {"e": body.email, "p": security.hash_password(body.password)},
        )
        await session.commit()
    except IntegrityError:
        raise HTTPException(409, "An account with this email already exists")
    return {"token": security.create_session_token(cid)}


@app.post("/api/auth/login")
async def login(body: Credentials, request: Request, session: Session):
    ip = request.client.host if request.client else "unknown"
    try:  # brute-force guard: 10 attempts / 15 min per IP + email
        ok, _ = await hit(f"rl:login:{ip}:{body.email}", 10, 900)
    except RedisError:
        raise HTTPException(503, "Try again shortly")
    if not ok:
        raise HTTPException(429, "Too many login attempts, try again later",
                            headers={"Retry-After": "900"})
    row = (await session.execute(
        text("SELECT id, password_hash FROM customers WHERE email = :e"), {"e": body.email}
    )).first()
    valid = security.verify_password(body.password, row.password_hash if row else security.DUMMY_HASH)
    if not row or not valid:
        raise HTTPException(401, "Wrong email or password")
    return {"token": security.create_session_token(row.id)}


# ============================ KEY LIFECYCLE ===============================
@app.get("/api/keys")
async def list_keys(customer: Customer, session: Session):
    """List this customer's keys. NEVER returns key_hash."""
    rows = (await session.execute(
        text("SELECT id, name, key_prefix, is_active, last_used_at, created_at "
             "FROM api_keys WHERE customer_id = :c ORDER BY created_at DESC"),
        {"c": customer["id"]},
    )).mappings().all()
    return [dict(r) for r in rows]


@app.post("/api/keys", status_code=201)
async def create_key(body: KeyName, customer: Customer, session: Session):
    """The full key is returned ONE time only -- we store just its HMAC."""
    active = await session.scalar(
        text("SELECT count(*) FROM api_keys WHERE customer_id = :c AND is_active"),
        {"c": customer["id"]},
    )
    if active >= MAX_ACTIVE_KEYS:
        raise HTTPException(400, f"Limit of {MAX_ACTIVE_KEYS} active keys reached. Revoke one first.")
    full_key, prefix, key_hash = security.generate_api_key()
    key_id = await session.scalar(
        text("INSERT INTO api_keys (customer_id, name, key_prefix, key_hash) "
             "VALUES (:c, :n, :p, :h) RETURNING id"),
        {"c": customer["id"], "n": body.name, "p": prefix, "h": key_hash},
    )
    await session.commit()
    return {"id": key_id, "name": body.name, "key_prefix": prefix, "key": full_key}


@app.patch("/api/keys/{key_id}")
async def rename_key(key_id: uuid.UUID, body: KeyName, customer: Customer, session: Session):
    found = await session.scalar(
        text("UPDATE api_keys SET name = :n WHERE id = :id AND customer_id = :c RETURNING id"),
        {"n": body.name, "id": key_id, "c": customer["id"]},
    )
    if found is None:
        raise HTTPException(404, "Key not found")   # also hides other customers' keys
    await session.commit()
    return {"id": key_id, "name": body.name}


@app.post("/api/keys/{key_id}/revoke")
async def revoke_key(key_id: uuid.UUID, customer: Customer, session: Session):
    """Revoke = is_active false. Row is kept for audit. Takes effect on the next request."""
    found = await session.scalar(
        text("UPDATE api_keys SET is_active = false, revoked_at = COALESCE(revoked_at, now()) "
             "WHERE id = :id AND customer_id = :c RETURNING id"),
        {"id": key_id, "c": customer["id"]},
    )
    if found is None:
        raise HTTPException(404, "Key not found")
    await session.commit()
    return {"id": key_id, "is_active": False}


# ============================ TRAFFIC & LIMITS ============================
@app.get("/api/usage")
async def usage(customer: Customer, session: Session):
    used = int(await session.scalar(
        text("SELECT COALESCE(SUM(u.request_count), 0) FROM usage_log u "
             "JOIN api_keys k ON k.id = u.api_key_id "
             "WHERE k.customer_id = :c AND u.day = (now() AT TIME ZONE 'utc')::date"),
        {"c": customer["id"]},
    ))
    limit = customer["daily_limit"]
    return {"used": used, "limit": limit, "remaining": max(limit - used, 0),
            "percent": round(min(used / limit, 1) * 100, 1) if limit else 100.0}


# ============================== SALES REPORT ==============================
REPORTS_PER_MINUTE = 10


@app.get("/api/report/sales")
async def sales_report(
    customer: Customer,
    date_from: date | None = None,
    date_to: date | None = None,
    retention_target: Annotated[float, Query(ge=0, le=100)] = 60.0,
):
    """Numbers for the dashboard's Report Panel (dashboard session token, like /api/usage).

    Reads invoices from the ERP for the period AND the previous period of the same length.
    No dates -> the last 31 days. Retention target is a percentage.
    """
    try:
        period = resolve_period(date_from, date_to, datetime.now(timezone.utc).date())
    except ValueError as exc:
        raise HTTPException(422, str(exc))

    sec = int(time.time())
    try:
        ok, _ = await hit(f"rl:report:{customer['id']}:{sec // 60}", REPORTS_PER_MINUTE, 60)
    except RedisError:
        log.exception("redis unavailable")
        raise HTTPException(503, "Rate limiter unavailable, try again shortly")
    if not ok:
        raise HTTPException(429, "Too many reports, slow down",
                            headers={"Retry-After": str(60 - sec % 60)})

    ctx = {"customer_id": customer["id"], "resource": "invoices", "report": "sales"}
    acc = SalesAccumulator(period)
    try:
        rows, stats = await open_rows(
            get_erp_client(), RESOURCES["invoices"],
            {"date_from": period.previous.start, "date_to": period.end},
            None, get_settings().max_export_rows)
        async for row in rows:
            acc.add(row)
    except (ERPError, ERPNotConfigured, ExportTooLarge) as exc:
        raise http_error(exc, ctx)
    return acc.result(stats.skipped, retention_target)


# ============================ PROTECTED API ===============================
@app.get("/v1/ping")
async def ping(auth=Depends(enforce_limits)):
    """Example protected endpoint. Add real endpoints the same way: Depends(enforce_limits)."""
    return {"ok": True, "customer_id": auth["customer_id"]}


@app.get("/health")
async def health(session: Session):
    await session.execute(text("SELECT 1"))
    await redis.ping()
    return {"ok": True}


# ============================ DASHBOARD PAGE ==============================
@app.get("/", include_in_schema=False)
async def dashboard():
    return FileResponse(Path(__file__).parent / "index.html")


@app.get("/report", include_in_schema=False)
async def report_page():
    return FileResponse(Path(__file__).parent / "report-panel.html")
