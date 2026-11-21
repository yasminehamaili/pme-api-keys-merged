"""
KPI engine: exact formulas in plain Python (never computed by an LLM).

Pure functions: no I/O, no database. Every KPI returns a `KPI` carrying its formula, the exact
inputs used, and (optionally) the source record ids, so each displayed number can be proven.
A zero denominator gives value=None with a reason, never a fake 0 or an exception.
"""
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable

D = Decimal


@dataclass(frozen=True)
class Threshold:
    """alert/critical limits. higher_is_better=True: value <= alert -> alert, <= critical -> critical."""
    alert: Decimal
    critical: Decimal
    higher_is_better: bool = True


@dataclass
class KPI:
    name: str
    department: str
    value: Decimal | None
    unit: str
    formula: str
    inputs: dict[str, str]
    reason: str | None = None                    # why value is None
    evidence_ids: list[str] = field(default_factory=list)
    status: str = "unknown"                      # normal | alert | critical | unknown

    def to_dict(self) -> dict:
        return {"name": self.name, "department": self.department,
                "value": None if self.value is None else str(self.value), "unit": self.unit,
                "formula": self.formula, "inputs": self.inputs, "status": self.status,
                "reason": self.reason, "evidence_ids": self.evidence_ids}


def _q(v: Decimal, places: int = 4) -> Decimal:
    return v.quantize(Decimal(1).scaleb(-places))


def _make(name, dept, unit, formula, inputs, num, den, *, scale=D(1), threshold=None,
          evidence=None, places=4) -> KPI:
    shown = {k: str(v) for k, v in inputs.items()}
    if den is None or den == 0 or num is None:
        return KPI(name, dept, None, unit, formula, shown, reason="denominator is zero or missing",
                   evidence_ids=evidence or [])
    value = _q(D(num) / D(den) * scale, places)
    return KPI(name, dept, value, unit, formula, shown, evidence_ids=evidence or [],
               status=classify(value, threshold))


def classify(value: Decimal | None, t: Threshold | None) -> str:
    if value is None or t is None:
        return "unknown"
    if t.higher_is_better:
        return "critical" if value <= t.critical else "alert" if value <= t.alert else "normal"
    return "critical" if value >= t.critical else "alert" if value >= t.alert else "normal"


# ================================ SALES ================================
def revenue(total: Decimal, count: int, evidence=None) -> KPI:
    return KPI("revenue", "sales", _q(D(total), 2), "currency", "sum(invoice.total)",
               {"invoices": str(count)}, evidence_ids=evidence or [], status="normal")


def growth(current: Decimal, previous: Decimal, threshold: Threshold | None = None) -> KPI:
    return _make("growth", "sales", "%", "(current - previous) / previous * 100",
                 {"current": current, "previous": previous}, D(current) - D(previous), previous,
                 scale=D(100), threshold=threshold, places=2)


def average_basket(total: Decimal, orders: int) -> KPI:
    return _make("average_basket", "sales", "currency", "revenue / orders",
                 {"revenue": total, "orders": orders}, total, D(orders), places=2)


def gross_margin(revenue_: Decimal, cogs: Decimal, threshold: Threshold | None = None) -> KPI:
    return _make("gross_margin", "sales", "%", "(revenue - cogs) / revenue * 100",
                 {"revenue": revenue_, "cogs": cogs}, D(revenue_) - D(cogs), revenue_,
                 scale=D(100), threshold=threshold, places=2)


def return_rate(returned_orders: int, orders: int, threshold: Threshold | None = None) -> KPI:
    return _make("return_rate", "sales", "%", "returned_orders / orders * 100",
                 {"returned_orders": returned_orders, "orders": orders}, D(returned_orders),
                 D(orders), scale=D(100), threshold=threshold, places=2)


def retention(previous_customers: set, current_customers: set,
              threshold: Threshold | None = None) -> KPI:
    kept = len(previous_customers & current_customers)
    return _make("retention", "sales", "%", "|prev ∩ current| / |prev| * 100",
                 {"retained": kept, "previous_customers": len(previous_customers)}, D(kept),
                 D(len(previous_customers)), scale=D(100), threshold=threshold, places=2)


# ============================= PRODUCTION ==============================
def completion_rate(produced: Decimal, planned: Decimal, threshold: Threshold | None = None) -> KPI:
    return _make("completion_rate", "production", "%", "produced / planned * 100",
                 {"produced": produced, "planned": planned}, produced, planned,
                 scale=D(100), threshold=threshold, places=2)


def efficiency(standard_hours: Decimal, actual_hours: Decimal) -> KPI:
    return _make("efficiency", "production", "%", "standard_hours / actual_hours * 100",
                 {"standard_hours": standard_hours, "actual_hours": actual_hours},
                 standard_hours, actual_hours, scale=D(100), places=2)


def defect_rate(defective: Decimal, produced: Decimal, threshold: Threshold | None = None) -> KPI:
    return _make("defect_rate", "production", "%", "defective / produced * 100",
                 {"defective": defective, "produced": produced}, defective, produced,
                 scale=D(100), threshold=threshold, places=2)


def capacity_utilization(used_hours: Decimal, available_hours: Decimal) -> KPI:
    return _make("capacity_utilization", "production", "%", "used_hours / available_hours * 100",
                 {"used_hours": used_hours, "available_hours": available_hours},
                 used_hours, available_hours, scale=D(100), places=2)


def oee(run_time: Decimal, planned_time: Decimal, ideal_cycle_time: Decimal,
        total_count: Decimal, good_count: Decimal, threshold: Threshold | None = None) -> KPI:
    """OEE = Availability x Performance x Quality (each a 0..1 ratio)."""
    inputs = {"run_time": run_time, "planned_time": planned_time,
              "ideal_cycle_time": ideal_cycle_time, "total_count": total_count,
              "good_count": good_count}
    if 0 in (planned_time, run_time, total_count):
        return KPI("oee", "production", None, "%", "availability x performance x quality",
                   {k: str(v) for k, v in inputs.items()}, reason="denominator is zero or missing")
    a = D(run_time) / D(planned_time)
    p = D(ideal_cycle_time) * D(total_count) / D(run_time)
    q = D(good_count) / D(total_count)
    value = _q(a * p * q * 100, 2)
    k = KPI("oee", "production", value, "%", "availability x performance x quality",
            {**{k: str(v) for k, v in inputs.items()}, "availability": str(_q(a)),
             "performance": str(_q(p)), "quality": str(_q(q))}, status=classify(value, threshold))
    return k


# ================================ STOCK ================================
def available_stock(physical: Decimal, reserved: Decimal) -> Decimal:
    return D(physical) - D(reserved)


def stock_turnover(cogs: Decimal, average_inventory_value: Decimal) -> KPI:
    return _make("stock_turnover", "stock", "x", "cogs / average_inventory_value",
                 {"cogs": cogs, "average_inventory_value": average_inventory_value},
                 cogs, average_inventory_value, places=2)


def coverage_days(available: Decimal, daily_demand: Decimal) -> KPI:
    return _make("coverage_days", "stock", "days", "available / daily_demand",
                 {"available": available, "daily_demand": daily_demand},
                 available, daily_demand, places=1)


def stockout_rate(stockout_skus: int, total_skus: int, threshold: Threshold | None = None,
                  evidence=None) -> KPI:
    return _make("stockout_rate", "stock", "%", "skus_with_available<=0 / total_skus * 100",
                 {"stockout_skus": stockout_skus, "total_skus": total_skus}, D(stockout_skus),
                 D(total_skus), scale=D(100), threshold=threshold, evidence=evidence, places=2)


def overstock_rate(overstock_skus: int, total_skus: int, evidence=None) -> KPI:
    return _make("overstock_rate", "stock", "%", "overstock_skus / total_skus * 100",
                 {"overstock_skus": overstock_skus, "total_skus": total_skus}, D(overstock_skus),
                 D(total_skus), scale=D(100), evidence=evidence, places=2)


# ============================ PROCUREMENT ==============================
def on_time_delivery(on_time: int, delivered: int, threshold: Threshold | None = None) -> KPI:
    return _make("otd", "procurement", "%", "on_time_deliveries / deliveries * 100",
                 {"on_time": on_time, "deliveries": delivered}, D(on_time), D(delivered),
                 scale=D(100), threshold=threshold, places=2)


def average_lead_time(lead_times_days: Iterable[Decimal]) -> KPI:
    xs = [D(x) for x in lead_times_days]
    return _make("lead_time", "procurement", "days", "sum(lead_times) / count",
                 {"orders": len(xs)}, sum(xs, D(0)), D(len(xs)), places=1)


def price_variance(actual_price: Decimal, standard_price: Decimal) -> KPI:
    return _make("price_variance", "procurement", "%", "(actual - standard) / standard * 100",
                 {"actual": actual_price, "standard": standard_price},
                 D(actual_price) - D(standard_price), standard_price, scale=D(100), places=2)


def supplier_score(scores: dict[str, Decimal], weights: dict[str, Decimal]) -> KPI:
    """Weighted mean of 0..100 sub-scores, e.g. {'otd': 92, 'quality': 97, 'price': 80}."""
    total_w = sum((D(weights[k]) for k in scores), D(0))
    num = sum((D(scores[k]) * D(weights[k]) for k in scores), D(0))
    return _make("supplier_score", "procurement", "points", "sum(score_i * weight_i) / sum(weight_i)",
                 {**{f"score_{k}": v for k, v in scores.items()},
                  **{f"weight_{k}": weights[k] for k in scores}}, num, total_w, places=1)


# =============================== FINANCE ===============================
def operating_margin(operating_income: Decimal, revenue_: Decimal,
                     threshold: Threshold | None = None) -> KPI:
    return _make("operating_margin", "finance", "%", "operating_income / revenue * 100",
                 {"operating_income": operating_income, "revenue": revenue_}, operating_income,
                 revenue_, scale=D(100), threshold=threshold, places=2)


def budget_variance(actual: Decimal, budget: Decimal, threshold: Threshold | None = None) -> KPI:
    return _make("budget_variance", "finance", "%", "(actual - budget) / budget * 100",
                 {"actual": actual, "budget": budget}, D(actual) - D(budget), budget,
                 scale=D(100), threshold=threshold, places=2)


def cash_flow(inflows: Decimal, outflows: Decimal) -> KPI:
    return KPI("cash_flow", "finance", _q(D(inflows) - D(outflows), 2), "currency",
               "inflows - outflows", {"inflows": str(inflows), "outflows": str(outflows)},
               status="normal")


def working_capital(current_assets: Decimal, current_liabilities: Decimal) -> KPI:
    return KPI("working_capital", "finance", _q(D(current_assets) - D(current_liabilities), 2),
               "currency", "current_assets - current_liabilities",
               {"current_assets": str(current_assets), "current_liabilities": str(current_liabilities)},
               status="normal")
