import os
import json
import uuid
import requests
from dotenv import load_dotenv
from langchain_core.messages import SystemMessage, HumanMessage
from langgraph.graph import StateGraph, END

from nexus_state import NexusState, PendingApprovalAction
from optimization import solve_cross_department_plan, ProductInput, CompanyConstraints

load_dotenv()


class GemmaResponse:
    def __init__(self, content: str):
        self.content = content


class GemmaClient:
    """Direct HTTP client for Gemma 4 31B on AIGrid (zero OpenAI dependencies)."""
    def __init__(self):
        self.api_key = os.getenv("AIGRID_API_KEY")
        self.base_url = os.getenv("AIGRID_BASE_URL", "https://app.ai-grid.io/v1").rstrip("/")
        self.model = os.getenv("NEXUS_LLM_MODEL", "google/gemma-4-31B")

    def invoke(self, messages) -> GemmaResponse:
        if isinstance(messages, str):
            formatted = [{"role": "user", "content": messages}]
        else:
            role_map = {"system": "system", "human": "user", "ai": "assistant"}
            formatted = [
                {"role": role_map.get(getattr(m, "type", "human"), "user"), "content": m.content}
                for m in messages
            ]

        resp = requests.post(
            f"{self.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "messages": formatted,
                "temperature": 0.0,
            },
            timeout=60,
        )
        resp.raise_for_status()
        return GemmaResponse(resp.json()["choices"][0]["message"]["content"])


def get_llm() -> GemmaClient:
    return GemmaClient()


def route_entry(state: NexusState) -> str:
    return "employee_assistant" if state["intent"] == "employee_qa" else "kpi_and_diagnostics"


def kpi_and_diagnostics_node(state: NexusState) -> NexusState:
    company_id = state["tenant"].company_id
    state["verified_data"].kpis = {
        "company_id": company_id,
        "gross_margin_pct": 34.2,
        "stockout_rate_pct": 8.5,
    }
    state["verified_data"].anomalies = [
        {"department": "sales", "metric": "revenue", "delta_pct": -18.4, "severity": "high"}
    ]
    state["verified_data"].root_causes = [
        {
            "anomaly": "sales_revenue_drop",
            "classification": "causal_evidence",
            "p_value": 0.003,
            "summary": "Supplier lead-time spike caused raw material stockout on Line 2.",
        }
    ]
    return state


def forecast_and_optimize_node(state: NexusState) -> NexusState:
    sample_products = [
        ProductInput(
            sku="SKU-A", demand_forecast=1200, margin_per_unit=45.0,
            unit_cost=60.0, machine_hours_per_unit=0.5, raw_material_per_unit=2.0,
        ),
        ProductInput(
            sku="SKU-B", demand_forecast=800, margin_per_unit=30.0,
            unit_cost=40.0, machine_hours_per_unit=0.4, raw_material_per_unit=1.5,
        ),
    ]
    constraints = CompanyConstraints(
        max_machine_hours=750.0,
        available_raw_material=3000.0,
        max_working_capital_budget=90000.0,
    )
    opt_result = solve_cross_department_plan(sample_products, constraints)
    state["verified_data"].optimization = opt_result

    if opt_result.get("status") == "OPTIMAL":
        state["pending_approvals"].append(
            PendingApprovalAction(
                action_id=str(uuid.uuid4()),
                department="production",
                action_type="update_production_plan",
                description="Apply OR-Tools optimal production mix to maximize contribution margin.",
                estimated_financial_impact=opt_result["total_contribution_margin"],
                payload=opt_result["production_plan"],
            )
        )
    return state


def strategy_and_report_node(state: NexusState) -> NexusState:
    llm = get_llm()
    verified_json = state["verified_data"].model_dump_json(indent=2)

    prompt = (
        "You are NEXUS INTELLIGENCE. Using ONLY the verified JSON below, produce:\n"
        "1. Executive summary\n"
        "2. 3-Scenario comparison (Baseline, Minimum Intervention, Optimized)\n"
        "3. Top 3 SMART actions.\n"
        "NEVER invent numbers not present in the JSON.\n\n"
        f"VERIFIED DATA:\n{verified_json}"
    )
    response = llm.invoke([HumanMessage(content=prompt)])
    state["final_output"] = {
        "narrative": response.content,
        "verified_data": state["verified_data"].model_dump(),
        "pending_approvals": [a.model_dump() for a in state["pending_approvals"]],
    }
    return state


def employee_assistant_node(state: NexusState) -> NexusState:
    llm = get_llm()
    allowed = set(state["tenant"].allowed_departments)

    filtered_anomalies = [
        a for a in state["verified_data"].anomalies
        if a.get("department") in allowed or "all" in allowed
    ]
    context_payload = {
        "role": state["tenant"].role,
        "allowed_departments": list(allowed),
        "kpis": state["verified_data"].kpis,
        "anomalies": filtered_anomalies,
        "optimization": state["verified_data"].optimization,
    }

    sys_msg = SystemMessage(
        content=(
            "You are the NEXUS Employee Assistant. Answer the user's question using ONLY "
            "the verified company data provided. If the user asks about a department outside "
            "their allowed_departments, politely refuse. Never fabricate numbers."
        )
    )
    user_msg = HumanMessage(
        content=f"VERIFIED CONTEXT:\n{json.dumps(context_payload, indent=2)}\n\nQUESTION: {state['user_query']}"
    )
    response = llm.invoke([sys_msg, user_msg])
    state["final_output"] = {"answer": response.content}
    return state


def build_nexus_graph():
    graph = StateGraph(NexusState)
    graph.add_node("kpi_and_diagnostics", kpi_and_diagnostics_node)
    graph.add_node("forecast_and_optimize", forecast_and_optimize_node)
    graph.add_node("strategy_and_report", strategy_and_report_node)
    graph.add_node("employee_assistant", employee_assistant_node)

    graph.set_conditional_entry_point(
        route_entry,
        {
            "kpi_and_diagnostics": "kpi_and_diagnostics",
            "employee_assistant": "employee_assistant",
        },
    )
    graph.add_edge("kpi_and_diagnostics", "forecast_and_optimize")
    graph.add_edge("forecast_and_optimize", "strategy_and_report")
    graph.add_edge("strategy_and_report", END)
    graph.add_edge("employee_assistant", END)
    return graph.compile()


nexus_app = build_nexus_graph()