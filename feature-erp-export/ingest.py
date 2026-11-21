"""
Mode B: standalone CSV / Excel import -> data-quality report.

Stateless and non-destructive: the uploaded file is never modified or stored. Each row goes
through the SAME strict schemas as ERP data (erp_schemas), headers are auto-mapped to the
canonical fields, and every problem is reported with its source row number (data lineage).
"""
import codecs
import csv
import hashlib
import io
import re
from collections.abc import Iterator
from typing import IO, Any

from pydantic import ValidationError

from erp_schemas import Resource, _empty

MAX_ROWS = 500_000
REPORT_SAMPLE = 50
# resource -> (required field, column that stands in for it when the file has no such column)
ID_FALLBACK = {"invoices": ("id", "number")}


class IngestError(ValueError):
    """Bad upload (unsupported file, unmappable columns...). Message is safe to show the user."""


def _norm(header: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(header or "").lower()).strip("_")


# ------------------------------------------------------------ column mapping
def field_candidates(resource: Resource) -> dict[str, list[str]]:
    """canonical field -> normalised names it may appear under (field name first, then aliases)."""
    out = {}
    for name, info in resource.model.model_fields.items():
        alias = info.validation_alias
        choices = list(getattr(alias, "choices", []) or [])
        out[name] = [_norm(name)] + [_norm(c) for c in choices if isinstance(c, str)]
    return out


def map_columns(headers: list[str], resource: Resource,
                explicit: dict[str, str] | None = None) -> tuple[dict[int, str], list[str]]:
    """-> ({column index: canonical field}, unmapped header names). `explicit` = {header: field}."""
    cands = field_candidates(resource)
    mapping: dict[int, str] = {}
    taken: set[str] = set()
    for idx, h in enumerate(headers):                       # 1) explicit overrides win
        if explicit and h in explicit:
            field = explicit[h]
            if field not in cands:
                raise IngestError(f"mapping targets unknown field '{field}'")
            mapping[idx] = field
            taken.add(field)
    for field, names in cands.items():                      # 2) auto-map by alias priority
        if field in taken:
            continue
        for name in names:
            idx = next((i for i, h in enumerate(headers) if i not in mapping and _norm(h) == name), None)
            if idx is not None:
                mapping[idx] = field
                taken.add(field)
                break
    unmapped = [h for i, h in enumerate(headers) if i not in mapping and str(h or "").strip()]
    required = [n for n, i in resource.model.model_fields.items() if i.is_required()]
    fb = ID_FALLBACK.get(resource.name)                     # files often have no separate id
    missing = [f for f in required if f not in taken and not (fb and f == fb[0] and fb[1] in taken)]
    if missing:
        raise IngestError("required column(s) not found: " + ", ".join(missing) +
                          ". Rename the columns or pass a `mapping`.")
    return mapping, unmapped


# ------------------------------------------------------------------ readers
def _detect_kind(f: IO[bytes], filename: str) -> str:
    f.seek(0)
    head = f.read(4)
    f.seek(0)
    if head[:2] == b"PK":
        return "xlsx"
    if filename.lower().endswith(".xlsx"):
        raise IngestError("file is not a valid .xlsx workbook")
    if filename.lower().endswith((".csv", ".txt", ".tsv")):
        return "csv"
    raise IngestError("unsupported file type: upload .csv or .xlsx")


def _iter_csv(f: IO[bytes]) -> Iterator[list[Any]]:
    sample = f.read(65536)
    f.seek(0)
    enc = "utf-8-sig"
    try:
        codecs.getincrementaldecoder(enc)().decode(sample, final=False)
    except UnicodeDecodeError:
        enc = "cp1252"
    text = sample.decode(enc, errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    wrapper = io.TextIOWrapper(f, encoding=enc, errors="replace", newline="")
    try:
        yield from csv.reader(wrapper, dialect)
    finally:
        wrapper.detach()                                     # leave closing to the caller


def _iter_xlsx(f: IO[bytes], sheet: str | None) -> Iterator[list[Any]]:
    from openpyxl import load_workbook
    try:
        wb = load_workbook(f, read_only=True, data_only=True)
    except Exception:
        raise IngestError("could not read the workbook (corrupt or password-protected?)")
    try:
        if sheet and sheet not in wb.sheetnames:
            raise IngestError(f"sheet '{sheet}' not found; available: {', '.join(wb.sheetnames)}")
        ws = wb[sheet] if sheet else wb.worksheets[0]
        for row in ws.iter_rows(values_only=True):
            yield list(row)
    finally:
        wb.close()


# ------------------------------------------------------------------ analysis
def sha256_of(f: IO[bytes]) -> str:
    f.seek(0)
    h = hashlib.sha256()
    for chunk in iter(lambda: f.read(1 << 20), b""):
        h.update(chunk)
    f.seek(0)
    return h.hexdigest()


def analyze(f: IO[bytes], filename: str, resource: Resource,
            mapping: dict[str, str] | None = None, sheet: str | None = None) -> dict[str, Any]:
    kind = _detect_kind(f, filename)
    rows = _iter_csv(f) if kind == "csv" else _iter_xlsx(f, sheet)
    try:
        headers = [str(h).strip() if h is not None else "" for h in next(rows)]
    except StopIteration:
        raise IngestError("the file is empty")
    col_map, unmapped = map_columns(headers, resource, mapping)
    key_field = resource.columns[0]

    total = valid = blank = 0
    missing = {name: 0 for name in col_map.values()}
    invalid: list[dict] = []
    seen: dict[str, int] = {}
    dup_count, dup_sample = 0, []

    for line, cells in enumerate(rows, start=2):             # spreadsheet row numbers (header = 1)
        if not any(c is not None and str(c).strip() for c in cells):
            blank += 1
            continue
        total += 1
        if total > MAX_ROWS:
            raise IngestError(f"file has more than {MAX_ROWS} rows; split it")
        record = {field: (cells[i] if i < len(cells) else None) for i, field in col_map.items()}
        for field, value in record.items():
            if _empty(value):
                missing[field] += 1
        fb = ID_FALLBACK.get(resource.name)
        if fb and fb[0] not in record and fb[1] in record:
            record[fb[0]] = record[fb[1]]                     # synthesised id (not counted as missing)
        try:
            model = resource.model.model_validate(record)
        except (ValidationError, ValueError, TypeError, ArithmeticError) as exc:
            if len(invalid) < REPORT_SAMPLE:
                errs = ([{"field": ".".join(map(str, e["loc"])), "type": e["type"]}
                         for e in exc.errors()] if isinstance(exc, ValidationError)
                        else [{"field": None, "type": type(exc).__name__}])
                invalid.append({"row": line, "errors": errs})
            continue
        valid += 1
        key = str(getattr(model, key_field))
        if key in seen:
            dup_count += 1
            if len(dup_sample) < 20:
                dup_sample.append({"key": key, "row": line, "first_seen_row": seen[key]})
        else:
            seen[key] = line

    return {
        "resource": resource.name,
        "source": {"filename": filename, "format": kind},
        "mapping": {headers[i]: field for i, field in col_map.items()},
        "unmapped_columns": unmapped,
        "rows": {"total": total, "valid": valid, "invalid": total - valid, "blank_skipped": blank},
        "invalid_sample": invalid,
        "duplicates": {"key_field": key_field, "count": dup_count, "sample": dup_sample},
        "missing_values": {f: n for f, n in missing.items() if n},
        "quality_score": round(100 * valid / total, 2) if total else None,
    }
