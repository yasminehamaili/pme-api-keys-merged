"""
POST /v1/ingest/validate/{resource}   (multipart: file=<.csv|.xlsx>, mapping=<json>, sheet=<name>)

Mode B (no ERP needed): upload a file, get a data-quality report. Nothing is stored.
Protected by the same API-key auth + rate limits as the rest of /v1.
"""
import asyncio
import json
import logging
import os
import tempfile
import time
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from redis.exceptions import RedisError

import ingest
from erp_schemas import RESOURCES
from ratelimit import enforce_limits, hit

log = logging.getLogger("ingest")

router = APIRouter(prefix="/v1/ingest", tags=["ingest"])


def _max_bytes() -> int:
    return int(os.environ.get("INGEST_MAX_MB", "25")) * 1024 * 1024


async def ingest_guard(auth: dict = Depends(enforce_limits)) -> dict:
    limit = int(os.environ.get("INGEST_PER_MINUTE", "10"))
    sec = int(time.time())
    try:
        ok, _ = await hit(f"rl:ingest:{auth['key_id']}:{sec // 60}", limit, 60)
    except RedisError:
        log.exception("redis unavailable")
        raise HTTPException(503, "Rate limiter unavailable, try again shortly")
    if not ok:
        raise HTTPException(429, "Too many uploads, slow down",
                            headers={"Retry-After": str(60 - sec % 60)})
    return auth


@router.post("/validate/{resource}")
async def validate_file(
    resource: str,
    file: Annotated[UploadFile, File()],
    mapping: Annotated[str | None, Form(max_length=5000, description='JSON {"Column header": "field"}')] = None,
    sheet: Annotated[str | None, Form(max_length=31)] = None,
    auth: dict = Depends(ingest_guard),
):
    spec = RESOURCES.get(resource)
    if spec is None:
        raise HTTPException(404, f"Unknown resource. Choose one of: {', '.join(RESOURCES)}")
    explicit = None
    if mapping:
        try:
            explicit = json.loads(mapping)
            if not isinstance(explicit, dict) or not all(
                    isinstance(k, str) and isinstance(v, str) for k, v in explicit.items()):
                raise ValueError
        except ValueError:
            raise HTTPException(422, 'mapping must be a JSON object like {"Invoice No": "number"}')

    limit = _max_bytes()
    started = time.perf_counter()
    with tempfile.TemporaryFile(prefix="ingest_") as tmp:        # auto-deleted, never kept
        size = 0
        while chunk := await file.read(1 << 20):                 # copy to disk, bounded
            size += len(chunk)
            if size > limit:
                raise HTTPException(413, f"File too large (max {limit // 1048576} MB)")
            tmp.write(chunk)
        if size == 0:
            raise HTTPException(422, "The file is empty")
        try:
            sha = await asyncio.to_thread(ingest.sha256_of, tmp)
            report = await asyncio.to_thread(ingest.analyze, tmp, file.filename or "upload",
                                             spec, explicit, sheet)
        except ingest.IngestError as exc:
            raise HTTPException(422, str(exc))

    report["source"].update({"sha256": sha, "bytes": size})
    log.info("ingest validated", extra={
        "customer_id": auth["customer_id"], "resource": resource, "rows": report["rows"]["total"],
        "invalid": report["rows"]["invalid"], "ms": round((time.perf_counter() - started) * 1000, 1)})
    return report
