import asyncio
import pytest
from fastapi.testclient import TestClient

from app import main
from app.call_manager import CallStatus


@pytest.fixture(autouse=True)
def _clear_calls():
    main.call_manager._calls.clear()
    yield
    main.call_manager._calls.clear()


def test_health():
    client = TestClient(main.app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json() == {"service": "habla", "status": "running"}


def test_create_call_creates_state_and_bridge(monkeypatch):
    client = TestClient(main.app)

    def fake_initiate_outbound_call(to_number, from_number=None):
        assert to_number == "+34999999999"
        assert from_number == "+12025550123"
        return "CA123", "+12025550123"

    class DummyBridge:
        def __init__(self, call_sid, source_language, target_language):
            self.call_sid = call_sid
            self.source_language = source_language
            self.target_language = target_language

    monkeypatch.setattr(main, "initiate_outbound_call", fake_initiate_outbound_call)
    monkeypatch.setattr(main, "TranslationBridge", DummyBridge)

    resp = client.post(
        "/call",
        json={
            "to": "+34999999999",
            "from": "+12025550123",
            "source_language": "fr",
            "target_language": "de-DE",
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"call_sid": "CA123", "status": "initiating"}

    state = main.call_manager.get_call("CA123")
    assert state is not None
    assert state.to_number == "+34999999999"
    assert state.from_number == "+12025550123"
    assert state.source_language == "fr-FR"
    assert state.target_language == "de-DE"
    assert isinstance(state.bridge, DummyBridge)
    assert state.bridge.call_sid == "CA123"
    assert state.bridge.source_language == "fr-FR"
    assert state.bridge.target_language == "de-DE"


def test_get_call_status_uses_cached_state():
    client = TestClient(main.app)
    state = main.call_manager.create_call(
        "CA999",
        "+349999",
        "+1202",
        source_language="en-US",
        target_language="es-US",
    )
    state.status = CallStatus.IN_PROGRESS

    resp = client.get("/call/CA999/status")
    assert resp.status_code == 200
    assert resp.json() == {
        "call_sid": "CA999",
        "status": "in_progress",
        "to": "+349999",
        "from_": "+1202",
        "source_language": "en-US",
        "target_language": "es-US",
    }


def test_get_call_status_falls_back_to_twilio(monkeypatch):
    client = TestClient(main.app)

    def fake_fetch_call_status(call_sid):
        assert call_sid == "CA404"
        return {
            "call_sid": "CA404",
            "status": "ringing",
            "to": "+349999",
            "from_": "+1202",
        }

    monkeypatch.setattr(main, "fetch_call_status", fake_fetch_call_status)

    resp = client.get("/call/CA404/status")
    assert resp.status_code == 200
    assert resp.json() == {
        "call_sid": "CA404",
        "status": "ringing",
        "to": "+349999",
        "from_": "+1202",
        "source_language": "en-US",
        "target_language": "es-US",
    }


def test_create_call_rejects_unsupported_language(monkeypatch):
    client = TestClient(main.app)

    def fake_initiate_outbound_call(to_number, from_number=None):
        return "CA123", "+12025550123"

    monkeypatch.setattr(main, "initiate_outbound_call", fake_initiate_outbound_call)

    resp = client.post(
        "/call",
        json={
            "to": "+34999999999",
            "source_language": "xx-YY",
            "target_language": "es-US",
        },
    )
    assert resp.status_code == 422
    assert "Unsupported source_language" in resp.json()["detail"]


def test_translation_languages_endpoint():
    client = TestClient(main.app)
    resp = client.get("/translation/languages")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["default_source_language"] == "en-US"
    assert payload["default_target_language"] == "es-US"
    assert any(item["code"] == "en-US" for item in payload["supported_languages"])


def test_extract_form_field_from_urlencoded_body():
    body = b"CallSid=CA12345&Direction=outbound-api"
    value = main._extract_form_field_from_urlencoded_body(body, "CallSid")
    assert value == "CA12345"


def test_extract_twilio_call_sid_from_urlencoded_request_body():
    class DummyRequest:
        def __init__(self):
            self.headers = {"content-type": "application/x-www-form-urlencoded"}

        async def body(self):
            return b"CallSid=CA999&Foo=bar"

        async def form(self):
            raise AssertionError("form() should not be called for urlencoded bodies")

    sid = asyncio.run(main._extract_twilio_call_sid(DummyRequest(), "pending"))
    assert sid == "CA999"


def test_extract_twilio_call_sid_falls_back_when_form_unavailable():
    class DummyRequest:
        def __init__(self):
            self.headers = {"content-type": "multipart/form-data"}

        async def body(self):
            return b""

        async def form(self):
            raise AssertionError("python-multipart not installed")

    sid = asyncio.run(main._extract_twilio_call_sid(DummyRequest(), "pending"))
    assert sid == "pending"


def test_end_call_hangs_up_and_cleans(monkeypatch):
    client = TestClient(main.app)
    main.call_manager.create_call("CA888", "+349999", "+1202")
    hung_up = {"called": False}

    def fake_hangup(call_sid):
        assert call_sid == "CA888"
        hung_up["called"] = True

    monkeypatch.setattr(main, "hangup_call", fake_hangup)

    resp = client.post("/call/CA888/end")
    assert resp.status_code == 200
    assert resp.json() == {"call_sid": "CA888", "status": "completed"}
    assert hung_up["called"] is True
    assert main.call_manager.get_call("CA888") is None
