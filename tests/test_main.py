import asyncio
import pytest
from fastapi.testclient import TestClient

from app import main
from app import request_auth
from app.call_manager import CallStatus
from app.request_auth import _expected_token, auth_enabled


@pytest.fixture(autouse=True)
def _clear_calls():
    main.call_manager._calls.clear()
    main.agent_calls._calls.clear()
    yield
    main.call_manager._calls.clear()
    main.agent_calls._calls.clear()


def _auth_headers() -> dict[str, str]:
    headers = {"X-Habla-Device-ID": "test-device"}
    if auth_enabled():
        headers["Authorization"] = _expected_token()
    return headers


def test_health():
    client = TestClient(main.app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json() == {"service": "habla", "status": "running"}


def test_protected_route_rejects_missing_auth_when_enabled(monkeypatch):
    client = TestClient(main.app)
    monkeypatch.setattr(request_auth, "HABLA_SECRET", "test-secret")
    monkeypatch.setattr(request_auth, "HABLA_APP_BUNDLE_ID", "com.maximbilan.habla-ios")
    request_auth._expected_token.cache_clear()

    resp = client.get("/translation/languages")
    assert resp.status_code == 401


def test_create_call_creates_state_and_bridge(monkeypatch):
    client = TestClient(main.app)

    def fake_initiate_outbound_call(to_number, from_number=None, device_id=None):
        assert to_number == "+34999999999"
        assert from_number == "+12025550123"
        assert device_id == "test-device"
        return "CA123", "+12025550123"

    class DummyBridge:
        def __init__(self, call_sid, source_language, target_language, voice_gender=None):
            self.call_sid = call_sid
            self.source_language = source_language
            self.target_language = target_language
            self.voice_gender = voice_gender

    monkeypatch.setattr(main, "initiate_outbound_call", fake_initiate_outbound_call)
    monkeypatch.setattr(main, "TranslationBridge", DummyBridge)

    resp = client.post(
        "/call",
        json={
            "to": "+34999999999",
            "from": "+12025550123",
            "source_language": "fr",
            "target_language": "de-DE",
            "voice_gender": "female",
        },
        headers=_auth_headers(),
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
    assert state.bridge.voice_gender == "female"


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

    resp = client.get("/call/CA999/status", headers=_auth_headers())
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

    resp = client.get("/call/CA404/status", headers=_auth_headers())
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

    def fake_initiate_outbound_call(to_number, from_number=None, device_id=None):
        return "CA123", "+12025550123"

    monkeypatch.setattr(main, "initiate_outbound_call", fake_initiate_outbound_call)

    resp = client.post(
        "/call",
        json={
            "to": "+34999999999",
            "source_language": "xx-YY",
            "target_language": "es-US",
        },
        headers=_auth_headers(),
    )
    assert resp.status_code == 422
    assert "Unsupported source_language" in resp.json()["detail"]


def test_create_call_rejects_invalid_voice_gender(monkeypatch):
    client = TestClient(main.app)

    def fake_initiate_outbound_call(to_number, from_number=None, device_id=None):
        return "CA123", "+12025550123"

    monkeypatch.setattr(main, "initiate_outbound_call", fake_initiate_outbound_call)

    resp = client.post(
        "/call",
        json={
            "to": "+34999999999",
            "source_language": "en-US",
            "target_language": "es-US",
            "voice_gender": "robot",
        },
        headers=_auth_headers(),
    )
    assert resp.status_code == 422
    assert "voice_gender must be either 'female' or 'male'" in resp.json()["detail"]


def test_translation_languages_endpoint():
    client = TestClient(main.app)
    resp = client.get("/translation/languages", headers=_auth_headers())
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


def test_should_process_agent_media_track():
    assert main._should_process_agent_media_track(None) is True
    assert main._should_process_agent_media_track("inbound") is True
    assert main._should_process_agent_media_track("outbound") is False
    assert main._should_process_agent_media_track("outbound_track") is False


def test_end_call_hangs_up_and_cleans(monkeypatch):
    client = TestClient(main.app)
    main.call_manager.create_call("CA888", "+349999", "+1202")
    hung_up = {"called": False}

    def fake_hangup(call_sid):
        assert call_sid == "CA888"
        hung_up["called"] = True

    monkeypatch.setattr(main, "hangup_call", fake_hangup)

    resp = client.post("/call/CA888/end", headers=_auth_headers())
    assert resp.status_code == 200
    assert resp.json() == {"call_sid": "CA888", "status": "completed"}
    assert hung_up["called"] is True
    assert main.call_manager.get_call("CA888") is None


def test_create_agent_call_creates_state(monkeypatch):
    client = TestClient(main.app)

    def fake_initiate_agent_outbound_call(to_number, from_number=None, device_id=None):
        assert to_number == "+34999999999"
        assert from_number == "+12025550123"
        assert device_id == "test-device"
        return "CA_AGENT_1", "+12025550123"

    monkeypatch.setattr(main, "initiate_agent_outbound_call", fake_initiate_agent_outbound_call)

    resp = client.post(
        "/agent/call",
        json={
            "to": "+34999999999",
            "from": "+12025550123",
            "prompt": "Confirm an appointment for tomorrow.",
            "user_name": "Max",
            "language": "es",
            "voice_gender": "male",
        },
        headers=_auth_headers(),
    )
    assert resp.status_code == 200
    assert resp.json() == {"call_sid": "CA_AGENT_1", "status": "initiating"}

    manager = main.agent_calls.get("CA_AGENT_1")
    assert manager is not None
    assert manager.config.to_number == "+34999999999"
    assert manager.config.from_number == "+12025550123"
    assert manager.config.prompt == "Confirm an appointment for tomorrow."
    assert manager.config.user_name == "Max"
    assert manager.config.language == "es-US"
    assert manager.config.voice_gender == "male"


def test_create_agent_call_rejects_unsupported_language():
    client = TestClient(main.app)
    resp = client.post(
        "/agent/call",
        json={
            "to": "+34999999999",
            "prompt": "Test prompt",
            "language": "xx-YY",
        },
        headers=_auth_headers(),
    )
    assert resp.status_code == 422
    assert "Unsupported language" in resp.json()["detail"]


def test_create_agent_call_rejects_invalid_voice_gender():
    client = TestClient(main.app)
    resp = client.post(
        "/agent/call",
        json={
            "to": "+34999999999",
            "prompt": "Test prompt",
            "language": "es",
            "voice_gender": "robot",
        },
        headers=_auth_headers(),
    )
    assert resp.status_code == 422
    assert "voice_gender must be either 'female' or 'male'" in resp.json()["detail"]


def test_get_agent_call_status_returns_payload(monkeypatch):
    client = TestClient(main.app)

    class DummyManager:
        def status_payload(self):
            return {
                "call_sid": "CA_AGENT_STATUS",
                "status": "connected",
                "transcript": [],
                "quality_metrics": {},
            }

    monkeypatch.setattr(main.agent_calls, "get", lambda _: DummyManager())

    resp = client.get("/agent/call/CA_AGENT_STATUS/status", headers=_auth_headers())
    assert resp.status_code == 200
    assert resp.json() == {
        "call_sid": "CA_AGENT_STATUS",
        "status": "connected",
        "transcript": [],
        "quality_metrics": {},
    }


def test_end_agent_call_not_found():
    client = TestClient(main.app)
    resp = client.post("/agent/call/CA_UNKNOWN/end", headers=_auth_headers())
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Agent call not found"


def test_end_agent_call_invokes_manager(monkeypatch):
    client = TestClient(main.app)

    class DummyManager:
        def __init__(self):
            self.called = False

        async def end_call(self):
            self.called = True

    manager = DummyManager()
    monkeypatch.setattr(main.agent_calls, "get", lambda _: manager)

    resp = client.post("/agent/call/CA_AGENT_END/end", headers=_auth_headers())
    assert resp.status_code == 200
    assert resp.json() == {"status": "ended"}
    assert manager.called is True


def test_agent_twilio_webhook_uses_call_sid_from_form():
    client = TestClient(main.app)
    resp = client.post(
        "/agent/twilio/webhook/pending",
        data={"CallSid": "CA_FROM_TWILIO"},
    )
    assert resp.status_code == 200
    assert "/agent/twilio/media-stream/CA_FROM_TWILIO" in resp.text


def test_agent_twilio_webhook_uses_fallback_sid_when_form_missing_callsid():
    client = TestClient(main.app)
    resp = client.post(
        "/agent/twilio/webhook/CA_FALLBACK",
        data={"Foo": "bar"},
    )
    assert resp.status_code == 200
    assert "/agent/twilio/media-stream/CA_FALLBACK" in resp.text
