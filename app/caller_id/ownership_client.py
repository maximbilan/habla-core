from __future__ import annotations

import json
from dataclasses import dataclass
from urllib import error, request

from app.config import (
    HABLA_ACCOUNTS_BASE_URL,
    HABLA_ACCOUNTS_SERVICE_TOKEN,
    HABLA_ACCOUNTS_TIMEOUT_SECONDS,
)

SERVICE_TOKEN_HEADER = "X-Habla-Service-Token"
DEVICE_ID_HEADER = "X-Habla-Device-ID"


class OwnershipServiceError(RuntimeError):
    pass


@dataclass(frozen=True)
class CallerIdClaim:
    sid: str
    device_id: str
    phone_number: str

    @staticmethod
    def from_payload(payload: dict) -> "CallerIdClaim":
        return CallerIdClaim(
            sid=str(payload.get("sid", "")),
            device_id=str(payload.get("device_id", "")),
            phone_number=str(payload.get("phone_number", "")),
        )


def claim_sid(sid: str, phone_number: str, device_id: str) -> bool:
    payload = {"phone_number": phone_number}
    try:
        _request_json(
            method="PUT",
            path=f"/v1/caller-id/claims/{sid}",
            device_id=device_id,
            payload=payload,
            accepted_statuses={200},
        )
        return True
    except _HTTPStatusError as exc:
        if exc.status_code == 409:
            return False
        raise


def get_claim(sid: str) -> CallerIdClaim | None:
    try:
        payload = _request_json(
            method="GET",
            path=f"/v1/caller-id/claims/{sid}",
            accepted_statuses={200},
        )
    except _HTTPStatusError as exc:
        if exc.status_code == 404:
            return None
        raise

    claim_payload = payload.get("claim", {})
    if not isinstance(claim_payload, dict):
        return None
    return CallerIdClaim.from_payload(claim_payload)


def list_claimed_sids(device_id: str) -> set[str]:
    payload = _request_json(
        method="GET",
        path="/v1/caller-id/claims",
        device_id=device_id,
        accepted_statuses={200},
    )
    claims = payload.get("claims", [])
    if not isinstance(claims, list):
        return set()
    return {str(item.get("sid")) for item in claims if isinstance(item, dict) and item.get("sid")}


def delete_claim(sid: str, device_id: str) -> bool:
    try:
        _request_json(
            method="DELETE",
            path=f"/v1/caller-id/claims/{sid}",
            device_id=device_id,
            accepted_statuses={200},
        )
        return True
    except _HTTPStatusError as exc:
        if exc.status_code == 404:
            return False
        raise


def _request_json(
    *,
    method: str,
    path: str,
    accepted_statuses: set[int],
    device_id: str | None = None,
    payload: dict | None = None,
) -> dict:
    if not HABLA_ACCOUNTS_BASE_URL:
        raise OwnershipServiceError("HABLA_ACCOUNTS_BASE_URL is not configured")
    if not HABLA_ACCOUNTS_SERVICE_TOKEN:
        raise OwnershipServiceError("HABLA_ACCOUNTS_SERVICE_TOKEN is not configured")

    url = _build_url(path)
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")

    req = request.Request(url=url, data=body, method=method)
    req.add_header(SERVICE_TOKEN_HEADER, HABLA_ACCOUNTS_SERVICE_TOKEN)
    req.add_header("Content-Type", "application/json")
    if device_id:
        req.add_header(DEVICE_ID_HEADER, device_id)

    try:
        with request.urlopen(req, timeout=HABLA_ACCOUNTS_TIMEOUT_SECONDS) as resp:
            status_code = getattr(resp, "status", 200)
            raw = resp.read().decode("utf-8").strip()
    except error.HTTPError as exc:
        detail = _http_error_detail(exc)
        raise _HTTPStatusError(exc.code, detail) from exc
    except error.URLError as exc:
        raise OwnershipServiceError(f"Ownership service unavailable: {exc.reason}") from exc

    if status_code not in accepted_statuses:
        raise OwnershipServiceError(f"Unexpected ownership service status: {status_code}")

    if not raw:
        return {}

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise OwnershipServiceError("Ownership service returned invalid JSON") from exc

    if not isinstance(parsed, dict):
        raise OwnershipServiceError("Ownership service returned invalid payload")
    return parsed


def _build_url(path: str) -> str:
    base = HABLA_ACCOUNTS_BASE_URL.rstrip("/")
    suffix = path if path.startswith("/") else f"/{path}"
    return f"{base}{suffix}"


def _http_error_detail(exc: error.HTTPError) -> str:
    try:
        raw = exc.read().decode("utf-8")
        payload = json.loads(raw) if raw else {}
        if isinstance(payload, dict) and payload.get("detail"):
            return str(payload["detail"])
    except Exception:
        pass
    return f"HTTP {exc.code}"


class _HTTPStatusError(OwnershipServiceError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
