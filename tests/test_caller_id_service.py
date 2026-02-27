import pytest
from fastapi import HTTPException

from app.caller_id import service


class _DummyOutgoingCallerIds:
    def __init__(self, sid_by_number: dict[str, str]):
        self._sid_by_number = sid_by_number

    def list(self, phone_number: str):
        sid = self._sid_by_number.get(phone_number)
        if not sid:
            return []
        return [type("CallerId", (), {"sid": sid})()]


class _DummyClient:
    def __init__(self, sid_by_number: dict[str, str]):
        self.outgoing_caller_ids = _DummyOutgoingCallerIds(sid_by_number)


def test_resolve_outbound_uses_default_twilio_number(monkeypatch):
    monkeypatch.setattr(service, "TWILIO_FROM_NUMBER", "+19995550123")
    client = _DummyClient({})
    assert service.resolve_outbound_caller_id(client, None) == "+19995550123"


def test_resolve_outbound_requires_device_id_for_custom_number():
    client = _DummyClient({"+12025550123": "PN123"})
    with pytest.raises(HTTPException) as exc:
        service.resolve_outbound_caller_id(client, "+12025550123", None)
    assert exc.value.status_code == 400


def test_resolve_outbound_rejects_other_device(monkeypatch):
    monkeypatch.setattr(
        service,
        "get_claim",
        lambda _: type("Claim", (), {"device_id": "device-a"})(),
    )
    client = _DummyClient({"+12025550123": "PN123"})

    with pytest.raises(HTTPException) as exc:
        service.resolve_outbound_caller_id(
            client,
            "+12025550123",
            "device-b",
        )
    assert exc.value.status_code == 400


def test_resolve_outbound_accepts_owner_device(monkeypatch):
    monkeypatch.setattr(
        service,
        "get_claim",
        lambda _: type("Claim", (), {"device_id": "device-a"})(),
    )
    client = _DummyClient({"+12025550123": "PN123"})

    result = service.resolve_outbound_caller_id(
        client,
        "+12025550123",
        "device-a",
    )
    assert result == "+12025550123"
