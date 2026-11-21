from tests.synthetic_company import generate_synthetic_company
from rca_engine import run_root_cause_analysis
from forecast_engine import backtest_demand_forecast
from optimization import solve_cross_department_plan, ProductInput, CompanyConstraints
from nexus_state import VerifiedAnalyticsStore
from dashboard_report import build_verified_dashboard_and_report


def test_end_to_end_synthetic_company_consistency():
    # 1. Generate 12-month synthetic dataset with 7 injected problems
    data = generate_synthetic_company(seed=42)
    assert len(data["finance"]) == 365

    # 2. Deterministic Root-Cause Analysis
    root_causes = run_root_cause_analysis(data)
    rc_ids = {r["id"] for r in root_causes}
    assert "RCA-001" in rc_ids  # Supplier delay detected
    assert "RCA-003" in rc_ids  # Month 8 capacity limit detected
    assert "RCA-004" in rc_ids  # SKU-D excess inventory detected

    # 3. Chronological Backtested Forecasts
    forecasts = backtest_demand_forecast(data["sales"], horizon_days=30)
    assert "SKU-A" in forecasts
    assert forecasts["SKU-A"]["backtest_metrics"]["WAPE_pct"] >= 0

    # 4. Cross-Department OR-Tools Optimization (Month 9 Budget Cap scenario)
    products = [
        ProductInput(
            sku="SKU-A", demand_forecast=forecasts["SKU-A"]["next_30d_daily_forecast"] * 30,
            margin_per_unit=55.0, unit_cost=65.0, machine_hours_per_unit=0.5, raw_material_per_unit=2.0,
        ),
        ProductInput(
            sku="SKU-B", demand_forecast=forecasts["SKU-B"]["next_30d_daily_forecast"] * 30,
            margin_per_unit=35.0, unit_cost=50.0, machine_hours_per_unit=0.4, raw_material_per_unit=1.5,
        ),
    ]
    constraints = CompanyConstraints(
        max_machine_hours=1100.0,
        available_raw_material=4000.0,
        max_working_capital_budget=105000.0,
    )
    opt_result = solve_cross_department_plan(products, constraints)
    assert opt_result["status"] == "OPTIMAL"
    assert opt_result["total_contribution_margin"] > 0

    # 5. Populate single-source-of-truth VerifiedAnalyticsStore
    store = VerifiedAnalyticsStore(
        kpis={"finance": {"annual_revenue": round(float(data["finance"]["revenue"].sum()), 2)}},
        root_causes=root_causes,
        forecasts=forecasts,
        optimization=opt_result,
    )

    # 6. Verify Dashboard & 20-Section Report draw identical numbers
    report = build_verified_dashboard_and_report("tenant_synthetic_01", store)
    assert len(report.sections) == 20
    assert (
        report.sections["01_executive_summary"]["optimal_margin"]
        == opt_result["total_contribution_margin"]
    )
    assert report.dashboard_charts[0].series[0]["data"] == list(opt_result["production_plan"].values())