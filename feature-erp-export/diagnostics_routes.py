"""
Diagnostic endpoints (JSON in, structured findings out). Pure computation: nothing is stored.

  POST /v1/diagnostics/anomaly      KPI vs target and history -> normal | alert | critical
  POST /v1/diagnostics/association  correlation, optionally lagged (temporal precedence)
  POST /v1/diagnostics/compare      2 groups (t-test / Mann-Whitney) or 3+ groups (ANOVA / Kruskal)
  POST /v1/diagnostics/chi-square   independence of two categorical variables
"""
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator

import diagnostics as dx
from ratelimit import enforce_limits

router = APIRouter(prefix="/v1/diagnostics", tags=["diagnostics"], dependencies=[Depends(enforce_limits)])

Num = Annotated[float, Field(allow_inf_nan=False)]
Series = Annotated[list[Num], Field(max_length=10_000)]


class AnomalyIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    value: Num
    target: Num | None = None
    history: Series | None = None
    higher_is_better: bool = True
    alert_pct: float = Field(5, ge=0, le=1000)
    critical_pct: float = Field(15, ge=0, le=1000)


class AssociationIn(BaseModel):
    x: Series
    y: Series
    method: Literal["pearson", "spearman"] = "pearson"
    max_lag: int | None = Field(None, ge=1, le=24,
                                description="Set to test whether x (cause) leads y (effect) in time")

    @model_validator(mode="after")
    def _same_length(self):
        if len(self.x) != len(self.y):
            raise ValueError("x and y must have the same length")
        return self


class CompareIn(BaseModel):
    groups: dict[Annotated[str, Field(max_length=60)], Series] = Field(min_length=2, max_length=50)


class ChiSquareIn(BaseModel):
    table: list[Annotated[list[Annotated[int, Field(ge=0)]], Field(min_length=2, max_length=50)]] = Field(
        min_length=2, max_length=50)


def _run(fn, *args, **kwargs) -> dict:
    try:
        out = fn(*args, **kwargs)
    except dx.DiagnosticsError as exc:
        raise HTTPException(422, str(exc))
    return out.to_dict() if isinstance(out, dx.Finding) else out


@router.post("/anomaly")
def anomaly(body: AnomalyIn):
    return _run(dx.assess_kpi, body.name, body.value, target=body.target, history=body.history,
                higher_is_better=body.higher_is_better, alert_pct=body.alert_pct,
                critical_pct=body.critical_pct)


@router.post("/association")
def association(body: AssociationIn):
    if body.max_lag:
        return _run(dx.lagged_association, body.x, body.y, body.max_lag, body.method)
    return _run(dx.association, body.x, body.y, body.method)


@router.post("/compare")
def compare(body: CompareIn):
    g = list(body.groups.values())
    if len(g) == 2:
        return _run(dx.compare_groups, g[0], g[1])
    return _run(dx.compare_many, body.groups)


@router.post("/chi-square")
def chi_square(body: ChiSquareIn):
    if len({len(r) for r in body.table}) != 1:
        raise HTTPException(422, "all table rows must have the same length")
    return _run(dx.chi_square, body.table)
