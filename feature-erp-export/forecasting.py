"""
Sales forecasting with chronological backtesting. Pure Python/numpy, no LLM.

Models: naive, seasonal_naive, moving_average, ses (simple exponential smoothing), holt (trend),
holt_winters (additive trend + seasonality). Parameters are chosen on the TRAINING window only.

Backtest = rolling origin: for each cut-off, fit on the past, forecast the next `h` periods,
compare with what really happened (never trains on the future). Metrics:
  MAE   mean |error|            RMSE  sqrt(mean error^2)
  WAPE  sum|error| / sum|actual| (None when actuals sum to 0)
  bias  sum(forecast - actual) / sum(actual)   (> 0 = over-forecasting)
The recommended model must beat the naive baseline, otherwise naive is recommended.
"""
import math
from itertools import product
from typing import Callable, Sequence

import numpy as np

MODELS = ("naive", "seasonal_naive", "moving_average", "ses", "holt", "holt_winters")
MAX_ORIGINS = 24
Z80 = 1.2816


class ForecastError(ValueError):
    """Bad input. Message is safe to show to API clients."""


# ------------------------------------------------------------------ metrics
def metrics(actual: Sequence[float], forecast: Sequence[float]) -> dict:
    a, f = np.asarray(actual, float), np.asarray(forecast, float)
    if a.shape != f.shape or a.size == 0:
        raise ForecastError("actual and forecast must have the same non-zero length")
    err = f - a
    denom = float(np.abs(a).sum())
    return {
        "mae": round(float(np.abs(err).mean()), 4),
        "rmse": round(float(math.sqrt((err ** 2).mean())), 4),
        "wape": None if denom == 0 else round(float(np.abs(err).sum()) / denom, 4),
        "bias": None if float(a.sum()) == 0 else round(float(err.sum()) / float(a.sum()), 4),
        "n": int(a.size),
    }


# ------------------------------------------------------------------- models
# Each takes (train: list[float], h: int, m: int) and returns h forecasts.
def _naive(y, h, m):
    return [y[-1]] * h


def _seasonal_naive(y, h, m):
    return [y[len(y) - m + (i % m)] for i in range(h)]


def _moving_average(y, h, m, window: int = 3):
    return [sum(y[-window:]) / min(window, len(y))] * h


def _ses_run(y, a):
    level, sse = y[0], 0.0
    for v in y[1:]:
        sse += (v - level) ** 2
        level = a * v + (1 - a) * level
    return sse, level


def _ses(y, h, m):
    alpha = min((0.1, 0.2, 0.3, 0.5, 0.7, 0.9), key=lambda a: _ses_run(y, a)[0])
    return [_ses_run(y, alpha)[1]] * h


def _holt_run(y, a, b):
    level, trend, sse = y[0], y[1] - y[0], 0.0
    for v in y[1:]:
        f = level + trend
        sse += (v - f) ** 2
        new = a * v + (1 - a) * (level + trend)
        trend = b * (new - level) + (1 - b) * trend
        level = new
    return sse, level, trend


def _holt(y, h, m):
    a, b = min(product((0.2, 0.5, 0.8), (0.05, 0.2, 0.5)), key=lambda p: _holt_run(y, *p)[0])
    _, level, trend = _holt_run(y, a, b)
    return [level + (i + 1) * trend for i in range(h)]


def _hw_run(y, m, a, b, g):
    level = sum(y[:m]) / m
    trend = (sum(y[m:2 * m]) - sum(y[:m])) / (m * m)
    seas = [y[i] - level for i in range(m)]
    sse = 0.0
    for t, v in enumerate(y):
        s = seas[t % m]
        sse += (v - (level + trend + s)) ** 2
        new = a * (v - s) + (1 - a) * (level + trend)
        trend = b * (new - level) + (1 - b) * trend
        seas[t % m] = g * (v - new) + (1 - g) * s
        level = new
    return sse, level, trend, seas


def _holt_winters(y, h, m):
    a, b, g = min(product((0.2, 0.5, 0.8), (0.05, 0.2), (0.1, 0.4, 0.8)),
                  key=lambda p: _hw_run(y, m, *p)[0])
    _, level, trend, seas = _hw_run(y, m, a, b, g)
    n = len(y)
    return [level + (i + 1) * trend + seas[(n + i) % m] for i in range(h)]


_FUNCS: dict[str, Callable] = {"naive": _naive, "seasonal_naive": _seasonal_naive,
                               "moving_average": _moving_average, "ses": _ses,
                               "holt": _holt, "holt_winters": _holt_winters}


def min_train(model: str, m: int) -> int:
    return {"naive": 2, "moving_average": 3, "ses": 3, "holt": 4,
            "seasonal_naive": m, "holt_winters": 2 * m}[model]


def forecast(model: str, y: Sequence[float], h: int, m: int, non_negative: bool = True) -> list[float]:
    out = _FUNCS[model](list(map(float, y)), h, m)
    return [max(v, 0.0) if non_negative else v for v in out]


# ---------------------------------------------------------------- backtesting
def backtest(model: str, y: Sequence[float], h: int, m: int, *, start: int,
             non_negative: bool = True) -> dict:
    """Rolling-origin backtest. `start` = first training size; keeps the last MAX_ORIGINS cut-offs."""
    y = list(map(float, y))
    origins = list(range(max(start, min_train(model, m)), len(y) - h + 1))[-MAX_ORIGINS:]
    if not origins:
        raise ForecastError("series too short to backtest")
    acts, fcs, by_step = [], [], [[] for _ in range(h)]
    for o in origins:
        pred = forecast(model, y[:o], h, m, non_negative)
        for k in range(h):
            acts.append(y[o + k])
            fcs.append(pred[k])
            by_step[k].append(y[o + k] - pred[k])
    return {"metrics": metrics(acts, fcs), "origins": len(origins), "errors_by_step": by_step}


def run(y: Sequence[float], h: int = 3, season_length: int | None = None,
        models: Sequence[str] | None = None, non_negative: bool = True) -> dict:
    y = [float(v) for v in y]
    if any(not math.isfinite(v) for v in y):
        raise ForecastError("series contains NaN or infinite values")
    if not (1 <= h <= 36):
        raise ForecastError("h must be between 1 and 36")
    m = int(season_length or 0)
    if m == 1 or m < 0 or m > 52:
        raise ForecastError("season_length must be 0/omitted (none) or between 2 and 52")
    wanted = list(models) if models else list(MODELS)
    bad = [x for x in wanted if x not in MODELS]
    if bad:
        raise ForecastError(f"unknown model(s): {', '.join(bad)}")
    n = len(y)
    if n < 6 + h:
        raise ForecastError(f"need at least {6 + h} observations for h={h} (got {n})")

    start = max(6, 2 * m)
    board, skipped = [], {}
    for name in dict.fromkeys(["naive"] + wanted):               # naive always runs as the baseline
        if name in ("seasonal_naive", "holt_winters") and not m:
            skipped[name] = "needs season_length"
            continue
        if n - h < max(start, min_train(name, m)):
            skipped[name] = "series too short"
            continue
        try:
            bt = backtest(name, y, h, m, start=start, non_negative=non_negative)
        except ForecastError as exc:
            skipped[name] = str(exc)
            continue
        board.append({"model": name, **bt["metrics"], "origins": bt["origins"], "_errs": bt["errors_by_step"]})

    def score(r):
        return r["wape"] if r["wape"] is not None else r["mae"]

    board.sort(key=score)
    chosen = board[0]                       # naive sorts first on ties, so a model must strictly beat it
    beats = chosen["model"] != "naive"

    point = forecast(chosen["model"], y, h, m, non_negative)
    intervals = []
    for k, p in enumerate(point):
        errs = chosen["_errs"][k]
        if len(errs) >= 5:
            sd = float(np.std(errs, ddof=1))
            lo, hi = p - Z80 * sd, p + Z80 * sd
            intervals.append({"lower": round(max(lo, 0) if non_negative else lo, 3), "upper": round(hi, 3)})
        else:
            intervals.append(None)

    notes = []
    if not beats:
        notes.append("No model beat the naive baseline in backtesting: forecast is no better than "
                     "repeating the last value. Treat as low reliability.")
    if chosen["origins"] < 8:
        notes.append(f"Only {chosen['origins']} backtest cut-offs: accuracy estimate is uncertain.")
    if m and n < 3 * m:
        notes.append(f"Less than 3 full seasons ({n} points, season {m}): seasonal patterns are weakly supported.")
    naive_row = next(r for r in board if r["model"] == "naive")
    skill = None
    if chosen["wape"] is not None and naive_row["wape"]:
        skill = round(1 - chosen["wape"] / naive_row["wape"], 3)     # share of naive error removed
    if not beats or chosen["origins"] < 8:
        confidence = "low"
    elif skill is None or skill < 0.4 or (chosen["wape"] or 1) > 0.2:
        confidence = "medium"
    else:
        confidence = "high"
    for r in board:
        r.pop("_errs")
    return {
        "horizon": h, "season_length": m or None, "n_observations": n,
        "recommended_model": chosen["model"], "beats_naive": bool(beats),
        "skill_vs_naive": skill, "confidence": confidence,
        "forecast": [round(v, 3) for v in point], "interval_80": intervals,
        "backtest": {"method": "rolling origin, expanding window", "leaderboard": board,
                     "not_run": skipped},
        "notes": notes,
    }
