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
        def __init__(self, call_sid):
            self.call_sid = call_sid

    monkeypatch.setattr(main, "initiate_outbound_call", fake_initiate_outbound_call)
    monkeypatch.setattr(main, "TranslationBridge", DummyBridge)

    resp = client.post(
        "/call",
        json={"to": "+34999999999", "from": "+12025550123"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"call_sid": "CA123", "status": "initiating"}

    state = main.call_manager.get_call("CA123")
    assert state is not None
    assert state.to_number == "+34999999999"
    assert state.from_number == "+12025550123"
    assert isinstance(state.bridge, DummyBridge)
    assert state.bridge.call_sid == "CA123"


def test_get_call_status_uses_cached_state():
    client = TestClient(main.app)
    state = main.call_manager.create_call("CA999", "+349999", "+1202")
    state.status = CallStatus.IN_PROGRESS

    resp = client.get("/call/CA999/status")
    assert resp.status_code == 200
    assert resp.json() == {
        "call_sid": "CA999",
        "status": "in_progress",
        "to": "+349999",
        "from_": "+1202",
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
    }


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
