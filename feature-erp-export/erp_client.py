"""
Async, read-only ERP connector: pagination + timeouts + retries with exponential backoff.

Stateless: nothing is stored. Records are pulled page by page and handed to the caller.
"""
import asyncio
import hashlib
import json
import logging
import random
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from erp_config import ERPSettings, get_settings

log = logging.getLogger("erp")

RETRY_STATUS = {408, 429, 500, 502, 503, 504}


# ------------------------------------------------------------------ errors
class ERPError(Exception):
    """Base class. `public` is safe to show to API clients; details go to the log only."""
    public = "ERP request failed"


class ERPTimeout(ERPError):
    public = "The ERP system timed out"


class ERPUnavailable(ERPError):
    public = "The ERP system is unreachable"


class ERPRateLimited(ERPError):
    public = "The ERP system is rate limiting requests, try again shortly"

    def __init__(self, msg: str = "", retry_after: float | None = None):
        super().__init__(msg)
        self.retry_after = retry_after


class ERPAuthError(ERPError):
    public = "The ERP rejected our credentials"


class ERPBadResponse(ERPError):
    public = "The ERP returned an unexpected response"


# ----------------------------------------------------------------- helpers
def dig(data: Any, dotted: str) -> Any:
    """data["a"]["b"] for 'a.b'. Returns None if any step is missing."""
    for part in filter(None, dotted.split(".")):
        if isinstance(data, dict) and part in data:
            data = data[part]
        else:
            return None
    return data


def _retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(float(raw), 0.0)
    except ValueError:
        try:
            dt = parsedate_to_datetime(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max((dt - datetime.now(timezone.utc)).total_seconds(), 0.0)
        except (TypeError, ValueError):
            return None


def _fingerprint(records: list) -> str:
    blob = json.dumps([records[0], records[-1]], sort_keys=True, default=str)
    return hashlib.sha1(blob.encode()).hexdigest()


# ------------------------------------------------------------------ client
class ERPClient:
    def __init__(self, settings: ERPSettings | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = settings or get_settings()
        c = self.cfg
        headers = {"Accept": "application/json", "User-Agent": "pme-erp-export/1.0"}
        auth = None
        if c.auth_mode == "bearer":
            headers["Authorization"] = f"Bearer {c.api_token}"
        elif c.auth_mode == "header":
            headers[c.auth_header] = c.api_token
        elif c.auth_mode == "basic":
            auth = httpx.BasicAuth(c.username, c.password)
        self._http = httpx.AsyncClient(
            base_url=c.base_url,
            headers=headers,
            auth=auth,
            verify=c.verify_tls,
            follow_redirects=False,
            transport=transport,
            timeout=httpx.Timeout(c.read_timeout, connect=c.connect_timeout, pool=c.connect_timeout),
            limits=httpx.Limits(max_connections=c.max_connections,
                                max_keepalive_connections=c.max_connections),
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---------------------------------------------------------- one request
    async def get_json(self, path: str, params: dict[str, Any]) -> Any:
        c = self.cfg
        started = time.monotonic()
        attempt = 0
        while True:
            retry_after: float | None = None
            try:
                resp = await self._http.get(path, params=params)
            except httpx.TimeoutException as exc:
                failure: ERPError = ERPTimeout(type(exc).__name__)
            except httpx.TransportError as exc:      # connect refused, DNS, TLS, reset...
                failure = ERPUnavailable(type(exc).__name__)
            else:
                status = resp.status_code
                if status in RETRY_STATUS:
                    retry_after = _retry_after(resp)
                    failure = (ERPRateLimited(f"HTTP {status}", retry_after) if status == 429
                               else ERPUnavailable(f"HTTP {status}"))
                elif status in (401, 403):
                    raise ERPAuthError(f"HTTP {status}")
                elif status >= 400:
                    raise ERPBadResponse(f"HTTP {status}")
                else:
                    try:
                        return resp.json()
                    except ValueError:
                        raise ERPBadResponse("body is not valid JSON")

            # ---- retry decision (exponential backoff, full jitter, honours Retry-After)
            if attempt >= c.max_retries:
                raise failure
            delay = random.uniform(0, min(c.backoff_max, c.backoff_base * 2 ** attempt))
            if retry_after is not None:
                delay = min(max(delay, retry_after), c.backoff_max)
            if time.monotonic() - started + delay > c.retry_budget:
                raise failure
            attempt += 1
            log.warning("erp retry", extra={"erp_path": path, "attempt": attempt,
                                            "delay_s": round(delay, 2), "reason": str(failure)})
            await asyncio.sleep(delay)

    # ------------------------------------------------------------ pagination
    def _records(self, data: Any) -> list[Any]:
        found = dig(data, self.cfg.results_key) if self.cfg.results_key else data
        if not isinstance(found, list):
            raise ERPBadResponse("record list not found in response")
        return found

    async def iter_pages(self, path: str, params: dict[str, Any] | None = None,
                         max_pages: int = 100_000) -> AsyncIterator[list[Any]]:
        """Yield lists of raw records, one list per ERP page, until the ERP runs out."""
        c = self.cfg
        offset, last_print = 0, None
        for _ in range(max_pages):
            data = await self.get_json(
                path, {**(params or {}), c.limit_param: c.page_size, c.offset_param: offset})
            records = self._records(data)
            if not records:
                return
            fp = _fingerprint(records)
            if fp == last_print:       # ERP ignores `offset`: stop instead of looping forever
                raise ERPBadResponse("pagination is not advancing")
            last_print = fp
            yield records
            offset += len(records)
            total = dig(data, c.total_key) if c.total_key else None
            if isinstance(total, int) and offset >= total:
                return
            if c.trust_short_pages and len(records) < c.page_size:
                return
        raise ERPBadResponse("too many pages")


# -------------------------------------------------- process-wide singleton
_client: ERPClient | None = None


def get_erp_client() -> ERPClient:
    """Raises ERPNotConfigured if ERP_BASE_URL is missing."""
    global _client
    if _client is None:
        _client = ERPClient()
    return _client


async def close_erp_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
