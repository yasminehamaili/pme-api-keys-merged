"""Mode B import: pure tests (`pytest --noconftest tests/test_ingest.py`)."""
import io

import pytest
from openpyxl import Workbook

import ingest
from erp_schemas import RESOURCES

INV = RESOURCES["invoices"]
CSV = (
    "Invoice No;Customer;Invoice Date;Amount Total;Status;Notes\n"
    "INV-1;Acme;2026-03-01;1 000,50;posted;x\n"      # EU number: "1 000,50" is invalid for our parser? see test
    "INV-2;Acme;2026-03-02;200.00;posted;x\n"
    "INV-2;Acme;2026-03-02;200.00;posted;dup\n"
    "INV-4;Acme;not-a-date;50;posted;x\n"
    ";;;;;\n"
)


def run(data: bytes, name="inv.csv", **kw):
    return ingest.analyze(io.BytesIO(data), name, INV, **kw)


def test_requires_mappable_columns():
    with pytest.raises(ingest.IngestError, match="required column"):
        run(b"foo;bar\n1;2\n")


def test_auto_mapping_and_quality_report():
    r = run(CSV.encode())                                 # no id column: number stands in for it
    assert r["mapping"]["Invoice No"] == "number"
    assert r["mapping"]["Amount Total"] == "total"        # alias amount_total
    assert r["mapping"]["Invoice Date"] == "issue_date"   # alias invoice_date
    assert r["unmapped_columns"] == ["Notes"]
    assert r["rows"]["blank_skipped"] == 1
    assert r["rows"]["total"] == 4
    bad = {i["row"] for i in r["invalid_sample"]}
    assert 5 in bad                                       # the "not-a-date" row (spreadsheet row 5)
    assert r["duplicates"]["count"] == 1
    assert r["duplicates"]["sample"][0] == {"key": "INV-2", "row": 4, "first_seen_row": 3}
    assert "values" not in str(r["invalid_sample"])       # field names/types only


def test_xlsx_and_explicit_mapping_wrong_field():
    wb = Workbook()
    ws = wb.active
    ws.append(["id", "number", "issue_date", "total"])
    ws.append([1, "A1", "2026-01-01", 10])
    buf = io.BytesIO()
    wb.save(buf)
    r = run(buf.getvalue(), "inv.xlsx")
    assert r["rows"] == {"total": 1, "valid": 1, "invalid": 0, "blank_skipped": 0}
    assert r["quality_score"] == 100.0
    with pytest.raises(ingest.IngestError, match="unknown field"):
        run(CSV.encode(), mapping={"Notes": "nope"})


def test_unsupported_and_empty():
    with pytest.raises(ingest.IngestError, match="unsupported"):
        run(b"abc", "x.pdf")
    with pytest.raises(ingest.IngestError, match="empty"):
        run(b"", "x.csv")


def test_decimal_comma_is_not_read_as_thousands():
    from erp_schemas import InvoiceRow
    def total(v):
        return str(InvoiceRow.model_validate({"id": 1, "number": "x", "issue_date": "2026-01-01", "total": v}).total)
    assert total("1 000,50") == "1000.50"
    assert total("1.234,56") == "1234.56"
    assert total("1,234.56") == "1234.56"
    assert total("1,234") == "1234.00"
    assert total("12,5") == "12.50"
    assert total("(12.30)") == "-12.30"
