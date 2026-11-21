from typing import Any, Dict, List
from pydantic import BaseModel
from nexus_state import VerifiedAnalyticsStore


class EChartsSpec(BaseModel):
    title: str
    chart_type: str
    x_axis: List[str]
    series: List[Dict[str, Any]]


class NexusReportPayload(BaseModel):
    company_id: str
    dashboard_charts: List[EChartsSpec]
    sections: Dict[str, Any]


def build_verified_dashboard_and_report(
    company_id: str, store: VerifiedAnalyticsStore
) -> NexusReportPayload:
    opt = store.optimization
    plan = opt.get("production_plan", {})
    util = opt.get("resource_utilization_pct", {})

    charts = [
        EChartsSpec(
            title="Optimal Production Plan by SKU",
            chart_type="bar",
            x_axis=list(plan.keys()),
            series=[{"name": "Units", "type": "bar", "data": list(plan.values())}],
        ),
        EChartsSpec(
            title="Cross-Department Resource Utilization (%)",
            chart_type="bar",
            x_axis=list(util.keys()),
            series=[{"name": "Utilization %", "type": "bar", "data": list(util.values())}],
        ),
    ]

    # Structured 20-section report skeleton populated from VerifiedAnalyticsStore
    sections = {
        "01_executive_summary": {"kpis": store.kpis, "optimal_margin": opt.get("total_contribution_margin")},
        "02_data_quality": store.data_quality_report,
        "03_sales_kpis": store.kpis.get("sales", {}),
        "04_production_kpis": store.kpis.get("production", {}),
        "05_inventory_kpis": store.kpis.get("inventory", {}),
        "06_procurement_kpis": store.kpis.get("procurement", {}),
        "07_finance_kpis": store.kpis.get("finance", {}),
        "08_anomalies_detected": store.anomalies,
        "09_root_cause_analysis": store.root_causes,
        "10_demand_forecasts": store.forecasts,
        "11_optimization_model": opt,
        "12_scenario_baseline": store.scenarios.get("baseline", {}),
        "13_scenario_min_intervention": store.scenarios.get("min_intervention", {}),
        "14_scenario_optimized": store.scenarios.get("optimized", {}),
        "15_smart_action_plan": [],
        "16_okr_alignment": [],
        "17_financial_impact": {"projected_margin": opt.get("total_contribution_margin")},
        "18_risk_and_constraints": util,
        "19_hitl_governance": {"status": "all_erp_writes_require_human_approval"},
        "20_audit_trail": {"company_id": company_id, "deterministic_verified": True},
    }

    return NexusReportPayload(
        company_id=company_id,
        dashboard_charts=charts,
        sections=sections,
    )