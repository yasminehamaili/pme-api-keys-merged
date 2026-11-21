"""
Strict export schemas + ERP field mapping.

Every ERP record is validated and normalised here BEFORE it is written to a file.
Records that cannot be normalised are skipped and counted (never exported half-broken).

>>> EDIT THE `validation_alias=AliasChoices(...)` LISTS to match your ERP's field names. <<<
"""
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Annotated, Any

from pydantic import AliasChoices, BaseModel, BeforeValidator, ConfigDict, Field

_EMPTY = {"", "false", "null", "none", "n/a", "na", "nan", "-"}
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ------------------------------------------------------------ normalisers
def _empty(v: Any) -> bool:
    if v is None or v is False:
        return True
    if isinstance(v, float) and math.isnan(v):
        return True
    return isinstance(v, str) and v.strip().lower() in _EMPTY


def clean_text(v: Any) -> str | None:
    if _empty(v):
        return None
    if isinstance(v, (list, tuple)):           # Odoo-style many2one: [id, "Display name"]
        v = v[-1] if v else None
        if _empty(v):
            return None
    if isinstance(v, dict):                    # nested object: take a display name if present
        v = v.get("name") or v.get("display_name") or v.get("id")
        if _empty(v):
            return None
    if v is True:
        return "true"
    s = _CONTROL.sub("", str(v))
    s = re.sub(r"[ \t]+", " ", s).strip()
    return s[:2000] or None


def clean_id(v: Any) -> str | None:
    if isinstance(v, (list, tuple)) and v:     # [id, "name"] -> id
        v = v[0]
    if isinstance(v, bool) or _empty(v):
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return clean_text(v)


def _decimal(places: int):
    q = Decimal(1).scaleb(-places)

    def conv(v: Any) -> Decimal | None:
        if _empty(v):
            return None
        if isinstance(v, bool):
            raise ValueError("boolean is not a number")
        if isinstance(v, str):
            s = re.sub(r"[^\d.,()\-+eE]", "", v)           # drop currency symbols / spaces
            neg = s.startswith("(") and s.endswith(")")     # accounting negative: (12.30)
            s = s.strip("()")
            if "," in s and "." in s:                       # last separator is the decimal one
                dec = "," if s.rfind(",") > s.rfind(".") else "."
                thou = "." if dec == "," else ","
                s = s.replace(thou, "").replace(dec, ".")
            elif "," in s:
                if re.fullmatch(r"[+\-]?\d{1,3}(,\d{3})+", s):
                    s = s.replace(",", "")                  # 1,234 / 1,234,567 -> thousands
                elif s.count(",") == 1:
                    s = s.replace(",", ".")                 # 1000,50 / 12,5 -> decimal comma
                else:
                    raise ValueError(f"ambiguous number: {v!r}")
            v = f"-{s}" if neg else s
        try:
            d = Decimal(str(v))
        except InvalidOperation:
            raise ValueError(f"not a number: {v!r}")
        if not d.is_finite():
            raise ValueError("not a finite number")
        try:
            return d.quantize(q, rounding=ROUND_HALF_UP)
        except InvalidOperation:
            raise ValueError("number out of range")

    return conv


_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d", "%Y%m%d", "%d-%m-%Y")


def clean_date(v: Any) -> date | None:
    if _empty(v):
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognised date: {v!r}")


def clean_datetime(v: Any) -> datetime | None:
    """-> timezone-aware UTC datetime. Naive values are assumed to already be UTC."""
    if _empty(v):
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if v > 1e11:                                        # epoch milliseconds
            v = v / 1000
        try:
            return datetime.fromtimestamp(v, tz=timezone.utc)
        except (OverflowError, OSError):
            raise ValueError(f"timestamp out of range: {v!r}")
    if isinstance(v, datetime):
        dt = v
    else:
        s = str(v).strip()
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            d = clean_date(s)
            dt = datetime(d.year, d.month, d.day)
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def clean_bool(v: Any) -> bool | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in {"1", "true", "yes", "y", "active", "t"}:
        return True
    if s in {"0", "false", "no", "n", "inactive", "archived", "f"}:
        return False
    if s in _EMPTY:
        return None
    raise ValueError(f"not a boolean: {v!r}")


def clean_currency(v: Any) -> str | None:
    s = clean_text(v)
    if s is None:
        return None
    s = s.upper()
    if not re.fullmatch(r"[A-Z]{3}", s):
        raise ValueError(f"not an ISO currency code: {v!r}")
    return s


def clean_email(v: Any) -> str | None:
    s = clean_text(v)
    s = s.lower() if s else None
    return s if s and _EMAIL.match(s) else None             # junk emails become empty, not errors


def clean_country(v: Any) -> str | None:
    s = clean_text(v)
    return s.upper() if s and len(s) == 2 else s


Id = Annotated[str, BeforeValidator(clean_id)]
IdOpt = Annotated[str | None, BeforeValidator(clean_id)]
Text = Annotated[str, BeforeValidator(clean_text)]
TextOpt = Annotated[str | None, BeforeValidator(clean_text)]
Money = Annotated[Decimal, BeforeValidator(_decimal(2))]
MoneyOpt = Annotated[Decimal | None, BeforeValidator(_decimal(2))]
Qty = Annotated[Decimal, BeforeValidator(_decimal(3))]
QtyOpt = Annotated[Decimal | None, BeforeValidator(_decimal(3))]
DateReq = Annotated[date, BeforeValidator(clean_date)]
DateOpt = Annotated[date | None, BeforeValidator(clean_date)]
DateTimeOpt = Annotated[datetime | None, BeforeValidator(clean_datetime)]
BoolOpt = Annotated[bool | None, BeforeValidator(clean_bool)]
Currency = Annotated[str | None, BeforeValidator(clean_currency)]
Email = Annotated[str | None, BeforeValidator(clean_email)]
Country = Annotated[str | None, BeforeValidator(clean_country)]


def A(*names: str):
    """Field whose value is read from the first ERP key that is present."""
    return Field(validation_alias=AliasChoices(*names))


def O(*names: str):
    """Same, optional (defaults to None)."""
    return Field(default=None, validation_alias=AliasChoices(*names))


class ExportRow(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, frozen=True)


# ==================== FIELD MAPPING  (edit the alias lists) ====================
class InvoiceRow(ExportRow):
    id: Id = A("id", "invoice_id")
    number: Text = A("number", "invoice_number", "invoice_no", "name", "doc_no")
    customer_id: IdOpt = O("customer_id", "partner_id", "customer")
    customer_name: TextOpt = O("customer_name", "partner_name", "partner_id", "customer")
    status: TextOpt = O("status", "state")
    currency: Currency = O("currency", "currency_code", "currency_id")
    issue_date: DateReq = A("issue_date", "invoice_date", "date")
    due_date: DateOpt = O("due_date", "invoice_date_due", "payment_due")
    subtotal: MoneyOpt = O("subtotal", "amount_untaxed", "net_amount")
    tax: MoneyOpt = O("tax", "amount_tax", "tax_amount")
    total: Money = A("total", "amount_total", "gross_amount")
    amount_due: MoneyOpt = O("amount_due", "amount_residual", "balance")
    updated_at: DateTimeOpt = O("updated_at", "write_date", "modified_at")


class InventoryRow(ExportRow):
    sku: Text = A("sku", "default_code", "item_code")
    name: TextOpt = O("name", "description", "item_name")
    warehouse: TextOpt = O("warehouse", "warehouse_id", "location")
    quantity_on_hand: Qty = A("quantity_on_hand", "qty_available", "on_hand", "quantity")
    quantity_reserved: QtyOpt = O("quantity_reserved", "reserved", "qty_reserved")
    unit_cost: MoneyOpt = O("unit_cost", "standard_price", "cost")
    reorder_level: QtyOpt = O("reorder_level", "reorder_point", "min_qty")
    updated_at: DateTimeOpt = O("updated_at", "write_date", "modified_at")


class CustomerRow(ExportRow):
    id: Id = A("id", "customer_id", "partner_id")
    name: Text = A("name", "customer_name", "display_name")
    email: Email = O("email", "email_address")
    phone: TextOpt = O("phone", "telephone", "mobile")
    country: Country = O("country", "country_code", "country_id")
    city: TextOpt = O("city", "town")
    address: TextOpt = O("address", "street")
    tax_id: TextOpt = O("tax_id", "vat", "tax_number")
    is_active: BoolOpt = O("is_active", "active")
    created_at: DateTimeOpt = O("created_at", "create_date")


class SalesOrderRow(ExportRow):
    id: Id = A("id", "order_id")
    number: Text = A("number", "order_number", "order_no", "name")
    customer_id: IdOpt = O("customer_id", "partner_id", "customer")
    order_date: DateReq = A("order_date", "date_order", "date")
    status: TextOpt = O("status", "state")
    total: Money = A("total", "amount_total", "gross_amount")
    cogs: MoneyOpt = O("cogs", "cost_of_goods_sold", "total_cost")
    returned: BoolOpt = O("returned", "is_returned", "has_return")
    updated_at: DateTimeOpt = O("updated_at", "write_date", "modified_at")


class PurchaseOrderRow(ExportRow):
    id: Id = A("id", "purchase_order_id", "po_id")
    number: Text = A("number", "po_number", "po_no", "name")
    supplier_id: IdOpt = O("supplier_id", "partner_id", "vendor_id", "supplier")
    supplier_name: TextOpt = O("supplier_name", "vendor_name", "partner_id", "supplier")
    order_date: DateReq = A("order_date", "date_order", "date")
    promised_date: DateOpt = O("promised_date", "date_planned", "expected_date", "due_date")
    received_date: DateOpt = O("received_date", "date_received", "date_done", "delivery_date")
    status: TextOpt = O("status", "state")
    quantity_ordered: QtyOpt = O("quantity_ordered", "qty_ordered", "product_qty")
    quantity_received: QtyOpt = O("quantity_received", "qty_received")
    unit_price: MoneyOpt = O("unit_price", "price_unit", "actual_price")
    standard_price: MoneyOpt = O("standard_price", "std_price", "reference_price")
    updated_at: DateTimeOpt = O("updated_at", "write_date", "modified_at")


class ProductionOrderRow(ExportRow):
    id: Id = A("id", "production_order_id", "mo_id")
    number: Text = A("number", "mo_number", "mo_no", "name")
    product_id: IdOpt = O("product_id", "product", "sku")
    status: TextOpt = O("status", "state")
    start_date: DateReq = A("start_date", "date_start", "date_planned_start", "date")
    end_date: DateOpt = O("end_date", "date_finished", "date_end")
    planned_quantity: Qty = A("planned_quantity", "product_qty", "qty_planned")
    produced_quantity: QtyOpt = O("produced_quantity", "qty_produced", "quantity_done")
    defective_quantity: QtyOpt = O("defective_quantity", "qty_defective", "scrap_qty")
    standard_hours: QtyOpt = O("standard_hours", "planned_hours", "std_hours")
    actual_hours: QtyOpt = O("actual_hours", "worked_hours", "duration_hours")
    updated_at: DateTimeOpt = O("updated_at", "write_date", "modified_at")


# ============== resources: URL path on the ERP + how filters map to ERP params ==============
@dataclass(frozen=True)
class Resource:
    name: str
    path: str                       # ERP endpoint, relative to ERP_BASE_URL
    model: type[ExportRow]
    filters: dict[str, str]         # our filter name -> ERP query-param name

    @property
    def columns(self) -> list[str]:
        return list(self.model.model_fields)


RESOURCES: dict[str, Resource] = {
    "invoices": Resource("invoices", "/api/invoices", InvoiceRow, {
        "date_from": "date_from", "date_to": "date_to",
        "updated_since": "updated_since", "status": "status"}),
    "inventory": Resource("inventory", "/api/inventory", InventoryRow, {
        "updated_since": "updated_since"}),
    "customers": Resource("customers", "/api/customers", CustomerRow, {
        "updated_since": "updated_since", "status": "status"}),
    "sales_orders": Resource("sales_orders", "/api/sales-orders", SalesOrderRow, {
        "date_from": "date_from", "date_to": "date_to",
        "updated_since": "updated_since", "status": "status"}),
    "purchase_orders": Resource("purchase_orders", "/api/purchase-orders", PurchaseOrderRow, {
        "date_from": "date_from", "date_to": "date_to",
        "updated_since": "updated_since", "status": "status"}),
    "production_orders": Resource("production_orders", "/api/production-orders", ProductionOrderRow, {
        "date_from": "date_from", "date_to": "date_to",
        "updated_since": "updated_since", "status": "status"}),
}
