"""
Export service: ERP pages -> validated rows -> CSV / JSON / NDJSON stream, or XLSX / ZIP file.

Memory model
  * csv / json / ndjson : streamed straight to the client, ~64 KB buffered at a time.
  * xlsx / zip          : built row by row into an anonymous temp file (auto-deleted when closed),
                          then streamed from disk. RAM use stays flat regardless of row count.
"""
import asyncio
import csv
import io
import json
import logging
import re
import tempfile
import time
import zipfile
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import IO, Any

from pydantic import ValidationError

from erp_client import ERPClient
from erp_schemas import ExportRow, Resource

log = logging.getLogger("erp")

CHUNK = 64 * 1024
XLSX_MAX_ROWS = 1_048_575          # Excel sheet limit minus the header row


class ExportTooLarge(Exception):
    def __init__(self, limit: int):
        super().__init__(f"export exceeds {limit} rows")
        self.limit = limit


@dataclass
class Stats:
    rows: int = 0
    skipped: int = 0
    logged: int = field(default=0, repr=False)


# ----------------------------------------------------------- ERP -> rows
async def prime_pages(pages: AsyncIterator[list]) -> AsyncIterator[list]:
    """Fetch the first page NOW so ERP errors surface before any response byte is sent."""
    try:
        first = await anext(pages)
    except StopAsyncIteration:
        first = None

    async def chained():
        try:
            if first is not None:
                yield first
            async for page in pages:
                yield page
        finally:
            await pages.aclose()

    return chained()


def build_params(resource: Resource, filters: dict[str, Any]) -> dict[str, str]:
    params = {}
    for ours, theirs in resource.filters.items():
        v = filters.get(ours)
        if v is not None:
            params[theirs] = v.isoformat() if isinstance(v, (date, datetime)) else str(v)
    return params


async def iter_rows(resource: Resource, pages: AsyncIterator[list], stats: Stats,
                    user_limit: int | None, cap: int) -> AsyncIterator[ExportRow]:
    try:
        async for page in pages:
            for raw in page:
                try:
                    row = resource.model.model_validate(raw)
                except (ValidationError, ValueError, TypeError, ArithmeticError) as exc:
                    stats.skipped += 1
                    if stats.logged < 5:        # field names + error types only: never record values
                        stats.logged += 1
                        detail = ([(".".join(map(str, e["loc"])), e["type"]) for e in exc.errors()]
                                  if isinstance(exc, ValidationError) else type(exc).__name__)
                        log.warning("erp row skipped", extra={"resource": resource.name, "errors": detail})
                    continue
                if stats.rows >= cap:
                    raise ExportTooLarge(cap)
                stats.rows += 1
                yield row
                if user_limit is not None and stats.rows >= user_limit:
                    return
    finally:
        await pages.aclose()


async def open_rows(client: ERPClient, resource: Resource, filters: dict[str, Any],
                    user_limit: int | None, cap: int) -> tuple[AsyncIterator[ExportRow], Stats]:
    pages = await prime_pages(client.iter_pages(resource.path, build_params(resource, filters)))
    stats = Stats()
    return iter_rows(resource, pages, stats, user_limit, cap), stats


# ------------------------------------------------------------ cell values
_NUMERIC_LOOKING = re.compile(r"^[+\-]?[\d\s().\-]+$")     # "+33 1 23", "-5": cannot be a formula


def _guard(s: str) -> str:
    """Neutralise spreadsheet formula injection (=, +, -, @ at the start of a text cell)."""
    if s and s[0] in "=+-@\t\r" and not _NUMERIC_LOOKING.match(s):
        return "'" + s
    return s


def _csv_cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, Decimal):
        return format(v, "f")
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return _guard(str(v))


def _xlsx_cell(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc).replace(tzinfo=None)    # Excel has no tz support
    if isinstance(v, str):
        return _guard(v)
    return v


def _values(row: ExportRow) -> list[Any]:
    return list(row.model_dump().values())          # python mode: Decimal/date/datetime kept


# ------------------------------------------------------- chunked formatters
Rows = AsyncIterator[ExportRow]
Chunker = Callable[[Resource, Rows], AsyncIterator[bytes]]


async def csv_chunks(resource: Resource, rows: Rows) -> AsyncIterator[bytes]:
    buf = io.StringIO(newline="")
    buf.write("﻿")                              # BOM so Excel detects UTF-8
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(resource.columns)
    async for row in rows:
        writer.writerow([_csv_cell(v) for v in _values(row)])
        if buf.tell() >= CHUNK:
            yield buf.getvalue().encode("utf-8")
            buf.seek(0)
            buf.truncate(0)
    yield buf.getvalue().encode("utf-8")


async def json_chunks(resource: Resource, rows: Rows) -> AsyncIterator[bytes]:
    parts, size, first = ["["], 1, True
    async for row in rows:
        s = ("" if first else ",\n") + json.dumps(row.model_dump(mode="json"), ensure_ascii=False)
        first = False
        parts.append(s)
        size += len(s)
        if size >= CHUNK:
            yield "".join(parts).encode("utf-8")
            parts, size = [], 0
    parts.append("]\n")
    yield "".join(parts).encode("utf-8")


async def ndjson_chunks(resource: Resource, rows: Rows) -> AsyncIterator[bytes]:
    parts, size = [], 0
    async for row in rows:
        s = json.dumps(row.model_dump(mode="json"), ensure_ascii=False) + "\n"
        parts.append(s)
        size += len(s)
        if size >= CHUNK:
            yield "".join(parts).encode("utf-8")
            parts, size = [], 0
    yield "".join(parts).encode("utf-8")             # may be b"" for an empty export


@dataclass(frozen=True)
class Format:
    ext: str
    media_type: str
    chunker: Chunker | None        # None = file-based format (xlsx)


FORMATS: dict[str, Format] = {
    "csv": Format("csv", "text/csv; charset=utf-8", csv_chunks),
    "json": Format("json", "application/json", json_chunks),
    "ndjson": Format("ndjson", "application/x-ndjson", ndjson_chunks),
    "xlsx": Format("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", None),
}
ZIP_MEDIA = "application/zip"


# ------------------------------------------------------ streaming response
async def instrumented(chunks: AsyncIterator[bytes], stats_list: list[Stats],
                       ctx: dict[str, Any]) -> AsyncIterator[bytes]:
    """Pass chunks through; log the outcome. If the ERP dies mid-stream the exception is
    re-raised so the connection is cut and the client sees an incomplete download."""
    started = time.perf_counter()
    try:
        async for chunk in chunks:
            yield chunk
    except Exception:
        log.exception("erp export aborted mid-stream", extra=ctx)
        raise
    log.info("erp export done", extra={
        **ctx, "rows": sum(s.rows for s in stats_list), "skipped": sum(s.skipped for s in stats_list),
        "ms": round((time.perf_counter() - started) * 1000, 1)})


async def file_chunks(f: IO[bytes]) -> AsyncIterator[bytes]:
    try:
        while True:
            data = await asyncio.to_thread(f.read, CHUNK)
            if not data:
                return
            yield data
    finally:
        f.close()                                    # anonymous temp file is deleted here


def _finish(f: IO[bytes]) -> int:
    f.flush()
    size = f.seek(0, 2)
    f.seek(0)
    return size


# ----------------------------------------------------- file-based builders
def _new_tempfile() -> IO[bytes]:
    return tempfile.TemporaryFile(prefix="erp_export_")


async def build_xlsx(items: list[tuple[Resource, Rows, Stats]]) -> tuple[IO[bytes], int]:
    from openpyxl import Workbook                    # imported lazily: only xlsx needs it

    tmp = _new_tempfile()
    wb = Workbook(write_only=True)
    try:
        for resource, rows, stats in items:
            ws = wb.create_sheet(title=resource.name[:31])
            ws.append(resource.columns)
            async for row in rows:
                if stats.rows > XLSX_MAX_ROWS:
                    raise ExportTooLarge(XLSX_MAX_ROWS)
                ws.append([_xlsx_cell(v) for v in _values(row)])
        await asyncio.to_thread(wb.save, tmp)
        return tmp, _finish(tmp)
    except BaseException:
        tmp.close()
        raise
    finally:
        wb.close()


async def build_zip(items: list[tuple[Resource, Rows, Stats]], fmt: Format,
                    report: dict[str, Any]) -> tuple[IO[bytes], int]:
    assert fmt.chunker is not None
    tmp = _new_tempfile()
    try:
        zf = zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED)
        for resource, rows, stats in items:
            entry = zf.open(f"{resource.name}.{fmt.ext}", "w", force_zip64=True)
            async for chunk in fmt.chunker(resource, rows):
                await asyncio.to_thread(entry.write, chunk)
            entry.close()
            report["resources"][resource.name] = {"rows": stats.rows, "skipped": stats.skipped}
        zf.writestr("_export_report.json", json.dumps(report, indent=2))
        await asyncio.to_thread(zf.close)
        return tmp, _finish(tmp)
    except BaseException:
        tmp.close()
        raise


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
