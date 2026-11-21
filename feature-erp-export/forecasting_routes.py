"""
POST /v1/forecast/sales   {series, h, season_length, models, non_negative}

series = one value per period (e.g. monthly revenue, oldest first). Returns the recommended
model's forecast with an 80% interval, the backtest leaderboard (MAE, RMSE, WAPE, bias) and
a confidence level. Pure computation, nothing stored.
"""
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import forecasting
from ratelimit import enforce_limits

router = APIRouter(prefix="/v1/forecast", tags=["forecast"], dependencies=[Depends(enforce_limits)])

Num = Annotated[float, Field(ge=-1e12, le=1e12, allow_inf_nan=False)]


class ForecastIn(BaseModel):
    series: list[Num] = Field(min_length=8, max_length=1000, description="oldest first")
    h: int = Field(3, ge=1, le=36, description="periods to forecast")
    season_length: int | None = Field(None, ge=2, le=52, description="e.g. 12 for monthly data")
    models: list[Literal["naive", "seasonal_naive", "moving_average", "ses", "holt", "holt_winters"]] | None = None
    non_negative: bool = True


@router.post("/sales")
def forecast_sales(body: ForecastIn):
    try:
        return forecasting.run(body.series, body.h, body.season_length, body.models, body.non_negative)
    except forecasting.ForecastError as exc:
        raise HTTPException(422, str(exc))
