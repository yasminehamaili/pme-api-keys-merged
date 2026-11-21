"""ERP export endpoints, with a fake ERP (httpx.MockTransport). Uses the real auth + rate limiter."""
import io
import zipfile

import httpx
import pytest

import erp_client
from conftest import new_key, signup
from erp_config import get_settings

ROWS = [
    {"id": i, "number": f"INV-{i}", "customer_name": "=cmd|' /C calc'!A0" if i == 2 else "Acme",
     "status": "posted", "currency": "usd", "issue_date": "2026-01-15", "total": "1,000.50"}
    for i in range(1, 8)
] + [{"id": 99, "number": "BAD", "issue_date": "not-a-date", "total": "1"}]   # must be skipped

state = {"mode": "ok"}


def fake_erp(request: httpx.Request) -> httpx.Response:
    if state["mode"] == "down":
        raise httpx.ConnectError("refused")
    if state["mode"] == "timeout":
        raise httpx.ReadTimeout("slow")
    off, lim = int(request.url.params["offset"]), int(request.url.params["limit"])
    return httpx.Response(200, json={"data": ROWS[off:off + lim]})


@pytest.fixture(autouse=True)
async def erp(monkeypatch):
    for k, v in {"ERP_BASE_URL": "http://erp.test", "ERP_PAGE_SIZE": "3", "ERP_BACKOFF_BASE": "0",
                 "ERP_MAX_RETRIES": "1", "ERP_EXPORT_PER_MINUTE": "50"}.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    state["mode"] = "ok"
    erp_client._client = erp_client.ERPClient(transport=httpx.MockTransport(fake_erp))
    yield
    await erp_client.close_erp_client()
    get_settings.cache_clear()


async def auth(client):
    key = (await new_key(client, await signup(client)))["key"]
    return {"Authorization": f"Bearer {key}"}


async def test_requires_api_key(client):
    r = await client.get("/v1/erp/export/invoices")
    assert r.status_code == 401


async def test_csv_is_paginated_validated_and_injection_safe(client):
    r = await client.get("/v1/erp/export/invoices?format=csv", headers=await auth(client))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert r.headers["content-disposition"].startswith('attachment; filename="invoices_')
    lines = r.text.lstrip("﻿").splitlines()
    assert len(lines) == 1 + 7                      # header + 7 good rows, the bad row is skipped
    assert ",USD," in lines[1] and ",1000.50," in lines[1]
    assert "'=cmd" in lines[2]                      # formula neutralised


async def test_json_and_max_rows(client):
    r = await client.get("/v1/erp/export/invoices?format=json&max_rows=4", headers=await auth(client))
    assert r.status_code == 200 and len(r.json()) == 4


async def test_bundle_zip_and_xlsx(client):
    h = await auth(client)
    r = await client.get("/v1/erp/export-bundle?resources=invoices&format=csv", headers=h)
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert set(z.namelist()) == {"invoices.csv", "_export_report.json"}
    r = await client.get("/v1/erp/export/invoices?format=xlsx", headers=h)
    assert r.status_code == 200 and r.headers["x-export-skipped-rows"] == "1"


@pytest.mark.parametrize("mode,status", [("down", 502), ("timeout", 504)])
async def test_erp_failures_are_clean_http_errors(client, mode, status):
    state["mode"] = mode
    r = await client.get("/v1/erp/export/invoices", headers=await auth(client))
    assert r.status_code == status
    assert "detail" in r.json()


async def test_unknown_resource_and_filter(client):
    h = await auth(client)
    assert (await client.get("/v1/erp/export/nope", headers=h)).status_code == 404
    assert (await client.get("/v1/erp/export/inventory?date_from=2026-01-01", headers=h)).status_code == 422
