"""KPI formulas: pure unit tests (no DB/Redis needed: `pytest --noconftest tests/test_kpi.py`)."""
from decimal import Decimal as D

import kpi


def test_growth_and_status():
    k = kpi.growth(D(90), D(100), kpi.Threshold(D(0), D(-10)))
    assert k.value == D("-10.00") and k.status == "critical"
    assert kpi.growth(D(110), D(100), kpi.Threshold(D(0), D(-10))).status == "normal"


def test_zero_denominator_is_none_not_error():
    k = kpi.growth(D(5), D(0))
    assert k.value is None and k.reason


def test_basket_margin_return_retention():
    assert kpi.average_basket(D(1000), 4).value == D("250.00")
    assert kpi.gross_margin(D(200), D(150)).value == D("25.00")
    assert kpi.return_rate(2, 40).value == D("5.00")
    assert kpi.retention({"a", "b", "c", "d"}, {"b", "c", "x"}).value == D("50.00")


def test_oee_is_availability_x_performance_x_quality():
    k = kpi.oee(D(400), D(480), D("1"), D(380), D(361))
    # 400/480 * (1*380/400) * 361/380 = 0.8333 * 0.95 * 0.95
    assert k.value == D("75.21")
    assert k.inputs["availability"] == "0.8333"


def test_stock_and_procurement():
    assert kpi.available_stock(D(100), D(30)) == D(70)
    assert kpi.stockout_rate(5, 100, kpi.Threshold(D(5), D(15), False)).status == "alert"
    assert kpi.on_time_delivery(9, 10).value == D("90.00")
    assert kpi.average_lead_time([D(4), D(6)]).value == D("5.0")
    s = kpi.supplier_score({"otd": D(90), "price": D(70)}, {"otd": D(3), "price": D(1)})
    assert s.value == D("85.0")


def test_finance():
    assert kpi.operating_margin(D(20), D(200)).value == D("10.00")
    assert kpi.budget_variance(D(110), D(100)).value == D("10.00")
    assert kpi.cash_flow(D(500), D(620)).value == D("-120.00")
    assert kpi.working_capital(D(300), D(120)).value == D("180.00")
