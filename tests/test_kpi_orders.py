"""Order-based KPI endpoints with a fake ERP. No DB/Redis needed: the auth guard is overridden.
Run alone with:  pytest --noconftest tests/test_kpi_orders.py"""
import os

os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://x:y@localhost/x_test")

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402

import erp_client  # noqa: E402
import erp_routes  # noqa: E402
import kpi_routes  # noqa: E402
from erp_config import get_settings  # noqa: E402
from ratelimit import enforce_limits  # noqa: E402

DATA = {
    "/api/sales-orders": [
        {"id": 1, "number": "S1", "order_date": "2026-03-02", "total": 100, "cogs": 60, "returned": False, "state": "sale"},
        {"id": 2, "number": "S2", "order_date": "2026-03-03", "total": 100, "cogs": 80, "returned": True, "state": "sale"},
        {"id": 3, "number": "S3", "order_date": "2026-03-04", "total": 500, "state": "sale"},        # no cost
        {"id": 4, "number": "S4", "order_date": "2026-03-05", "total": 9, "state": "cancel"},        # excluded
    ],
    "/api/purchase-orders": [
        {"id": 1, "number": "P1", "order_date": "2026-03-01", "promised_date": "2026-03-10", "received_date": "2026-03-09",
         "quantity_ordered": 10, "unit_price": 11, "standard_price": 10},
        {"id": 2, "number": "P2", "order_date": "2026-03-02", "promised_date": "2026-03-10", "received_date": "2026-03-14",
         "quantity_ordered": 10, "unit_price": 10, "standard_price": 10},
        {"id": 3, "number": "P3", "order_date": "2026-03-03", "promised_date": "2026-03-08"},        # open + overdue
        {"id": 4, "number": "P4", "order_date": "2026-03-04", "received_date": "2026-03-06"},        # no promise
    ],
    "/api/production-orders": [
        {"id": 1, "number": "M1", "start_date": "2026-03-01", "planned_quantity": 100, "produced_quantity": 90,
         "defective_quantity": 9, "standard_hours": 9, "actual_hours": 10},
        {"id": 2, "number": "M2", "start_date": "2026-03-02", "planned_quantity": 100, "produced_quantity": 100,
         "defective_quantity": 1},
        {"id": 3, "number": "M3", "start_date": "2026-03-03", "planned_quantity": 100},               # unknown result
    ],
}


def fake_erp(req: httpx.Request) -> httpx.Response:
    off = int(req.url.params["offset"])
    return httpx.Response(200, json={"data": DATA[req.url.path][off:]})


@pytest.fixture
async def api(monkeypatch):
    monkeypatch.setenv("ERP_BASE_URL", "http://erp.test")
    get_settings.cache_clear()
    erp_client._client = erp_client.ERPClient(transport=httpx.MockTransport(fake_erp))
    app = FastAPI()
    app.include_router(kpi_routes.router)
    app.dependency_overrides[enforce_limits] = lambda: {"customer_id": "c", "key_id": "k"}
    app.dependency_overrides[erp_routes.export_guard] = lambda: {"customer_id": "c", "key_id": "k"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c
    await erp_client.close_erp_client()
    get_settings.cache_clear()


def by_name(r):
    return {k["name"]: k for k in r.json()["kpis"]}


Q = "?date_from=2026-03-01&date_to=2026-03-31"


async def test_sales_orders(api):
    r = await api.get("/v1/kpi/sales-orders" + Q)
    k = by_name(r)
    assert k["gross_margin"]["value"] == "30.00"              # (200-140)/200, order without cost excluded
    assert r.json()["data_quality"]["margin_coverage"] == "2/3"
    assert k["return_rate"]["value"] == "33.33" and k["return_rate"]["evidence_ids"] == ["2"]


async def test_procurement(api):
    r = await api.get("/v1/kpi/procurement" + Q)
    k = by_name(r)
    assert k["otd"]["value"] == "50.00" and k["otd"]["evidence_ids"] == ["2"]   # P2 late; P4 not judged
    assert k["lead_time"]["value"] == "7.3"                                      # (8 + 12 + 2) / 3 days
    assert k["price_variance"]["value"] == "5.00"                                # 210 vs 200
    assert r.json()["overdue_open_orders"]["sample_ids"] == ["3"]
    assert r.json()["data_quality"]["delivered_without_promised_date"] == 1


async def test_production(api):
    r = await api.get("/v1/kpi/production" + Q)
    k = by_name(r)
    assert k["completion_rate"]["value"] == "95.00"           # 190/200; M3 unknown, not counted as 0
    assert k["completion_rate"]["evidence_ids"] == ["1"]
    assert k["defect_rate"]["value"] == "5.26"                # 10/190
    assert k["efficiency"]["value"] == "90.00"
    assert r.json()["data_quality"] == {"orders": 3, "orders_with_results": 2, "skipped_invalid_records": 0}


async def test_bad_range(api):
    assert (await api.get("/v1/kpi/procurement?date_from=2026-04-01&date_to=2026-03-01")).status_code == 422
