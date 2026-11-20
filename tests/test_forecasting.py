"""Forecasting + backtesting. No DB/Redis: `pytest --noconftest tests/test_forecasting.py`."""
import math
import os

os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://x:y@localhost/x_test")

import httpx  # noqa: E402
import numpy as np  # noqa: E402
import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402

import forecasting as fc  # noqa: E402
import forecasting_routes  # noqa: E402
from ratelimit import enforce_limits  # noqa: E402


def test_metrics_by_hand():
    m = fc.metrics([100, 200], [110, 180])
    assert m["mae"] == 15 and m["rmse"] == round(math.sqrt(250), 4)
    assert m["wape"] == 0.1 and m["bias"] == round(-10 / 300, 4)       # under-forecast overall
    assert fc.metrics([0, 0], [1, 1])["wape"] is None                   # undefined, not a fake number


def test_models_on_simple_series():
    y = [10, 12, 14, 16, 18, 20]
    assert fc.forecast("naive", y, 2, 0) == [20, 20]
    assert fc.forecast("moving_average", y, 1, 0) == [18]
    holt = fc.forecast("holt", y, 2, 0)
    assert holt[0] == pytest.approx(22, abs=0.5) and holt[1] > holt[0]   # follows the trend
    assert fc.forecast("seasonal_naive", [1, 2, 3, 4, 1, 2, 3, 4], 5, 4) == [1, 2, 3, 4, 1]


def season(n, m=12, trend=2.0, amp=30.0, noise=0.0, seed=0):
    rng = np.random.default_rng(seed)
    return [200 + trend * t + amp * math.sin(2 * math.pi * t / m) + rng.normal(0, noise) for t in range(n)]


def test_seasonal_series_picks_a_seasonal_model_and_beats_naive():
    r = fc.run(season(60, noise=3), h=3, season_length=12)
    assert r["recommended_model"] in ("holt_winters", "seasonal_naive") and r["beats_naive"]
    naive = next(x for x in r["backtest"]["leaderboard"] if x["model"] == "naive")
    best = r["backtest"]["leaderboard"][0]
    assert best["wape"] < naive["wape"]
    assert len(r["forecast"]) == 3 and all(v >= 0 for v in r["forecast"])
    assert r["skill_vs_naive"] > 0.4 and r["confidence"] == "high"


def test_random_noise_falls_back_to_naive_family_and_says_so():
    rng = np.random.default_rng(5)
    y = list(100 + rng.normal(0, 20, 40))
    r = fc.run(y, h=2)
    assert r["confidence"] != "high"                        # small error alone is not skill
    if not r["beats_naive"]:
        assert r["recommended_model"] == "naive" and any("naive baseline" in n for n in r["notes"])


def test_backtest_never_uses_the_future():
    # a huge spike in the LAST point must not influence forecasts made before it
    y = [100.0] * 20 + [10_000.0]
    bt = fc.backtest("naive", y, 1, 0, start=6)
    # the only origin that sees the spike as an actual is the last one; earlier forecasts are exactly 100
    assert bt["errors_by_step"][0][:-1] == [0.0] * (len(bt["errors_by_step"][0]) - 1)
    assert bt["errors_by_step"][0][-1] == 9900.0


def test_validation():
    with pytest.raises(fc.ForecastError):
        fc.run([1, 2, 3], h=3)
    with pytest.raises(fc.ForecastError):
        fc.run([1.0] * 20, h=0)
    with pytest.raises(fc.ForecastError):
        fc.run([1.0] * 19 + [float("nan")], h=2)
    r = fc.run([5.0] * 12, h=2, models=["seasonal_naive"])              # no season_length
    assert r["backtest"]["not_run"]["seasonal_naive"] == "needs season_length"
    assert r["recommended_model"] == "naive"


def test_constant_series_is_perfect():
    r = fc.run([50.0] * 15, h=3)
    assert r["forecast"] == [50.0] * 3 and r["backtest"]["leaderboard"][0]["wape"] == 0.0


@pytest.fixture
async def api():
    app = FastAPI()
    app.include_router(forecasting_routes.router)
    app.dependency_overrides[enforce_limits] = lambda: {"customer_id": "c", "key_id": "k"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def test_endpoint(api):
    r = await api.post("/v1/forecast/sales", json={"series": season(48, noise=2), "h": 4, "season_length": 12})
    assert r.status_code == 200 and len(r.json()["forecast"]) == 4 and "leaderboard" in r.json()["backtest"]
    assert (await api.post("/v1/forecast/sales", json={"series": [1, 2, 3]})).status_code == 422
    assert (await api.post("/v1/forecast/sales", json={"series": [1.0] * 10, "models": ["magic"]})).status_code == 422
