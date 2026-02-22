import hashlib
import hmac

import pytest
from fastapi import HTTPException, WebSocketException, status

from app import request_auth


def _token(secret: str, bundle_id: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        bundle_id.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


@pytest.fixture(autouse=True)
def _clear_expected_token_cache():
    request_auth._expected_token.cache_clear()
    yield
    request_auth._expected_token.cache_clear()


def test_is_authorized_disabled_when_secret_missing(monkeypatch):
    monkeypatch.setattr(request_auth, "HABLA_SECRET", "")
    monkeypatch.setattr(request_auth, "HABLA_APP_BUNDLE_ID", "com.maximbilan.habla-ios")

    assert request_auth.auth_enabled() is False
    assert request_auth.is_authorized(None) is True
    assert request_auth.is_authorized("anything") is True


def test_is_authorized_accepts_raw_token(monkeypatch):
    secret = "super-secret"
    bundle_id = "com.maximbilan.habla-ios"
    expected = _token(secret, bundle_id)

    monkeypatch.setattr(request_auth, "HABLA_SECRET", secret)
    monkeypatch.setattr(request_auth, "HABLA_APP_BUNDLE_ID", bundle_id)

    assert request_auth.auth_enabled() is True
    assert request_auth.is_authorized(expected) is True
    assert request_auth.is_authorized("wrong") is False


def test_is_authorized_accepts_bearer_token(monkeypatch):
    secret = "another-secret"
    bundle_id = "com.maximbilan.habla-ios"
    expected = _token(secret, bundle_id)

    monkeypatch.setattr(request_auth, "HABLA_SECRET", secret)
    monkeypatch.setattr(request_auth, "HABLA_APP_BUNDLE_ID", bundle_id)

    assert request_auth.is_authorized(f"Bearer {expected}") is True


def test_require_authorized_request_raises_on_invalid_token(monkeypatch):
    monkeypatch.setattr(request_auth, "HABLA_SECRET", "required-secret")
    monkeypatch.setattr(request_auth, "HABLA_APP_BUNDLE_ID", "com.maximbilan.habla-ios")

    with pytest.raises(HTTPException) as exc:
        request_auth.require_authorized_request("bad-token")
    assert exc.value.status_code == 401


def test_normalize_authorization_token_handles_bearer_prefix():
    assert request_auth._normalize_authorization_token("  Bearer   abc123  ") == "abc123"


def test_require_authorized_websocket_accepts_valid_token(monkeypatch):
    secret = "required-secret"
    bundle_id = "com.maximbilan.habla-ios"
    token = _token(secret, bundle_id)

    monkeypatch.setattr(request_auth, "HABLA_SECRET", secret)
    monkeypatch.setattr(request_auth, "HABLA_APP_BUNDLE_ID", bundle_id)

    class DummyWS:
        headers = {"authorization": f"Bearer {token}"}

    request_auth.require_authorized_websocket(DummyWS())


def test_require_authorized_websocket_rejects_invalid_token(monkeypatch):
    monkeypatch.setattr(request_auth, "HABLA_SECRET", "required-secret")
    monkeypatch.setattr(request_auth, "HABLA_APP_BUNDLE_ID", "com.maximbilan.habla-ios")

    class DummyWS:
        headers = {"authorization": "bad-token"}

    with pytest.raises(WebSocketException) as exc:
        request_auth.require_authorized_websocket(DummyWS())

    assert exc.value.code == status.WS_1008_POLICY_VIOLATION
