import numpy as np
import pandas as pd
from typing import Dict


def generate_synthetic_company(seed: int = 42) -> Dict[str, pd.DataFrame]:
    """
    Generates a 12-month (365-day) synthetic ERP dataset across 5 departments
    with 7 injected real-world problems for end-to-end NEXUS validation.
    """
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2025-01-01", periods=365, freq="D")

    skus = {
        "SKU-A": {"price": 120.0, "cost": 65.0, "hrs": 0.5, "mat": 2.0, "base_demand": 40},
        "SKU-B": {"price": 85.0,  "cost": 50.0, "hrs": 0.4, "mat": 1.5, "base_demand": 30},
        "SKU-C": {"price": 60.0,  "cost": 35.0, "hrs": 0.3, "mat": 1.0, "base_demand": 25},
        "SKU-D": {"price": 45.0,  "cost": 30.0, "hrs": 0.2, "mat": 0.8, "base_demand": 5},  # Slow mover
    }

    sales_rows, prod_rows, inv_rows, proc_rows, fin_rows = [], [], [], [], []
    raw_material_stock = 2000.0
    sku_d_stock = 800.0  # Problem 7: Excess Inventory (>120 days of stock)

    for d in dates:
        month = d.month

        # Problem 3: Supplier Delay (Months 5-6) — lead time jumps from 5d to 18d
        lead_time = int(rng.normal(18, 2)) if month in (5, 6) else int(rng.normal(5, 1))
        lead_time = max(1, lead_time)

        # Problem 4: Raw Material Shortage (Month 7) — delayed replenishment hits stock
        replenishment = 60.0 if month == 7 else 240.0
        raw_material_stock = max(0.0, raw_material_stock + replenishment)

        # Problem 5: Capacity Limit (Month 8) — Line 2 hits 100% cap (max 38 hrs/day)
        daily_capacity_hrs = 38.0 if month == 8 else 65.0

        # Problem 6: Finance Budget Cap (Month 9) — working capital ceiling
        daily_budget_cap = 3500.0 if month == 9 else 12000.0

        used_hrs = 0.0
        daily_cogs = 0.0
        daily_rev = 0.0

        for sku, cfg in skus.items():
            demand = max(1, int(rng.poisson(cfg["base_demand"])))

            # Problem 1: Sales Drop (Month 4) — 30% drop on SKU-B due to stockout
            if month == 4 and sku == "SKU-B":
                fulfilled = int(demand * 0.70)
            # Problem 2: Demand Surge (Month 7) — 45% spike on SKU-A
            elif month == 7 and sku == "SKU-A":
                demand = int(demand * 1.45)
                max_by_mat = int(raw_material_stock // cfg["mat"])
                fulfilled = min(demand, max_by_mat)
            else:
                max_by_cap = int((daily_capacity_hrs - used_hrs) // cfg["hrs"])
                max_by_mat = int(raw_material_stock // cfg["mat"])
                max_by_bud = int((daily_budget_cap - daily_cogs) // cfg["cost"])
                fulfilled = max(0, min(demand, max_by_cap, max_by_mat, max_by_bud))

            used_hrs += fulfilled * cfg["hrs"]
            raw_material_stock = max(0.0, raw_material_stock - fulfilled * cfg["mat"])
            rev = fulfilled * cfg["price"]
            cost = fulfilled * cfg["cost"]
            daily_rev += rev
            daily_cogs += cost

            if sku == "SKU-D":
                sku_d_stock = max(0.0, sku_d_stock + 6.0 - fulfilled)  # Over-ordered slow mover

            sales_rows.append({
                "date": d, "month": month, "sku": sku,
                "demand_units": demand, "sold_units": fulfilled,
                "lost_sales_units": max(0, demand - fulfilled),
                "revenue": round(rev, 2),
            })
            prod_rows.append({
                "date": d, "month": month, "sku": sku,
                "produced_units": fulfilled,
                "machine_hours_used": round(fulfilled * cfg["hrs"], 2),
                "daily_capacity_hrs": daily_capacity_hrs,
            })

        inv_rows.append({
            "date": d, "month": month,
            "raw_material_on_hand": round(raw_material_stock, 2),
            "sku_d_excess_units": round(sku_d_stock, 2),
        })
        proc_rows.append({
            "date": d, "month": month,
            "supplier_id": "SUP-01",
            "lead_time_days": lead_time,
            "replenishment_units": replenishment,
        })
        fin_rows.append({
            "date": d, "month": month,
            "revenue": round(daily_rev, 2),
            "cogs": round(daily_cogs, 2),
            "gross_margin": round(daily_rev - daily_cogs, 2),
            "daily_budget_cap": daily_budget_cap,
        })

    return {
        "sales": pd.DataFrame(sales_rows),
        "production": pd.DataFrame(prod_rows),
        "inventory": pd.DataFrame(inv_rows),
        "procurement": pd.DataFrame(proc_rows),
        "finance": pd.DataFrame(fin_rows),
    }