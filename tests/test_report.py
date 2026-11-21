"""Report maths (report.py). Pure functions: no database, Redis or ERP needed."""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pytest

from report import Period, SalesAccumulator, data_note, resolve_period, summary_text

P = Period(date(2026, 10, 1), date(2026, 10, 31))     # 31 days; previous = 30 Aug .. 30 Sep


@dataclass
class Row:
    issue_date: date
    total: Decimal
    customer_id: str | None = "c1"
    status: str | None = "posted"


def run(rows, skipped=0, target=60.0):
    acc = SalesAccumulator(P)
    for r in rows:
        acc.add(r)
    return acc.result(skipped, target)


def test_previous_period_has_same_length():
    prev = P.previous
    assert prev.end == date(2026, 9, 30) and prev.days == P.days == 31


def test_growth_basket_and_retention():
    rows = [
        Row(date(2026, 9, 10), Decimal("100"), "a"), Row(date(2026, 9, 11), Decimal("100"), "b"),
        Row(date(2026, 9, 12), Decimal("100"), "c"), Row(date(2026, 9, 13), Decimal("100"), "d"),
        Row(date(2026, 10, 5), Decimal("216"), "a"), Row(date(2026, 10, 6), Decimal("216"), "b"),
        Row(date(2026, 10, 7), Decimal("0.00") + Decimal("0"), "z"),
    ]
    r = run(rows)
    assert r["kpis"]["revenue"] == 432.0 and r["kpis"]["previous_revenue"] == 400.0
    assert r["kpis"]["growth_pct"] == 8.0
    assert r["kpis"]["orders"] == 3 and r["kpis"]["basket"] == 144.0
    assert r["kpis"]["retention_pct"] == 50.0            # a and b came back, c and d did not
    assert r["summary"] == "Revenue grew 8% on the previous 31 days; retention slipped below target."


def test_rows_outside_both_windows_are_ignored():
    r = run([Row(date(2025, 1, 1), Decimal("999")), Row(date(2026, 11, 2), Decimal("999"))])
    assert r["kpis"]["revenue"] == 0 and r["data_quality"]["rows_total"] == 0
    assert r["data_quality"]["percent"] is None


def test_not_billable_and_invalid_rows_are_excluded_and_lower_quality():
    rows = [Row(date(2026, 10, 2), Decimal("10")), Row(date(2026, 10, 3), Decimal("10")),
            Row(date(2026, 10, 4), Decimal("500"), status="Draft"),
            Row(date(2026, 10, 5), Decimal("500"), status=" cancelled ")]
    r = run(rows, skipped=6)                              # 4 in window + 6 invalid = 10 rows
    assert r["kpis"]["revenue"] == 20.0
    assert r["data_quality"] == {"percent": 20.0, "rows_total": 10, "rows_excluded": 8}
    assert r["data_note"] == "8 of 10 rows were invalid or not billable and were excluded"


def test_no_previous_revenue_means_no_growth_figure():
    r = run([Row(date(2026, 10, 2), Decimal("50"))])
    assert r["kpis"]["growth_pct"] is None and r["kpis"]["retention_pct"] is None
    assert r["summary"] == "No revenue in the previous 31 days to compare with."


def test_missing_customer_ids_do_not_count_for_retention():
    rows = [Row(date(2026, 9, 2), Decimal("5"), None), Row(date(2026, 10, 2), Decimal("5"), None)]
    assert run(rows)["kpis"]["retention_pct"] is None


@pytest.mark.parametrize("growth,retention,expected", [
    (8.2, 50.0, "Revenue grew 8.2% on the previous 31 days; retention slipped below target."),
    (-3.5, 75.0, "Revenue fell 3.5% on the previous 31 days; retention is on target."),
    (0.0, None, "Revenue was flat on the previous 31 days."),
])
def test_summary_sentences(growth, retention, expected):
    assert summary_text(growth, retention, 60.0, 31) == expected


def test_data_note_uses_thousands_separators():
    assert data_note(212, 4310) == "212 of 4,310 rows were invalid or not billable and were excluded"


def test_resolve_period():
    today = date(2026, 11, 21)
    assert resolve_period(None, None, today) == Period(date(2026, 10, 22), today)
    assert resolve_period(date(2026, 10, 1), date(2026, 10, 31), today) == P
    for bad in [(date(2026, 10, 1), None), (None, date(2026, 10, 1)),
                (date(2026, 11, 2), date(2026, 11, 1)), (date(2024, 1, 1), date(2026, 1, 1))]:
        with pytest.raises(ValueError):
            resolve_period(*bad, today)
