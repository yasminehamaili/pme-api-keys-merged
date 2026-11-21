"""ERP connector settings, read from the environment (see .env.example)."""
import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


class ERPNotConfigured(RuntimeError):
    pass


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class ERPSettings:
    base_url: str
    auth_mode: str            # bearer | basic | header | none
    api_token: str
    username: str
    password: str
    auth_header: str          # header name used when auth_mode == "header"
    verify_tls: bool
    connect_timeout: float
    read_timeout: float
    max_connections: int
    max_retries: int
    backoff_base: float       # seconds; delay = base * 2**attempt, full jitter
    backoff_max: float        # cap for one sleep (also caps Retry-After)
    retry_budget: float       # max total seconds spent retrying one request
    page_size: int
    limit_param: str
    offset_param: str
    results_key: str          # dotted path to the record list; "" = response IS the list
    total_key: str            # dotted path to the total count; "" = unknown
    trust_short_pages: bool   # stop at the first page shorter than page_size
    max_export_rows: int
    export_per_minute: int    # exports per API key per minute (on top of enforce_limits)


@lru_cache
def get_settings() -> ERPSettings:
    base = os.environ.get("ERP_BASE_URL", "").strip().rstrip("/")
    if not base:
        raise ERPNotConfigured("ERP_BASE_URL is not set")
    if not base.startswith(("https://", "http://")):
        raise ERPNotConfigured("ERP_BASE_URL must start with https:// or http://")
    mode = os.environ.get("ERP_AUTH_MODE", "bearer").strip().lower()
    if mode not in {"bearer", "basic", "header", "none"}:
        raise ERPNotConfigured("ERP_AUTH_MODE must be bearer, basic, header or none")
    return ERPSettings(
        base_url=base,
        auth_mode=mode,
        api_token=os.environ.get("ERP_API_TOKEN", ""),
        username=os.environ.get("ERP_USERNAME", ""),
        password=os.environ.get("ERP_PASSWORD", ""),
        auth_header=os.environ.get("ERP_AUTH_HEADER", "X-API-Key"),
        verify_tls=_bool("ERP_VERIFY_TLS", True),
        connect_timeout=_float("ERP_CONNECT_TIMEOUT", 5),
        read_timeout=_float("ERP_READ_TIMEOUT", 30),
        max_connections=_int("ERP_MAX_CONNECTIONS", 8),
        max_retries=_int("ERP_MAX_RETRIES", 4),
        backoff_base=_float("ERP_BACKOFF_BASE", 0.5),
        backoff_max=_float("ERP_BACKOFF_MAX", 20),
        retry_budget=_float("ERP_RETRY_BUDGET", 60),
        page_size=_int("ERP_PAGE_SIZE", 500),
        limit_param=os.environ.get("ERP_LIMIT_PARAM", "limit"),
        offset_param=os.environ.get("ERP_OFFSET_PARAM", "offset"),
        results_key=os.environ.get("ERP_RESULTS_KEY", "data"),
        total_key=os.environ.get("ERP_TOTAL_KEY", ""),
        trust_short_pages=_bool("ERP_TRUST_SHORT_PAGES", False),
        max_export_rows=_int("ERP_MAX_EXPORT_ROWS", 500_000),
        export_per_minute=_int("ERP_EXPORT_PER_MINUTE", 5),
    )
