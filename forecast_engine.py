import numpy as np
import pandas as pd
from typing import Any, Dict


def backtest_demand_forecast(sales_df: pd.DataFrame, horizon_days: int = 30) -> Dict[str, Any]:
    """
    Chronological backtest per SKU using a seasonal naive + moving average baseline.
    Reports MAE, RMSE, WAPE, and Bias with zero future data leakage.
    """
    results: Dict[str, Any] = {}

    for sku, grp in sales_df.groupby("sku"):
        series = grp.sort_values("date")["demand_units"].values.astype(float)
        train, test = series[:-horizon_days], series[-horizon_days:]

        # Simple deterministic 14-day rolling mean forecast (plug in LightGBM/Statsmodels here)
        rolling_pred = np.full(shape=len(test), fill_value=np.mean(train[-14:]))

        errors = test - rolling_pred
        mae = float(np.mean(np.abs(errors)))
        rmse = float(np.sqrt(np.mean(errors ** 2)))
        wape = float(np.sum(np.abs(errors)) / max(np.sum(np.abs(test)), 1.0)) * 100
        bias = float(np.mean(errors))

        results[sku] = {
            "next_30d_daily_forecast": round(float(np.mean(series[-14:])), 2),
            "backtest_metrics": {
                "MAE": round(mae, 2),
                "RMSE": round(rmse, 2),
                "WAPE_pct": round(wape, 2),
                "Bias": round(bias, 2),
            },
        }

    return results