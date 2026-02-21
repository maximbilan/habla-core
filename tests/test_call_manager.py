import asyncio

from app.call_manager import CallManager, CallStatus


class DummyBridge:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class DummyWS:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


def test_create_call_and_get_call():
    manager = CallManager()
    created = manager.create_call("CA1", "+34999", "+1202")
    fetched = manager.get_call("CA1")

    assert fetched is created


def test_cleanup_call_closes_resources_and_removes_state():
    manager = CallManager()
    state = manager.create_call("CA3", "+34997", "+1204")
    state.bridge = DummyBridge()
    state.ios_ws = DummyWS()
    state.twilio_ws = DummyWS()

    asyncio.run(manager.cleanup_call("CA3"))

    assert state.status == CallStatus.COMPLETED
    assert state.bridge.closed is True
    assert state.ios_ws.closed is True
    assert state.twilio_ws.closed is True
    assert manager.get_call("CA3") is None


def test_cleanup_call_is_idempotent():
    manager = CallManager()
    manager.create_call("CA4", "+34996", "+1205")

    asyncio.run(manager.cleanup_call("CA4"))
    asyncio.run(manager.cleanup_call("CA4"))

    assert manager.get_call("CA4") is None
