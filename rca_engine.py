import pandas as pd
from scipy import stats
from typing import Any, Dict, List


def run_root_cause_analysis(data: Dict[str, pd.DataFrame]) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    sales = data["sales"]
    proc = data["procurement"]
    inv = data["inventory"]
    prod = data["production"]

    # 1. Supplier Lead-Time Spike Test (Months 5-6 vs Baseline)
    delay_period = proc[proc["month"].isin([5, 6])]["lead_time_days"]
    normal_period = proc[~proc["month"].isin([5, 6])]["lead_time_days"]
    t_stat, p_val = stats.ttest_ind(delay_period, normal_period, equal_var=False)
    if p_val < 0.01 and delay_period.mean() > normal_period.mean():
        findings.append({
            "id": "RCA-001",
            "department": "procurement",
            "classification": "causal_evidence",
            "test": "welch_t_test",
            "p_value": round(float(p_val), 6),
            "metric_delta": f"Lead time jumped from {normal_period.mean():.1f}d to {delay_period.mean():.1f}d",
            "summary": "Statistically significant supplier lead-time spike in Months 5-6.",
        })

    # 2. Correlation vs Causation: Raw Material Stock vs Lost Sales
    daily_lost = sales.groupby("date")["lost_sales_units"].sum().reset_index()
    merged = pd.merge(daily_lost, inv, on="date")
    r_val, r_p = stats.pearsonr(merged["raw_material_on_hand"], merged["lost_sales_units"])
    if r_p < 0.05 and r_val < -0.2:
        findings.append({
            "id": "RCA-002",
            "department": "inventory",
            "classification": "correlational",
            "test": "pearson_r",
            "r_value": round(float(r_val), 4),
            "p_value": round(float(r_p), 6),
            "summary": "Negative correlation between raw material stock and lost sales; requires operational check for stockout causality.",
        })

    # 3. Capacity Bottleneck Detection (Month 8)
    daily_prod = prod.groupby(["date", "month"]).agg(
        used=("machine_hours_used", "sum"),
        cap=("daily_capacity_hrs", "first"),
    ).reset_index()
    daily_prod["util_pct"] = (daily_prod["used"] / daily_prod["cap"]) * 100
    m8_util = daily_prod[daily_prod["month"] == 8]["util_pct"].mean()
    if m8_util > 90.0:
        findings.append({
            "id": "RCA-003",
            "department": "production",
            "classification": "causal_evidence",
            "test": "threshold_utilization",
            "utilization_pct": round(float(m8_util), 1),
            "summary": f"Production capacity hit {m8_util:.1f}% average utilization in Month 8, capping output.",
        })

    # 4. Excess Inventory Detection (SKU-D)
    avg_d_stock = inv["sku_d_excess_units"].mean()
    avg_d_daily_sales = sales[sales["sku"] == "SKU-D"]["sold_units"].mean()
    days_of_supply = avg_d_stock / max(avg_d_daily_sales, 0.1)
    if days_of_supply > 120:
        findings.append({
            "id": "RCA-004",
            "department": "inventory",
            "classification": "causal_evidence",
            "test": "days_of_supply",
            "days_of_supply": round(float(days_of_supply), 1),
            "summary": f"SKU-D holds {days_of_supply:.1f} days of stock (>120d threshold), tying up working capital.",
        })

    return findings