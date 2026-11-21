from typing import Any, Dict, List
from ortools.linear_solver import pywraplp
from pydantic import BaseModel


class ProductInput(BaseModel):
    sku: str
    demand_forecast: float
    margin_per_unit: float
    unit_cost: float
    machine_hours_per_unit: float
    raw_material_per_unit: float
    min_contractual_units: float = 0.0


class CompanyConstraints(BaseModel):
    max_machine_hours: float
    available_raw_material: float
    max_working_capital_budget: float


def solve_cross_department_plan(
    products: List[ProductInput],
    constraints: CompanyConstraints,
) -> Dict[str, Any]:
    solver = pywraplp.Solver.CreateSolver("SCIP")
    if not solver:
        raise RuntimeError("OR-Tools SCIP solver is not available.")

    x = {
        p.sku: solver.NumVar(
            p.min_contractual_units,
            max(p.min_contractual_units, p.demand_forecast),
            f"units_{p.sku}",
        )
        for p in products
    }

    solver.Add(sum(p.machine_hours_per_unit * x[p.sku] for p in products) <= constraints.max_machine_hours)
    solver.Add(sum(p.raw_material_per_unit * x[p.sku] for p in products) <= constraints.available_raw_material)
    solver.Add(sum(p.unit_cost * x[p.sku] for p in products) <= constraints.max_working_capital_budget)

    solver.Maximize(sum(p.margin_per_unit * x[p.sku] for p in products))
    status = solver.Solve()

    if status == pywraplp.Solver.OPTIMAL:
        plan = {p.sku: round(x[p.sku].solution_value(), 2) for p in products}
        used_hours = sum(p.machine_hours_per_unit * plan[p.sku] for p in products)
        used_material = sum(p.raw_material_per_unit * plan[p.sku] for p in products)
        used_budget = sum(p.unit_cost * plan[p.sku] for p in products)

        return {
            "status": "OPTIMAL",
            "total_contribution_margin": round(solver.Objective().Value(), 2),
            "production_plan": plan,
            "unmet_demand": {
                p.sku: round(max(0.0, p.demand_forecast - plan[p.sku]), 2)
                for p in products
            },
            "resource_utilization_pct": {
                "production_capacity": round((used_hours / constraints.max_machine_hours) * 100, 1),
                "raw_material": round((used_material / constraints.available_raw_material) * 100, 1),
                "finance_budget": round((used_budget / constraints.max_working_capital_budget) * 100, 1),
            },
        }

    return {"status": "INFEASIBLE", "violations": ["Constraints cannot be simultaneously satisfied."]}
