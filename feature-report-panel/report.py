"""
Sales report ("Rapport") numbers for the dashboard's Report Panel.

Pure, stdlib-only logic (no FastAPI, no ERP calls) so it is easy to test.
The route in main.py feeds it validated invoice rows; this module turns them into:

  revenue / growth vs the previous period of the same length, average basket,
  customer retention, data quality, and the three sentences the panel shows.

Rows are duck-typed: anything with .issue_date, .total, .customer_id and .status works
(erp_schemas.InvoiceRow does).
"""
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

MAX_SPAN_DAYS = 366

# Invoices in these states are not revenue. They are counted as "excluded" rows.
NON_BILLABLE_STATUSES = frozenset({"draft", "cancel", "cancelled", "canceled", "void", "voided"})


@dataclass(frozen=True)
class Period:
    start: date
    end: date                      # inclusive

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def previous(self) -> "Period":
        """The period of the same length that ends the day before this one starts."""
        return Period(self.start - timedelta(days=self.days), self.start - timedelta(days=1))

    def contains(self, d: date) -> bool:
        return self.start <= d <= self.end


def resolve_period(date_from: date | None, date_to: date | None, today: date) -> Period:
    """Both dates, or neither (-> the last 31 days ending today). Raises ValueError otherwise."""
    if date_from is None and date_to is None:
        return Period(today - timedelta(days=30), today)
    if date_from is None or date_to is None:
        raise ValueError("Give both date_from and date_to, or neither")
    if date_from > date_to:
        raise ValueError("date_from must be on or before date_to")
    period = Period(date_from, date_to)
    if period.days > MAX_SPAN_DAYS:
        raise ValueError(f"The period is limited to {MAX_SPAN_DAYS} days")
    return period


def _money(v: Decimal) -> float:
    return float(round(v, 2))


def _pct(v: Decimal | None) -> float | None:
    return None if v is None else float(round(v, 1))


class SalesAccumulator:
    """Feed rows with add(); call result() once at the end. One pass, constant memory per customer."""

    def __init__(self, period: Period):
        self.period = period
        self.previous = period.previous
        self.revenue = Decimal(0)
        self.previous_revenue = Decimal(0)
        self.orders = 0
        self.customers: set[str] = set()
        self.previous_customers: set[str] = set()
        self.in_window = 0             # rows dated inside either period
        self.not_billable = 0

    def add(self, row: Any) -> None:
        d = row.issue_date
        current = self.period.contains(d)
        if not current and not self.previous.contains(d):
            return                     # outside both windows: ignore, not part of this report
        self.in_window += 1
        if (row.status or "").strip().lower() in NON_BILLABLE_STATUSES:
            self.not_billable += 1
            return
        cid = row.customer_id
        if current:
            self.revenue += row.total
            self.orders += 1
            if cid:
                self.customers.add(cid)
        else:
            self.previous_revenue += row.total
            if cid:
                self.previous_customers.add(cid)

    def result(self, skipped: int, retention_target: float) -> dict[str, Any]:
        """`skipped` = rows the ERP returned that failed validation (see erp_export.Stats)."""
        growth = ((self.revenue - self.previous_revenue) / self.previous_revenue * 100
                  if self.previous_revenue > 0 else None)
        basket = self.revenue / self.orders if self.orders else None
        retention = (Decimal(len(self.customers & self.previous_customers))
                     / len(self.previous_customers) * 100
                     if self.previous_customers else None)

        total_rows = self.in_window + skipped
        excluded = self.not_billable + skipped
        quality = (Decimal(total_rows - excluded) / total_rows * 100) if total_rows else None

        return {
            "period": {"from": self.period.start.isoformat(), "to": self.period.end.isoformat(),
                       "days": self.period.days,
                       "previous_from": self.previous.start.isoformat(),
                       "previous_to": self.previous.end.isoformat()},
            "source": "ERP invoices",
            "kpis": {"revenue": _money(self.revenue), "previous_revenue": _money(self.previous_revenue),
                     "growth_pct": _pct(growth),
                     "basket": None if basket is None else _money(basket),
                     "orders": self.orders,
                     "retention_pct": _pct(retention), "retention_target_pct": retention_target},
            "data_quality": {"percent": _pct(quality), "rows_total": total_rows,
                             "rows_excluded": excluded},
            "summary": summary_text(_pct(growth), _pct(retention), retention_target,
                                    self.period.days),
            "data_note": data_note(excluded, total_rows),
        }


def summary_text(growth: float | None, retention: float | None, target: float, days: int) -> str:
    if growth is None:
        head = f"No revenue in the previous {days} days to compare with"
    elif growth == 0:
        head = f"Revenue was flat on the previous {days} days"
    elif growth > 0:
        head = f"Revenue grew {growth:g}% on the previous {days} days"
    else:
        head = f"Revenue fell {abs(growth):g}% on the previous {days} days"

    if retention is None:
        return head + "."
    tail = "retention slipped below target" if retention < target else "retention is on target"
    return f"{head}; {tail}."


def data_note(excluded: int, total: int) -> str:
    return f"{excluded:,} of {total:,} rows were invalid or not billable and were excluded"
