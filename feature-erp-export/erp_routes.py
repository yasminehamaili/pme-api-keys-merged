"""
ERP export endpoints (all stateless, all protected by your existing API-key auth + rate limits).

  GET /v1/erp/resources                  what can be exported
  GET /v1/erp/{resource}                 same, dedicated route per resource (invoices, inventory, customers)
  GET /v1/erp/export/{resource}          one resource as csv | json | ndjson | xlsx
  GET /v1/erp/export-bundle              several resources: one ZIP (csv/json/ndjson) or one XLSX workbook
"""
import logging
import time
from datetime import date, datetime, timezone
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from redis.exceptions import RedisError

from erp_client import (ERPAuthError, ERPBadResponse, ERPError, ERPRateLimited, ERPTimeout,
                        ERPUnavailable, get_erp_client)
from erp_config import ERPNotConfigured, get_settings
from erp_export import (FORMATS, ZIP_MEDIA, ExportTooLarge, build_xlsx, build_zip, file_chunks,
                        instrumented, open_rows, utc_stamp)
from erp_schemas import RESOURCES
from ratelimit import enforce_limits, hit

log = logging.getLogger("erp")

router = APIRouter(prefix="/v1/erp", tags=["erp-export"])

FormatName = Literal["csv", "json", "ndjson", "xlsx"]


# ------------------------------------------------------------- dependencies
async def export_guard(auth: dict = Depends(enforce_limits)) -> dict:
    """API-key auth + daily/minute limits (enforce_limits) + a tighter per-key export limit."""
    try:
        per_minute = get_settings().export_per_minute
    except ERPNotConfigured:
        raise HTTPException(503, "ERP export is not configured")
    sec = int(time.time())
    try:
        ok, _ = await hit(f"rl:erp:{auth['key_id']}:{sec // 60}", per_minute, 60)
    except RedisError:
        log.exception("redis unavailable")
        raise HTTPException(503, "Rate limiter unavailable, try again shortly")
    if not ok:
        raise HTTPException(429, "Too many exports, slow down",
                            headers={"Retry-After": str(60 - sec % 60)})
    return auth


class Filters:
    """Query-string filters. Only the ones a resource declares in erp_schemas.RESOURCES are used."""

    def __init__(
        self,
        date_from: date | None = None,
        date_to: date | None = None,
        updated_since: datetime | None = None,
        status: Annotated[str | None, Query(max_length=40, pattern=r"^[\w\- ]+$")] = None,
        max_rows: Annotated[int | None, Query(ge=1, le=10_000_000,
                                              description="Stop after this many rows")] = None,
    ):
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must be on or before date_to")
        self.max_rows = max_rows
        self.values: dict[str, Any] = {k: v for k, v in {
            "date_from": date_from, "date_to": date_to,
            "updated_since": updated_since, "status": status}.items() if v is not None}


# ----------------------------------------------------------- error mapping
def http_error(exc: Exception, ctx: dict[str, Any]) -> HTTPException:
    """Turn connector errors into clean HTTP errors. ERP internals only go to the log."""
    log.warning("erp export failed", extra={**ctx, "kind": type(exc).__name__, "detail": str(exc)})
    if isinstance(exc, ERPNotConfigured):
        return HTTPException(503, "ERP export is not configured")
    if isinstance(exc, ExportTooLarge):
        return HTTPException(413, f"Export exceeds {exc.limit} rows. Narrow the filters "
                                  "(date_from, date_to, updated_since) or split the export.")
    if isinstance(exc, ERPTimeout):
        return HTTPException(504, exc.public)
    if isinstance(exc, ERPRateLimited):
        headers = {"Retry-After": str(int(exc.retry_after or 30))}
        return HTTPException(503, exc.public, headers=headers)
    if isinstance(exc, (ERPUnavailable, ERPAuthError, ERPBadResponse)):
        return HTTPException(502, exc.public)
    return HTTPException(502, ERPError.public)


def _headers(filename: str, size: int | None = None, rows: int | None = None,
             skipped: int | None = None) -> dict[str, str]:
    h = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "no-store",
        "X-Accel-Buffering": "no",
    }
    if size is not None:
        h["Content-Length"] = str(size)
    if rows is not None:
        h["X-Export-Rows"] = str(rows)
    if skipped is not None:
        h["X-Export-Skipped-Rows"] = str(skipped)
    return h


# ------------------------------------------------------------------- routes
@router.get("/resources")
async def list_resources(_: dict = Depends(enforce_limits)):
    return {
        "formats": list(FORMATS),
        "resources": {r.name: {"columns": r.columns, "filters": list(r.filters)}
                      for r in RESOURCES.values()},
    }


@router.get("/export/{resource}")
async def export_resource(
    resource: str,
    fmt: Annotated[FormatName, Query(alias="format")] = "csv",
    filters: Filters = Depends(),
    auth: dict = Depends(export_guard),
):
    spec = RESOURCES.get(resource)
    if spec is None:
        raise HTTPException(404, f"Unknown resource. Choose one of: {', '.join(RESOURCES)}")
    unsupported = sorted(set(filters.values) - set(spec.filters))
    if unsupported:
        raise HTTPException(422, f"'{resource}' does not support filter(s): {', '.join(unsupported)}")

    ctx = {"customer_id": auth["customer_id"], "resource": resource, "format": fmt}
    started = time.perf_counter()
    try:
        cfg = get_settings()
        rows, stats = await open_rows(get_erp_client(), spec, filters.values,
                                      filters.max_rows, cfg.max_export_rows)
        fmt_info = FORMATS[fmt]
        filename = f"{resource}_{utc_stamp()}.{fmt_info.ext}"

        if fmt_info.chunker is None:                      # xlsx: build on disk, then stream
            tmp, size = await build_xlsx([(spec, rows, stats)])
            log.info("erp export done", extra={**ctx, "rows": stats.rows, "skipped": stats.skipped,
                                                "ms": round((time.perf_counter() - started) * 1000, 1)})
            return StreamingResponse(file_chunks(tmp), media_type=fmt_info.media_type,
                                     headers=_headers(filename, size, stats.rows, stats.skipped))

        # csv / json / ndjson: the first ERP page is already fetched, so any ERP error above
        # became a clean HTTP error. From here on bytes flow straight to the client.
        body = fmt_info.chunker(spec, rows)
        first = await anext(body)           # forces the first page to be parsed/validated too
    except (ERPError, ERPNotConfigured, ExportTooLarge) as exc:
        raise http_error(exc, ctx)

    async def stream():
        yield first
        async for chunk in body:
            yield chunk

    return StreamingResponse(instrumented(stream(), [stats], ctx), media_type=fmt_info.media_type,
                             headers=_headers(filename))


@router.get("/export-bundle")
async def export_bundle(
    fmt: Annotated[FormatName, Query(alias="format")] = "csv",
    resources: Annotated[str, Query(max_length=200, pattern=r"^[a-z_,]+$")] = ",".join(RESOURCES),
    filters: Filters = Depends(),
    auth: dict = Depends(export_guard),
):
    names = list(dict.fromkeys(n for n in resources.split(",") if n))
    unknown = [n for n in names if n not in RESOURCES]
    if unknown or not names:
        raise HTTPException(422, f"Unknown resource(s) {unknown}. Choose from: {', '.join(RESOURCES)}")

    ctx = {"customer_id": auth["customer_id"], "resource": ",".join(names),
           "format": fmt, "bundle": True}
    started = time.perf_counter()
    try:
        cfg = get_settings()
        client = get_erp_client()
        items = []
        for name in names:
            spec = RESOURCES[name]
            applicable = {k: v for k, v in filters.values.items() if k in spec.filters}
            rows, stats = await open_rows(client, spec, applicable, filters.max_rows,
                                          cfg.max_export_rows)
            items.append((spec, rows, stats))

        stamp = utc_stamp()
        if fmt == "xlsx":                                  # one workbook, one sheet per resource
            tmp, size = await build_xlsx(items)
            filename, media = f"erp_export_{stamp}.xlsx", FORMATS["xlsx"].media_type
        else:
            report = {"generated_at": datetime.now(timezone.utc).isoformat(), "format": fmt,
                      "filters": {k: str(v) for k, v in filters.values.items()}, "resources": {}}
            tmp, size = await build_zip(items, FORMATS[fmt], report)
            filename, media = f"erp_export_{stamp}.zip", ZIP_MEDIA
    except (ERPError, ERPNotConfigured, ExportTooLarge) as exc:
        raise http_error(exc, ctx)

    rows_total = sum(s.rows for _, _, s in items)
    skipped_total = sum(s.skipped for _, _, s in items)
    log.info("erp export done", extra={**ctx, "rows": rows_total, "skipped": skipped_total,
                                        "ms": round((time.perf_counter() - started) * 1000, 1)})
    return StreamingResponse(file_chunks(tmp), media_type=media,
                             headers=_headers(filename, size, rows_total, skipped_total))


# ------------------------------------------- one dedicated endpoint per resource
# GET /v1/erp/invoices, /v1/erp/inventory, /v1/erp/customers ... generated from RESOURCES, so a
# new entry in erp_schemas.RESOURCES gets its own endpoint automatically. Each call is
# independent: clients can request them in parallel instead of waiting on the bundle.
def _register_resource_route(name: str) -> None:
    async def handler(
        fmt: Annotated[FormatName, Query(alias="format")] = "csv",
        filters: Filters = Depends(),
        auth: dict = Depends(export_guard),
    ):
        return await export_resource(name, fmt, filters, auth)

    handler.__name__ = f"export_{name}"
    router.add_api_route(f"/{name}", handler, methods=["GET"], name=f"export_{name}")


for _name in RESOURCES:
    _register_resource_route(_name)
