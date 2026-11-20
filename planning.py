"""
Inventory & production planning formulas (step 5, prescriptive basics). Pure functions.

  Safety stock   SS  = z * sqrt(L * sigma_d^2 + d^2 * sigma_L^2)
  Reorder point  ROP = d * L + SS
  Net production Q   = max(0, gross_requirement + safety_stock - on_hand - in_pipeline), rounded up
                       to the lot size

  d = mean daily demand, sigma_d = std of daily demand, L = mean lead time (days),
  sigma_L = std of lead time (days), z = normal quantile of the target service level.
Every result carries its formula and the exact inputs used.
"""
import math
from statistics import NormalDist
from typing import Sequence


class PlanningError(ValueError):
    """Bad input. Message is safe to show to API clients."""


def _nonneg(name: str, v: float) -> float:
    v = float(v)
    if not math.isfinite(v) or v < 0:
        raise PlanningError(f"{name} must be a finite number >= 0")
    return v


def z_for_service_level(service_level: float) -> float:
    if not (0.5 <= service_level < 1):
        raise PlanningError("service_level must be >= 0.5 and < 1 (e.g. 0.95)")
    return NormalDist().inv_cdf(service_level)


def mean_std(values: Sequence[float], name: str, min_n: int = 2) -> tuple[float, float]:
    """Mean and sample standard deviation (n-1) of a series."""
    xs = [float(v) for v in values]
    if len(xs) < min_n:
        raise PlanningError(f"{name}: need at least {min_n} observations")
    if any(not math.isfinite(x) or x < 0 for x in xs):
        raise PlanningError(f"{name} must contain finite numbers >= 0")
    m = sum(xs) / len(xs)
    var = sum((x - m) ** 2 for x in xs) / (len(xs) - 1)
    return m, math.sqrt(var)


def safety_stock(daily_demand: float, demand_std: float, lead_time_days: float,
                 lead_time_std: float, service_level: float = 0.95) -> dict:
    d, sd = _nonneg("daily_demand", daily_demand), _nonneg("demand_std", demand_std)
    lt, slt = _nonneg("lead_time_days", lead_time_days), _nonneg("lead_time_std", lead_time_std)
    z = z_for_service_level(service_level)
    ss = z * math.sqrt(lt * sd ** 2 + d ** 2 * slt ** 2)
    return {"safety_stock": math.ceil(ss - 1e-9), "unrounded": round(ss, 3), "z": round(z, 4),
            "formula": "z * sqrt(L * sigma_d^2 + d^2 * sigma_L^2)",
            "inputs": {"d": d, "sigma_d": sd, "L": lt, "sigma_L": slt, "service_level": service_level}}


def reorder_point(daily_demand: float, demand_std: float, lead_time_days: float,
                  lead_time_std: float, service_level: float = 0.95) -> dict:
    ss = safety_stock(daily_demand, demand_std, lead_time_days, lead_time_std, service_level)
    lead_demand = float(daily_demand) * float(lead_time_days)
    rop = lead_demand + ss["unrounded"]
    return {"reorder_point": math.ceil(rop - 1e-9), "unrounded": round(rop, 3),
            "demand_during_lead_time": round(lead_demand, 3), "safety_stock": ss["safety_stock"],
            "formula": "d * L + SS", "inputs": ss["inputs"], "safety_stock_detail": ss}


def reorder_status(on_hand: float, reserved: float, on_order: float, rop: float) -> dict:
    """Inventory position = on_hand - reserved + on_order. Order when position <= ROP."""
    pos = _nonneg("on_hand", on_hand) - _nonneg("reserved", reserved) + _nonneg("on_order", on_order)
    return {"inventory_position": round(pos, 3), "reorder_point": rop, "should_reorder": pos <= rop,
            "shortfall": round(max(rop - pos, 0), 3),
            "formula": "position = on_hand - reserved + on_order; reorder when position <= ROP"}


def net_production(gross_requirement: float, on_hand: float, in_pipeline: float = 0,
                   safety_stock_target: float = 0, reserved: float = 0, lot_size: float = 1,
                   min_batch: float = 0) -> dict:
    """gross_requirement = forecast demand over the planning horizon."""
    gross = _nonneg("gross_requirement", gross_requirement)
    lot = _nonneg("lot_size", lot_size)
    if lot <= 0:
        raise PlanningError("lot_size must be > 0")
    net = (gross + _nonneg("reserved", reserved) + _nonneg("safety_stock_target", safety_stock_target)
           - _nonneg("on_hand", on_hand) - _nonneg("in_pipeline", in_pipeline))
    if net <= 0:
        qty = 0
    else:
        qty = math.ceil(net / lot - 1e-9) * lot
        qty = max(qty, _nonneg("min_batch", min_batch)) if min_batch else qty
        qty = math.ceil(qty / lot - 1e-9) * lot
    return {"quantity_to_produce": qty, "net_requirement": round(max(net, 0), 3),
            "formula": "max(0, gross + reserved + safety_stock - on_hand - in_pipeline), "
                       "rounded up to lot_size (and min_batch)",
            "inputs": {"gross_requirement": gross, "reserved": reserved,
                       "safety_stock_target": safety_stock_target, "on_hand": on_hand,
                       "in_pipeline": in_pipeline, "lot_size": lot, "min_batch": min_batch}}
