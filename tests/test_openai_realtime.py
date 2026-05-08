import asyncio
import base64

from app.agent.agent_bridge import AgentBridge
from app.agent.agent_openai_session import AgentOpenAIRealtimeSession
from app.openai_realtime import OpenAIRealtimeTranslationSession


async def _noop_audio(_: bytes) -> None:
    return None


async def _noop_transcript(_: str, __: str) -> None:
    return None


async def _noop_status(_: str) -> None:
    return None


def test_translation_session_update_uses_target_base_language():
    session = OpenAIRealtimeTranslationSession(
        session_id="CA123-a",
        target_language="es-US",
    )

    assert session._session_update_event() == {
        "type": "session.update",
        "session": {"audio": {"output": {"language": "es"}}},
    }


def test_translation_audio_append_uses_translation_event_type():
    session = OpenAIRealtimeTranslationSession(
        session_id="CA123-a",
        target_language="de-DE",
    )

    event = session._audio_append_event(b"\x01\x02")

    assert event["type"] == "session.input_audio_buffer.append"
    assert base64.b64decode(event["audio"]) == b"\x01\x02"


def test_agent_session_update_configures_audio_and_transcription():
    session = AgentOpenAIRealtimeSession(
        session_id="agent-CA123",
        system_prompt="Call the restaurant.",
        callee_language="fr-FR",
        on_audio_output=_noop_audio,
        on_transcript=_noop_transcript,
        on_agent_status=_noop_status,
    )

    payload = session._session_update_event()
    config = payload["session"]

    assert payload["type"] == "session.update"
    assert config["model"] == "gpt-realtime-2"
    assert config["output_modalities"] == ["audio"]
    assert config["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 24000}
    assert config["audio"]["input"]["transcription"]["language"] == "fr"
    assert config["audio"]["output"]["format"] == {"type": "audio/pcm", "rate": 24000}
    assert config["audio"]["output"]["voice"] == "cedar"


def test_agent_instruction_creates_text_item_and_response():
    session = AgentOpenAIRealtimeSession(
        session_id="agent-CA123",
        system_prompt="Call the restaurant.",
        callee_language="es-US",
        on_audio_output=_noop_audio,
        on_transcript=_noop_transcript,
        on_agent_status=_noop_status,
    )

    item = session._text_item_event("Say hello.")
    response = session._response_create_event()

    assert item["type"] == "conversation.item.create"
    assert item["item"]["role"] == "user"
    assert item["item"]["content"][0]["type"] == "input_text"
    assert "Say hello." in item["item"]["content"][0]["text"]
    assert response == {
        "type": "response.create",
        "response": {"output_modalities": ["audio"]},
    }


def test_agent_bridge_resamples_twilio_payload_to_openai_pcm24():
    bridge = AgentBridge("CA123")
    sent: list[bytes] = []
    payload = base64.b64encode(b"\xff" * 160).decode("ascii")

    async def capture(audio: bytes) -> None:
        sent.append(audio)

    asyncio.run(bridge.forward_twilio_media_to_nova(payload, capture))

    assert len(sent) == 1
    assert 900 <= len(sent[0]) <= 960


def test_agent_session_emits_audio_and_transcripts_from_events():
    audio: list[bytes] = []
    transcripts: list[tuple[str, str]] = []
    statuses: list[str] = []

    async def capture_audio(chunk: bytes) -> None:
        audio.append(chunk)

    async def capture_transcript(role: str, text: str) -> None:
        transcripts.append((role, text))

    async def capture_status(status: str) -> None:
        statuses.append(status)

    session = AgentOpenAIRealtimeSession(
        session_id="agent-CA123",
        system_prompt="Call the restaurant.",
        callee_language="es-US",
        on_audio_output=capture_audio,
        on_transcript=capture_transcript,
        on_agent_status=capture_status,
    )

    async def run() -> None:
        await session._handle_event(
            {
                "type": "response.output_audio.delta",
                "delta": base64.b64encode(b"audio").decode("ascii"),
            }
        )
        await session._handle_event(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": "Hola",
            }
        )
        await session._handle_event(
            {
                "type": "response.output_audio_transcript.done",
                "transcript": "Buenos dias",
            }
        )

    asyncio.run(run())

    assert audio == [b"audio"]
    assert transcripts == [("callee", "Hola"), ("agent", "Buenos dias")]
    assert statuses == ["speaking"]


def test_agent_session_queues_instruction_until_active_response_finishes():
    session = AgentOpenAIRealtimeSession(
        session_id="agent-CA123",
        system_prompt="Call the restaurant.",
        callee_language="es-US",
        on_audio_output=_noop_audio,
        on_transcript=_noop_transcript,
        on_agent_status=_noop_status,
    )
    session.is_active = True
    session._response_active = True
    sent_events: list[dict] = []

    async def capture_send(event: dict) -> None:
        sent_events.append(event)

    session._send = capture_send  # type: ignore[method-assign]

    async def run() -> None:
        await session.inject_instruction("Say hello.")
        assert sent_events == []
        await session._handle_event({"type": "response.done"})

    asyncio.run(run())

    assert [event["type"] for event in sent_events] == [
        "conversation.item.create",
        "response.create",
    ]
    assert session._response_active is True
