import time
import uuid

import security


def test_generate_api_key_shape():
    raw, prefix, h = security.generate_api_key()
    assert raw.startswith("pme_") and prefix == raw[:12]
    assert len(h) == 64 and h == security.hash_api_key(raw)
    assert raw not in h


def test_keys_are_unique():
    assert len({security.generate_api_key()[0] for _ in range(50)}) == 50


def test_hash_depends_on_pepper(monkeypatch):
    raw, _, h = security.generate_api_key()
    monkeypatch.setenv("KEY_PEPPER", "another-pepper-" + "x" * 40)
    assert security.hash_api_key(raw) != h


def test_missing_secret_fails_loudly(monkeypatch):
    monkeypatch.setenv("KEY_PEPPER", "short")
    try:
        security.check_config()
        assert False, "should have raised"
    except RuntimeError:
        pass


def test_password_hashing():
    h = security.hash_password("correct-horse-1")
    assert "correct-horse-1" not in h
    assert security.verify_password("correct-horse-1", h)
    assert not security.verify_password("wrong-password", h)
    assert not security.verify_password("x", "garbage")


def test_session_token_roundtrip_and_tamper():
    cid = uuid.uuid4()
    t = security.create_session_token(cid)
    assert security.verify_session_token(t) == cid
    assert security.verify_session_token(t[:-2] + "xx") is None
    assert security.verify_session_token("pmes.abc.def") is None
    assert security.verify_session_token("pme_notasession") is None


def test_session_token_expires(monkeypatch):
    t = security.create_session_token(uuid.uuid4())
    monkeypatch.setattr(time, "time", lambda: 9_999_999_999)
    assert security.verify_session_token(t) is None


def test_parse_bearer():
    assert security.parse_bearer("Bearer abc") == "abc"
    assert security.parse_bearer("bearer abc") == "abc"
    assert security.parse_bearer("Basic abc") is None
    assert security.parse_bearer("Bearer ") is None
    assert security.parse_bearer(None) is None
