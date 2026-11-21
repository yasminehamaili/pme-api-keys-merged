"""Planning formulas + endpoints. No DB/Redis: `pytest --noconftest tests/test_planning.py`."""
import os

os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://x:y@localhost/x_test")

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402

import planning  # noqa: E402
import planning_routes  # noqa: E402
from ratelimit import enforce_limits  # noqa: E402


def test_safety_stock_and_rop_by_hand():
    # d=100, sd=20, L=4, sL=0 -> SS = 1.6449 * sqrt(4*400) = 1.6449*40 = 65.79 -> 66 ; ROP = 400+65.79 = 465.79 -> 466
    ss = planning.safety_stock(100, 20, 4, 0, 0.95)
    assert ss["safety_stock"] == 66 and ss["z"] == 1.6449
    r = planning.reorder_point(100, 20, 4, 0, 0.95)
    assert r["reorder_point"] == 466 and r["demand_during_lead_time"] == 400


def test_lead_time_variability_raises_safety_stock():
    base = planning.safety_stock(100, 20, 4, 0)["safety_stock"]
    assert planning.safety_stock(100, 20, 4, 1)["safety_stock"] > base       # d^2 * sigma_L^2 term
    assert planning.safety_stock(100, 20, 4, 0, 0.99)["safety_stock"] > base  # higher service level


def test_zero_variability_means_no_safety_stock():
    assert planning.safety_stock(50, 0, 7, 0)["safety_stock"] == 0
    assert planning.reorder_point(50, 0, 7, 0)["reorder_point"] == 350


def test_reorder_decision_uses_inventory_position():
    s = planning.reorder_status(on_hand=300, reserved=50, on_order=100, rop=466)
    assert s["inventory_position"] == 350 and s["should_reorder"] and s["shortfall"] == 116
    assert not planning.reorder_status(500, 0, 0, 466)["should_reorder"]


def test_net_production_lot_rounding():
    r = planning.net_production(gross_requirement=1000, on_hand=300, in_pipeline=200,
                                safety_stock_target=100, reserved=50, lot_size=100)
    assert r["net_requirement"] == 650 and r["quantity_to_produce"] == 700
    assert planning.net_production(100, 500)["quantity_to_produce"] == 0      # enough stock
    assert planning.net_production(10, 0, lot_size=1, min_batch=50)["quantity_to_produce"] == 50


def test_input_validation():
    for bad in (lambda: planning.safety_stock(-1, 1, 1, 1), lambda: planning.safety_stock(1, 1, 1, 1, 1.0),
                lambda: planning.safety_stock(1, float("nan"), 1, 1), lambda: planning.net_production(1, 1, lot_size=0),
                lambda: planning.mean_std([5], "x")):
        with pytest.raises(planning.PlanningError):
            bad()


@pytest.fixture
async def api():
    app = FastAPI()
    app.include_router(planning_routes.router)
    app.dependency_overrides[enforce_limits] = lambda: {"customer_id": "c", "key_id": "k"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def test_endpoints(api):
    r = await api.post("/v1/planning/reorder-point", json={
        "daily_demand": 100, "demand_std": 20, "lead_time_days": 4, "lead_time_std": 0, "on_hand": 300})
    assert r.status_code == 200 and r.json()["reorder_point"] == 466 and r.json()["status"]["should_reorder"]
    r = await api.post("/v1/planning/reorder-point", json={
        "demand_history": [90, 110, 100, 95, 105], "lead_time_history": [4, 4, 5, 3]})
    assert r.status_code == 200 and r.json()["derived_from_history"] == {"demand": True, "lead_time": True}
    assert (await api.post("/v1/planning/reorder-point", json={"daily_demand": 1})).status_code == 422
    r = await api.post("/v1/planning/net-production", json={"gross_requirement": 1000, "on_hand": 300, "lot_size": 100})
    assert r.json()["quantity_to_produce"] == 700
    assert (await api.post("/v1/planning/net-production", json={"gross_requirement": -1, "on_hand": 0})).status_code == 422
