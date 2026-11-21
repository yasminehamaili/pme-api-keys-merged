from typing import Any, Dict, List, Literal, Optional, TypedDict
from pydantic import BaseModel, Field


class TenantContext(BaseModel):
    company_id: str
    user_id: str
    role: Literal["admin", "finance", "operations", "sales", "procurement", "viewer"]
    allowed_departments: List[str]


class VerifiedAnalyticsStore(BaseModel):
    """Populated EXCLUSIVELY by deterministic Python engines—never by the LLM."""
    data_quality_report: Dict[str, Any] = Field(default_factory=dict)
    kpis: Dict[str, Any] = Field(default_factory=dict)
    anomalies: List[Dict[str, Any]] = Field(default_factory=list)
    root_causes: List[Dict[str, Any]] = Field(default_factory=list)
    forecasts: Dict[str, Any] = Field(default_factory=dict)
    optimization: Dict[str, Any] = Field(default_factory=dict)
    scenarios: Dict[str, Any] = Field(default_factory=dict)


class PendingApprovalAction(BaseModel):
    """HITL safeguard: ERP write actions must be approved by a human."""
    action_id: str
    department: str
    action_type: str  # e.g., "create_purchase_order", "adjust_production_schedule"
    description: str
    estimated_financial_impact: float
    payload: Dict[str, Any]
    status: Literal["pending", "approved", "rejected"] = "pending"


class NexusState(TypedDict):
    tenant: TenantContext
    user_query: str
    intent: Literal["full_pipeline", "employee_qa"]
    verified_data: VerifiedAnalyticsStore
    pending_approvals: List[PendingApprovalAction]
    chat_history: List[Dict[str, str]]
    final_output: Optional[Dict[str, Any]]