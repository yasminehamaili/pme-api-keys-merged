"""GET /api/report/sales with a fake ERP (httpx.MockTransport). Uses the real session auth + limiter."""
import httpx
import pytest

import erp_client
from conftest import signup
from erp_config import get_settings

ROWS = (
    # previous period (30 Aug .. 30 Sep): 400 from customers a, b, c, d
    [{"id": i, "number": f"P-{i}", "customer_id": c, "status": "posted",
      "issue_date": f"2026-09-{10 + i}", "total": "100"} for i, c in enumerate("abcd", 1)]
    # current period (1 .. 31 Oct): 432 from a, b (retention 2/4) + one draft that must not count
    + [{"id": 10, "number": "C-1", "customer_id": "a", "status": "posted",
        "issue_date": "2026-10-05", "total": "216"},
       {"id": 11, "number": "C-2", "customer_id": "b", "status": "posted",
        "issue_date": "2026-10-06", "total": "216"},
       {"id": 12, "number": "C-3", "customer_id": "a", "status": "draft",
        "issue_date": "2026-10-07", "total": "999"},
       {"id": 99, "number": "BAD", "issue_date": "not-a-date", "total": "1"}]      # skipped
)
PERIOD = "?date_from=2026-10-01&date_to=2026-10-31"
state = {"mode": "ok"}


def fake_erp(request: httpx.Request) -> httpx.Response:
    if state["mode"] == "down":
        raise httpx.ConnectError("refused")
    off, lim = int(request.url.params["offset"]), int(request.url.params["limit"])
    return httpx.Response(200, json={"data": ROWS[off:off + lim]})


@pytest.fixture(autouse=True)
async def erp(monkeypatch):
    for k, v in {"ERP_BASE_URL": "http://erp.test", "ERP_PAGE_SIZE": "3", "ERP_BACKOFF_BASE": "0",
                 "ERP_MAX_RETRIES": "1"}.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    state["mode"] = "ok"
    erp_client._client = erp_client.ERPClient(transport=httpx.MockTransport(fake_erp))
    yield
    await erp_client.close_erp_client()
    get_settings.cache_clear()


async def test_requires_a_dashboard_session(client):
    assert (await client.get("/api/report/sales" + PERIOD)).status_code == 401


async def test_report_numbers(client):
    r = await client.get("/api/report/sales" + PERIOD, headers=await signup(client))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kpis"]["revenue"] == 432.0 and body["kpis"]["previous_revenue"] == 400.0
    assert body["kpis"]["growth_pct"] == 8.0 and body["kpis"]["retention_pct"] == 50.0
    assert body["summary"] == "Revenue grew 8% on the previous 31 days; retention slipped below target."
    # 7 invoices in the two windows + 1 invalid row = 8; the draft and the invalid row are excluded
    assert body["data_quality"] == {"percent": 75.0, "rows_total": 8, "rows_excluded": 2}


async def test_retention_target_changes_the_sentence(client):
    r = await client.get("/api/report/sales" + PERIOD + "&retention_target=40", headers=await signup(client))
    assert r.json()["summary"].endswith("retention is on target.")


@pytest.mark.parametrize("query", ["?date_from=2026-10-01", "?date_from=2026-11-01&date_to=2026-10-01",
                                   PERIOD + "&retention_target=150"])
async def test_bad_input_is_rejected(client, query):
    r = await client.get("/api/report/sales" + query, headers=await signup(client))
    assert r.status_code == 422


async def test_erp_down_is_a_clean_502(client):
    state["mode"] = "down"
    r = await client.get("/api/report/sales" + PERIOD, headers=await signup(client))
    assert r.status_code == 502
