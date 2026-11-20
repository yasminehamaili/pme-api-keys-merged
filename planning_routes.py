"""
Planning endpoints (pure computation, nothing stored).

  POST /v1/planning/reorder-point   safety stock + ROP (+ reorder decision if stock is given)
  POST /v1/planning/net-production  quantity to produce for a planning horizon
Demand and lead time can be given as summary numbers OR as raw history series.
"""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator

import planning
from ratelimit import enforce_limits

router = APIRouter(prefix="/v1/planning", tags=["planning"], dependencies=[Depends(enforce_limits)])

Num = Annotated[float, Field(ge=0, le=1e12, allow_inf_nan=False)]
Series = Annotated[list[Num], Field(min_length=2, max_length=10_000)]


class ReorderIn(BaseModel):
    service_level: float = Field(0.95, ge=0.5, lt=1)
    # either summary numbers...
    daily_demand: Num | None = None
    demand_std: Num | None = None
    lead_time_days: Num | None = None
    lead_time_std: Num | None = None
    # ...or raw history (daily demand per day; lead time per past order, in days)
    demand_history: Series | None = None
    lead_time_history: Series | None = None
    # optional: current stock to get a reorder decision
    on_hand: Num | None = None
    reserved: Num = 0
    on_order: Num = 0

    @model_validator(mode="after")
    def _complete(self):
        if self.demand_history is None and (self.daily_demand is None or self.demand_std is None):
            raise ValueError("give demand_history, or both daily_demand and demand_std")
        if self.lead_time_history is None and (self.lead_time_days is None or self.lead_time_std is None):
            raise ValueError("give lead_time_history, or both lead_time_days and lead_time_std")
        return self


class NetProductionIn(BaseModel):
    gross_requirement: Num
    on_hand: Num
    in_pipeline: Num = 0
    safety_stock_target: Num = 0
    reserved: Num = 0
    lot_size: float = Field(1, gt=0, le=1e12, allow_inf_nan=False)
    min_batch: Num = 0


@router.post("/reorder-point")
def reorder_point(body: ReorderIn):
    try:
        if body.demand_history is not None:
            d, sd = planning.mean_std(body.demand_history, "demand_history")
        else:
            d, sd = body.daily_demand, body.demand_std
        if body.lead_time_history is not None:
            lt, slt = planning.mean_std(body.lead_time_history, "lead_time_history")
        else:
            lt, slt = body.lead_time_days, body.lead_time_std
        out = planning.reorder_point(d, sd, lt, slt, body.service_level)
        out["derived_from_history"] = {"demand": body.demand_history is not None,
                                       "lead_time": body.lead_time_history is not None}
        if body.on_hand is not None:
            out["status"] = planning.reorder_status(body.on_hand, body.reserved, body.on_order,
                                                    out["reorder_point"])
        return out
    except planning.PlanningError as exc:
        raise HTTPException(422, str(exc))


@router.post("/net-production")
def net_production(body: NetProductionIn):
    try:
        return planning.net_production(**body.model_dump())
    except planning.PlanningError as exc:
        raise HTTPException(422, str(exc))
