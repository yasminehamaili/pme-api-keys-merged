"""
KPI endpoints computed live from the ERP (stateless: nothing is stored).

  GET /v1/kpi/sales?date_from=2026-01-01&date_to=2026-03-31
  GET /v1/kpi/stock?overstock_factor=5

Rows are aggregated while streaming from the ERP, so memory stays flat. Every KPI carries its
formula, inputs and a sample of source record ids (data lineage).
"""
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

import kpi
from erp_client import ERPError, get_erp_client
from erp_config import ERPNotConfigured, get_settings
from erp_export import ExportTooLarge, open_rows
from erp_routes import export_guard, http_error
from erp_schemas import RESOURCES

log = logging.getLogger("kpi")

router = APIRouter(prefix="/v1/kpi", tags=["kpi"])

EXCLUDED_STATUSES = {"draft", "cancel", "cancelled", "canceled", "void"}
EVIDENCE_SAMPLE = 25
MAX_RANGE_DAYS = 3660

# Edit to your business. higher_is_better=False for "bad when high" KPIs.
THRESHOLDS = {
    "growth": kpi.Threshold(Decimal("0"), Decimal("-10")),
    "stockout_rate": kpi.Threshold(Decimal("5"), Decimal("15"), higher_is_better=False),
    "otd": kpi.Threshold(Decimal("90"), Decimal("80")),
    "completion_rate": kpi.Threshold(Decimal("95"), Decimal("85")),
    "defect_rate": kpi.Threshold(Decimal("2"), Decimal("5"), higher_is_better=False),
    "return_rate": kpi.Threshold(Decimal("5"), Decimal("10"), higher_is_better=False),
    "gross_margin": kpi.Threshold(Decimal("20"), Decimal("10")),
}


async def _sales_period(client, d_from: date, d_to: date, cap: int):
    """Stream invoices for a period -> (revenue, count, customer_ids, evidence_ids, skipped)."""
    rows, stats = await open_rows(client, RESOURCES["invoices"],
                                  {"date_from": d_from, "date_to": d_to}, None, cap)
    total, count, customers, evidence = Decimal(0), 0, set(), []
    async for inv in rows:
        # defensive: re-check the range ourselves in case the ERP ignored the filter
        if not (d_from <= inv.issue_date <= d_to) or (inv.status or "").lower() in EXCLUDED_STATUSES:
            continue
        total += inv.total
        count += 1
        if inv.customer_id:
            customers.add(inv.customer_id)
        if len(evidence) < EVIDENCE_SAMPLE:
            evidence.append(inv.id)
    return total, count, customers, evidence, stats.skipped


@router.get("/sales")
async def sales_kpis(
    date_from: date,
    date_to: date,
    auth: dict = Depends(export_guard),
):
    if date_from > date_to:
        raise HTTPException(422, "date_from must be on or before date_to")
    days = (date_to - date_from).days + 1
    if days > MAX_RANGE_DAYS:
        raise HTTPException(422, f"Range too large (max {MAX_RANGE_DAYS} days)")
    prev_to = date_from - timedelta(days=1)
    prev_from = prev_to - timedelta(days=days - 1)       # same length, immediately before

    ctx = {"customer_id": auth["customer_id"], "resource": "kpi.sales"}
    try:
        cap = get_settings().max_export_rows
        client = get_erp_client()
        cur = await _sales_period(client, date_from, date_to, cap)
        prev = await _sales_period(client, prev_from, prev_to, cap)
    except (ERPError, ERPNotConfigured, ExportTooLarge) as exc:
        raise http_error(exc, ctx)

    (rev, n, cust, ev, skipped), (p_rev, p_n, p_cust, _, p_skipped) = cur, prev
    kpis = [
        kpi.revenue(rev, n, ev),
        kpi.growth(rev, p_rev, THRESHOLDS["growth"]),
        kpi.average_basket(rev, n),
        kpi.retention(p_cust, cust),
    ]
    return {
        "period": {"from": date_from, "to": date_to},
        "previous_period": {"from": prev_from, "to": prev_to},
        "kpis": [k.to_dict() for k in kpis],
        "data_quality": {"skipped_invalid_records": skipped + p_skipped},
        "not_available": {
            "gross_margin": "needs cost of goods sold (COGS) data",
            "return_rate": "needs returns data",
        },
    }


@router.get("/stock")
async def stock_kpis(
    overstock_factor: Annotated[Decimal, Query(gt=1, le=100)] = Decimal(5),
    auth: dict = Depends(export_guard),
):
    """available = on_hand - reserved. Overstock: available > overstock_factor x reorder_level."""
    ctx = {"customer_id": auth["customer_id"], "resource": "kpi.stock"}
    try:
        cfg = get_settings()
        rows, stats = await open_rows(get_erp_client(), RESOURCES["inventory"], {}, None,
                                      cfg.max_export_rows)
        total = 0
        out, over, low, value = [], [], [], Decimal(0)
        async for item in rows:
            total += 1
            available = kpi.available_stock(item.quantity_on_hand, item.quantity_reserved or 0)
            if item.unit_cost is not None and item.quantity_on_hand > 0:
                value += item.quantity_on_hand * item.unit_cost
            if available <= 0:
                out.append(item.sku)
            elif item.reorder_level and available <= item.reorder_level:
                low.append(item.sku)
            if item.reorder_level and available > overstock_factor * item.reorder_level:
                over.append(item.sku)
    except (ERPError, ERPNotConfigured, ExportTooLarge) as exc:
        raise http_error(exc, ctx)

    kpis = [
        kpi.stockout_rate(len(out), total, THRESHOLDS["stockout_rate"], out[:EVIDENCE_SAMPLE]),
        kpi.overstock_rate(len(over), total, over[:EVIDENCE_SAMPLE]),
    ]
    return {
        "kpis": [k.to_dict() for k in kpis],
        "inventory_value": str(value.quantize(Decimal("0.01"))),
        "below_reorder_level": {"count": len(low), "sku_sample": low[:EVIDENCE_SAMPLE]},
        "data_quality": {"skipped_invalid_records": stats.skipped},
        "not_available": {
            "stock_turnover": "needs COGS data (kpi.stock_turnover is ready)",
            "coverage_days": "needs daily demand (kpi.coverage_days is ready)",
        },
    }


# ============================ order-based KPIs ============================
def _check_range(date_from: date, date_to: date) -> None:
    if date_from > date_to:
        raise HTTPException(422, "date_from must be on or before date_to")
    if (date_to - date_from).days + 1 > MAX_RANGE_DAYS:
        raise HTTPException(422, f"Range too large (max {MAX_RANGE_DAYS} days)")


def _live(status: str | None) -> bool:
    return (status or "").lower() not in EXCLUDED_STATUSES


@router.get("/sales-orders")
async def sales_order_kpis(date_from: date, date_to: date, auth: dict = Depends(export_guard)):
    """Gross margin and return rate. Margin only uses orders that carry a cost (coverage reported)."""
    _check_range(date_from, date_to)
    ctx = {"customer_id": auth["customer_id"], "resource": "kpi.sales_orders"}
    try:
        rows, stats = await open_rows(get_erp_client(), RESOURCES["sales_orders"],
                                      {"date_from": date_from, "date_to": date_to}, None,
                                      get_settings().max_export_rows)
        n = returned = with_cost = 0
        rev_costed = cogs = Decimal(0)
        returned_ids: list[str] = []
        async for o in rows:
            if not (date_from <= o.order_date <= date_to) or not _live(o.status):
                continue
            n += 1
            if o.returned:
                returned += 1
                if len(returned_ids) < EVIDENCE_SAMPLE:
                    returned_ids.append(o.id)
            if o.cogs is not None:
                with_cost += 1
                rev_costed += o.total
                cogs += o.cogs
    except (ERPError, ERPNotConfigured, ExportTooLarge) as exc:
        raise http_error(exc, ctx)
    rr = kpi.return_rate(returned, n, THRESHOLDS["return_rate"])
    rr.evidence_ids = returned_ids
    return {
        "period": {"from": date_from, "to": date_to},
        "kpis": [kpi.gross_margin(rev_costed, cogs, THRESHOLDS["gross_margin"]).to_dict(), rr.to_dict()],
        "data_quality": {"orders": n, "orders_with_cost": with_cost,
                         "margin_coverage": f"{with_cost}/{n}",
                         "skipped_invalid_records": stats.skipped},
    }


@router.get("/procurement")
async def procurement_kpis(date_from: date, date_to: date, auth: dict = Depends(export_guard)):
    """OTD, lead time, price variance for purchase orders placed in the period."""
    _check_range(date_from, date_to)
    ctx = {"customer_id": auth["customer_id"], "resource": "kpi.procurement"}
    today = datetime.now(timezone.utc).date()
    try:
        rows, stats = await open_rows(get_erp_client(), RESOURCES["purchase_orders"],
                                      {"date_from": date_from, "date_to": date_to}, None,
                                      get_settings().max_export_rows)
        on_time = judged = no_promise = priced = 0
        lead: list[Decimal] = []
        late_ids: list[str] = []
        overdue_open: list[str] = []
        actual_val = std_val = Decimal(0)
        async for po in rows:
            if not (date_from <= po.order_date <= date_to) or not _live(po.status):
                continue
            if po.received_date:
                lead.append(Decimal((po.received_date - po.order_date).days))
                if po.promised_date:
                    judged += 1
                    if po.received_date <= po.promised_date:
                        on_time += 1
                    elif len(late_ids) < EVIDENCE_SAMPLE:
                        late_ids.append(po.id)
                else:
                    no_promise += 1
            elif po.promised_date and po.promised_date < today and len(overdue_open) < EVIDENCE_SAMPLE:
                overdue_open.append(po.id)
            qty = po.quantity_received if po.quantity_received is not None else po.quantity_ordered
            if po.unit_price is not None and po.standard_price is not None and qty:
                priced += 1
                actual_val += po.unit_price * qty
                std_val += po.standard_price * qty
    except (ERPError, ERPNotConfigured, ExportTooLarge) as exc:
        raise http_error(exc, ctx)
    otd = kpi.on_time_delivery(on_time, judged, THRESHOLDS["otd"])
    otd.evidence_ids = late_ids                              # the late orders are the proof
    return {
        "period": {"from": date_from, "to": date_to},
        "kpis": [otd.to_dict(), kpi.average_lead_time(lead).to_dict(),
                 kpi.price_variance(actual_val, std_val).to_dict()],
        "overdue_open_orders": {"as_of": today, "sample_ids": overdue_open},
        "data_quality": {"delivered_without_promised_date": no_promise, "orders_with_prices": priced,
                         "skipped_invalid_records": stats.skipped},
    }


@router.get("/production")
async def production_kpis(date_from: date, date_to: date, auth: dict = Depends(export_guard)):
    """Completion rate, defect rate, efficiency for production orders started in the period."""
    _check_range(date_from, date_to)
    ctx = {"customer_id": auth["customer_id"], "resource": "kpi.production"}
    try:
        rows, stats = await open_rows(get_erp_client(), RESOURCES["production_orders"],
                                      {"date_from": date_from, "date_to": date_to}, None,
                                      get_settings().max_export_rows)
        planned = produced = defective = std_h = act_h = Decimal(0)
        n = known = 0
        short_ids: list[str] = []
        async for mo in rows:
            if not (date_from <= mo.start_date <= date_to) or not _live(mo.status):
                continue
            n += 1
            if mo.produced_quantity is None:
                continue                                     # unknown outcome: never counted as 0
            known += 1
            planned += mo.planned_quantity
            produced += mo.produced_quantity
            if mo.defective_quantity is not None:
                defective += mo.defective_quantity
            if mo.standard_hours is not None and mo.actual_hours is not None:
                std_h += mo.standard_hours
                act_h += mo.actual_hours
            if mo.produced_quantity < mo.planned_quantity and len(short_ids) < EVIDENCE_SAMPLE:
                short_ids.append(mo.id)
    except (ERPError, ERPNotConfigured, ExportTooLarge) as exc:
        raise http_error(exc, ctx)
    comp = kpi.completion_rate(produced, planned, THRESHOLDS["completion_rate"])
    comp.evidence_ids = short_ids
    return {
        "period": {"from": date_from, "to": date_to},
        "kpis": [comp.to_dict(),
                 kpi.defect_rate(defective, produced, THRESHOLDS["defect_rate"]).to_dict(),
                 kpi.efficiency(std_h, act_h).to_dict()],
        "data_quality": {"orders": n, "orders_with_results": known,
                         "skipped_invalid_records": stats.skipped},
        "not_available": {"capacity_utilization": "needs available machine hours",
                          "oee": "needs run time, ideal cycle time and good counts (kpi.oee is ready)"},
    }
