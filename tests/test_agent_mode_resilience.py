import asyncio

from app.agent.agent_call_manager import (
    AgentCallConfig,
    AgentCallManager,
    MAX_NOVA_RESTART_ATTEMPTS,
    initiate_agent_outbound_call,
)
import app.agent.agent_call_manager as agent_call_manager
from app.agent.agent_nova_session import AgentNovaSession


async def _noop_audio(_: bytes) -> None:
    return None


async def _noop_transcript(_: str, __: str) -> None:
    return None


async def _noop_status(_: str) -> None:
    return None


class FakeActiveNovaSession:
    def __init__(self) -> None:
        self.is_active = True
        self.instructions: list[str] = []
        self.audio_chunks: list[bytes] = []

    async def inject_instruction(self, instruction_text: str) -> None:
        self.instructions.append(instruction_text)

    async def send_audio(self, pcm_audio: bytes) -> None:
        self.audio_chunks.append(pcm_audio)


class FakeBridge:
    def __init__(self) -> None:
        self.forwarded_payloads: list[str] = []

    async def forward_twilio_media_to_nova(self, payload: str, send_audio_cb) -> None:
        self.forwarded_payloads.append(payload)


class CaptureAgentNovaSession(AgentNovaSession):
    def __init__(self) -> None:
        super().__init__(
            session_id="test-session",
            system_prompt="test",
            voice_id="lupe",
            on_audio_output=_noop_audio,
            on_transcript=_noop_transcript,
            on_agent_status=_noop_status,
        )
        self.sent_events: list[dict] = []
        self.is_active = True
        self.prompt_name = "test-prompt"

    async def _send(self, event: dict) -> None:
        self.sent_events.append(event)


def test_inject_instruction_includes_text_input_configuration():
    session = CaptureAgentNovaSession()
    asyncio.run(session.inject_instruction("Hola"))

    assert len(session.sent_events) == 3
    content_start = session.sent_events[0]["event"]["contentStart"]
    assert content_start["type"] == "TEXT"
    assert content_start["textInputConfiguration"]["mediaType"] == "text/plain"


def test_ensure_nova_session_throttles_rapid_restarts():
    manager = AgentCallManager(
        call_sid="CA_TEST",
        config=AgentCallConfig(
            to_number="+12025550100",
            from_number=None,
            prompt="Test",
            user_name="Tester",
            language="es",
        ),
    )

    async def fake_start() -> None:
        return None

    manager.start_nova_session = fake_start  # type: ignore[method-assign]

    first = asyncio.run(manager.ensure_nova_session())
    second = asyncio.run(manager.ensure_nova_session())

    assert first is True
    assert second is False


def test_ensure_nova_session_fails_after_restart_limit():
    manager = AgentCallManager(
        call_sid="CA_TEST",
        config=AgentCallConfig(
            to_number="+12025550100",
            from_number=None,
            prompt="Test",
            user_name="Tester",
            language="es",
        ),
    )
    manager._nova_restart_attempts = MAX_NOVA_RESTART_ATTEMPTS

    result = asyncio.run(manager.ensure_nova_session())

    assert result is False
    assert manager.status == "failed"


def test_handle_transcript_injects_listen_first_guidance():
    manager = AgentCallManager(
        call_sid="CA_LISTEN",
        config=AgentCallConfig(
            to_number="+12025550100",
            from_number=None,
            prompt="Test",
            user_name="Tester",
            language="es",
        ),
    )
    session = FakeActiveNovaSession()
    manager.nova_session = session  # type: ignore[assignment]

    async def fake_translate(source_text: str) -> str:
        return source_text

    manager.transcript.translate_to_english = fake_translate  # type: ignore[method-assign]

    asyncio.run(manager.handle_transcript("callee", "Necesito confirmar el horario de hoy."))

    assert any("acknowledge that point first" in instruction for instruction in session.instructions)
    assert manager.status_payload()["quality_metrics"]["listen_first_guidance"] == 1


def test_handle_transcript_triggers_repetition_guard():
    manager = AgentCallManager(
        call_sid="CA_REPEAT",
        config=AgentCallConfig(
            to_number="+12025550100",
            from_number=None,
            prompt="Test",
            user_name="Tester",
            language="es",
        ),
    )
    session = FakeActiveNovaSession()
    manager.nova_session = session  # type: ignore[assignment]

    async def fake_translate(source_text: str) -> str:
        return source_text

    manager.transcript.translate_to_english = fake_translate  # type: ignore[method-assign]

    async def _run() -> None:
        await manager.handle_transcript("agent", "Claro, le puedo ayudar con eso ahora.")
        await manager.handle_transcript("agent", "Claro, le puedo ayudar con eso ahora mismo.")

    asyncio.run(_run())

    assert any("repeating prior phrasing" in instruction for instruction in session.instructions)
    assert manager.status_payload()["quality_metrics"]["repeat_guard_triggers"] == 1


def test_handle_twilio_media_forwards_payloads():
    manager = AgentCallManager(
        call_sid="CA_BARGE",
        config=AgentCallConfig(
            to_number="+12025550100",
            from_number=None,
            prompt="Test",
            user_name="Tester",
            language="es",
        ),
    )
    session = FakeActiveNovaSession()
    manager.nova_session = session  # type: ignore[assignment]

    bridge = FakeBridge()
    manager.bridge = bridge  # type: ignore[assignment]

    async def _run() -> None:
        await manager.handle_twilio_media("first")
        await manager.handle_twilio_media("second")

    asyncio.run(_run())

    assert bridge.forwarded_payloads == ["first", "second"]
    assert session.instructions == []
    assert set(manager.status_payload()["quality_metrics"].keys()) == {
        "callee_turns",
        "agent_turns",
        "avg_agent_words_per_turn",
        "repeat_guard_triggers",
        "listen_first_guidance",
    }


def test_is_control_transcript_payload_detection():
    manager = AgentCallManager(
        call_sid="CA_CTRL",
        config=AgentCallConfig(
            to_number="+12025550100",
            from_number=None,
            prompt="Test",
            user_name="Tester",
            language="es",
        ),
    )

    assert manager._is_control_transcript_payload("[Additional instruction from caller]: short update")
    assert manager._is_control_transcript_payload('{"type":"status","reason":"done"}')
    assert manager._is_control_transcript_payload('{"event":"interrupt","ok":true}')
    assert not manager._is_control_transcript_payload("Necesito confirmar una cita.")
    assert not manager._is_control_transcript_payload('{"message":"normal content","count":5,"other":"x","extra":"y"}')


def test_initiate_agent_outbound_call_rejects_blank_public_url(monkeypatch):
    monkeypatch.setattr(agent_call_manager, "PUBLIC_URL", "")

    async def _unused(*args, **kwargs):
        raise AssertionError("should not create Twilio call")

    monkeypatch.setattr(agent_call_manager, "create_outbound_call", _unused)

    try:
        initiate_agent_outbound_call("+12025550100")
    except RuntimeError as exc:
        assert "PUBLIC_URL" in str(exc)
    else:
        raise AssertionError("expected PUBLIC_URL validation error")


def test_should_auto_end_after_agent_turn_detects_closing_phrases():
    manager = AgentCallManager(
        call_sid="CA_END",
        config=AgentCallConfig(
            to_number="+12025550100",
            from_number=None,
            prompt="Test",
            user_name="Tester",
            language="es",
        ),
    )

    assert manager._should_auto_end_after_agent_turn("Muchas gracias por su tiempo, adios.")
    assert not manager._should_auto_end_after_agent_turn("¿Puede confirmar la dirección?")
    assert not manager._should_auto_end_after_agent_turn("Necesito confirmar un detalle más.")
