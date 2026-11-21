"""
Key + password + session security.

- API keys:  "pme_" + 256 random bits. Stored as HMAC-SHA256(server pepper, key).
  The pepper lives only in the environment (KEY_PEPPER), so a leaked database
  alone is not enough to check guesses. Changing the pepper invalidates ALL keys.
- Passwords: scrypt with a per-user random salt.
- Dashboard sessions: signed, expiring token (HMAC-SHA256 with SESSION_SECRET).
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import uuid

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

load_dotenv()

KEY_TAG = "pme_"
KEY_PREFIX_LEN = 12
MAX_KEY_LEN = 200
SESSION_TTL_SECONDS = 8 * 3600
SESSION_TAG = "pmes."


def _secret(name: str) -> bytes:
    value = os.environ.get(name, "")
    if len(value) < 32:
        raise RuntimeError(f"{name} must be set to a random string of at least 32 characters")
    return value.encode()


def check_config() -> None:
    """Call at startup so a missing secret fails immediately, not on first request."""
    _secret("KEY_PEPPER")
    _secret("SESSION_SECRET")


# ------------------------------------------------------------------ API keys
def hash_api_key(raw: str) -> str:
    return hmac.new(_secret("KEY_PEPPER"), raw.encode(), hashlib.sha256).hexdigest()


def generate_api_key() -> tuple[str, str, str]:
    """-> (full_key, key_prefix, key_hash). full_key is shown to the customer once."""
    raw = KEY_TAG + secrets.token_urlsafe(32)
    return raw, raw[:KEY_PREFIX_LEN], hash_api_key(raw)


def looks_like_api_key(raw: str) -> bool:
    return raw.startswith(KEY_TAG) and len(raw) <= MAX_KEY_LEN


def parse_bearer(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


async def authenticate_key(session: AsyncSession, raw: str) -> dict | None:
    """Return the key's owner info if the key exists and is active, else None."""
    row = (
        await session.execute(
            text(
                "SELECT k.id AS key_id, k.customer_id, k.key_prefix, c.daily_limit "
                "FROM api_keys k JOIN customers c ON c.id = k.customer_id "
                "WHERE k.key_hash = :h AND k.is_active"
            ),
            {"h": hash_api_key(raw)},
        )
    ).mappings().first()
    return dict(row) if row else None


# ----------------------------------------------------------------- passwords
_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt_hex, dk_hex = stored.split("$")
        dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex),
                            n=int(n), r=int(r), p=int(p))
        return hmac.compare_digest(dk.hex(), dk_hex)
    except (ValueError, TypeError):
        return False


# Used so "unknown email" costs the same time as "wrong password".
DUMMY_HASH = hash_password("dummy-password-for-timing")


# ------------------------------------------------------------------ sessions
def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(payload_b64: str) -> str:
    return _b64(hmac.new(_secret("SESSION_SECRET"), payload_b64.encode(), hashlib.sha256).digest())


def create_session_token(customer_id: uuid.UUID) -> str:
    payload = _b64(json.dumps({"cid": str(customer_id), "exp": int(time.time()) + SESSION_TTL_SECONDS}).encode())
    return f"{SESSION_TAG}{payload}.{_sign(payload)}"


def verify_session_token(token: str) -> uuid.UUID | None:
    try:
        if not token.startswith(SESSION_TAG):
            return None
        payload, _, sig = token[len(SESSION_TAG):].partition(".")
        if not hmac.compare_digest(sig, _sign(payload)):
            return None
        data = json.loads(_unb64(payload))
        if data["exp"] < time.time():
            return None
        return uuid.UUID(data["cid"])
    except (ValueError, KeyError, TypeError):
        return None
