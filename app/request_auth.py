"""
Simple request authentication for iOS-originated backend traffic.

Token format:
    HMAC-SHA256(HABLA_SECRET, HABLA_APP_BUNDLE_ID)  -> hex digest
"""

from __future__ import annotations

import hashlib
import hmac
from functools import lru_cache

from fastapi import Header, HTTPException, WebSocket, WebSocketException, status

from app.config import HABLA_APP_BUNDLE_ID, HABLA_SECRET


def auth_enabled() -> bool:
    return bool(HABLA_SECRET)


def _normalize_authorization_token(authorization: str | None) -> str:
    if not authorization:
        return ""

    value = authorization.strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    return value


@lru_cache(maxsize=1)
def _expected_token() -> str:
    if not auth_enabled():
        return ""

    mac = hmac.new(
        HABLA_SECRET.encode("utf-8"),
        HABLA_APP_BUNDLE_ID.encode("utf-8"),
        hashlib.sha256,
    )
    return mac.hexdigest()


def is_authorized(authorization: str | None) -> bool:
    if not auth_enabled():
        return True

    provided = _normalize_authorization_token(authorization)
    expected = _expected_token()
    return bool(provided) and hmac.compare_digest(provided, expected)


def require_authorized_request(authorization: str | None = Header(default=None)) -> None:
    if not is_authorized(authorization):
        raise HTTPException(status_code=401, detail="unauthorized")


def require_authorized_websocket(ws: WebSocket) -> None:
    if is_authorized(ws.headers.get("authorization")):
        return
    raise WebSocketException(
        code=status.WS_1008_POLICY_VIOLATION,
        reason="unauthorized",
    )
