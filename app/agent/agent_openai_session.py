"""Single OpenAI Realtime session used by Agent Mode."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections import defaultdict
from typing import Awaitable, Callable
from urllib.parse import quote

import websockets

from app.config import (
    OPENAI_API_KEY,
    OPENAI_AUDIO_SAMPLE_RATE,
    OPENAI_REALTIME_AGENT_MODEL,
    OPENAI_REALTIME_AGENT_TRANSCRIPTION_MODEL,
    OPENAI_REALTIME_AGENT_VOICE,
)
from app.language_support import openai_language_code

logger = logging.getLogger(__name__)

OPENAI_REALTIME_URL = "wss://api.openai.com/v1/realtime"


def _json_dumps(payload: dict) -> str:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=True)


def _require_openai_api_key() -> str:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is required for OpenAI Realtime sessions")
    return OPENAI_API_KEY


class AgentOpenAIRealtimeSession:
    """Manages one bidirectional OpenAI Realtime stream for autonomous calls."""

    def __init__(
        self,
        session_id: str,
        system_prompt: str,
        callee_language: str,
        on_audio_output: Callable[[bytes], Awaitable[None]],
        on_transcript: Callable[[str, str], Awaitable[None]],
        on_agent_status: Callable[[str], Awaitable[None]],
        model: str = OPENAI_REALTIME_AGENT_MODEL,
        voice: str = OPENAI_REALTIME_AGENT_VOICE,
        input_sample_rate: int = OPENAI_AUDIO_SAMPLE_RATE,
        output_sample_rate: int = OPENAI_AUDIO_SAMPLE_RATE,
    ) -> None:
        self.session_id = session_id
        self.system_prompt = system_prompt
        self.callee_language = openai_language_code(callee_language)
        self.model = model
        self.voice = voice
        self.input_sample_rate = input_sample_rate
        self.output_sample_rate = output_sample_rate

        self._on_audio_output = on_audio_output
        self._on_transcript = on_transcript
        self._on_agent_status = on_agent_status

        self._ws = None
        self.is_active = False
        self._response_task: asyncio.Task | None = None
        self._instruction_lock = asyncio.Lock()
        self._response_active = False
        self._pending_instructions: list[str] = []
        self._agent_transcript_fragments: dict[str, list[str]] = defaultdict(list)
        self._agent_text_fragments: dict[str, list[str]] = defaultdict(list)

    async def start(self) -> None:
        api_key = _require_openai_api_key()
        url = f"{OPENAI_REALTIME_URL}?model={quote(self.model, safe='')}"
        self._ws = await websockets.connect(
            url,
            additional_headers={
                "Authorization": f"Bearer {api_key}",
                "OpenAI-Safety-Identifier": self.session_id,
            },
        )
        self.is_active = True
        await self._send(self._session_update_event())
        self._response_task = asyncio.create_task(
            self._process_responses(),
            name=f"openai-agent-rx-{self.session_id}",
        )
        logger.info(
            "Agent OpenAI Realtime session started: %s (model=%s voice=%s)",
            self.session_id,
            self.model,
            self.voice,
        )

    def _session_update_event(self) -> dict:
        return {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "model": self.model,
                "instructions": self.system_prompt,
                "output_modalities": ["audio"],
                "audio": {
                    "input": {
                        "format": {
                            "type": "audio/pcm",
                            "rate": self.input_sample_rate,
                        },
                        "transcription": {
                            "model": OPENAI_REALTIME_AGENT_TRANSCRIPTION_MODEL,
                            "language": self.callee_language,
                        },
                        "turn_detection": {
                            "type": "server_vad",
                            "create_response": True,
                            "interrupt_response": True,
                            "prefix_padding_ms": 300,
                            "silence_duration_ms": 500,
                        },
                    },
                    "output": {
                        "format": {
                            "type": "audio/pcm",
                            "rate": self.output_sample_rate,
                        },
                        "voice": self.voice,
                    },
                },
            },
        }

    def _audio_append_event(self, pcm_audio: bytes) -> dict:
        return {
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(pcm_audio).decode("ascii"),
        }

    def _text_item_event(self, instruction_text: str) -> dict:
        return {
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": f"[Additional instruction from caller]: {instruction_text}",
                    }
                ],
            },
        }

    def _response_create_event(self) -> dict:
        return {
            "type": "response.create",
            "response": {
                "output_modalities": ["audio"],
            },
        }

    async def _send(self, event: dict) -> None:
        if not self._ws:
            return
        await self._ws.send(_json_dumps(event))

    async def send_audio(self, pcm_audio: bytes) -> None:
        if not self.is_active:
            return
        await self._send(self._audio_append_event(pcm_audio))

    async def inject_instruction(self, instruction_text: str) -> None:
        if not self.is_active:
            return

        async with self._instruction_lock:
            self._pending_instructions.append(instruction_text)
            await self._dispatch_pending_instructions_locked()

    async def _dispatch_pending_instructions_locked(self) -> None:
        if not self.is_active:
            return
        if self._response_active:
            return

        while self._pending_instructions:
            instruction_text = self._pending_instructions.pop(0)
            await self._send(self._text_item_event(instruction_text))

        if self._response_active or not self.is_active:
            return

        await self._send(self._response_create_event())
        self._response_active = True

    async def _process_responses(self) -> None:
        try:
            while self.is_active and self._ws:
                raw = await self._ws.recv()
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("[%s] non-JSON OpenAI agent event", self.session_id)
                    continue
                await self._handle_event(event)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            if self.is_active:
                logger.error("Agent OpenAI response loop error [%s]: %s", self.session_id, exc)
                self.is_active = False
        finally:
            logger.info("Agent OpenAI response loop ended [%s]", self.session_id)

    async def _handle_event(self, event: dict) -> None:
        event_type = event.get("type")

        if event_type == "response.output_audio.delta":
            await self._on_agent_status("speaking")
            delta = event.get("delta")
            if delta:
                await self._on_audio_output(base64.b64decode(delta))
            return

        if event_type == "response.output_audio_transcript.delta":
            item_id = str(event.get("item_id") or event.get("response_id") or "default")
            delta = event.get("delta")
            if delta:
                self._agent_transcript_fragments[item_id].append(str(delta))
            return

        if event_type == "response.output_text.delta":
            item_id = str(event.get("item_id") or event.get("response_id") or "default")
            delta = event.get("delta")
            if delta:
                self._agent_text_fragments[item_id].append(str(delta))
            return

        if event_type in {
            "response.output_audio_transcript.done",
            "response.output_text.done",
        }:
            await self._emit_agent_transcript(event)
            return

        if event_type == "conversation.item.input_audio_transcription.completed":
            transcript = str(event.get("transcript", "")).strip()
            if transcript:
                await self._on_transcript("callee", transcript)
            return

        if event_type == "input_audio_buffer.speech_started":
            await self._on_agent_status("listening")
            return

        if event_type == "input_audio_buffer.speech_stopped":
            await self._on_agent_status("thinking")
            return

        if event_type == "response.created":
            self._response_active = True
            await self._on_agent_status("thinking")
            return

        if event_type == "response.done":
            async with self._instruction_lock:
                self._response_active = False
                await self._dispatch_pending_instructions_locked()
            await self._on_agent_status("listening")
            return

        if event_type == "error":
            if event.get("code") == "conversation_already_has_active_response":
                self._response_active = True
            logger.error("[%s] OpenAI agent error: %s", self.session_id, event)

    async def _emit_agent_transcript(self, event: dict) -> None:
        item_id = str(event.get("item_id") or event.get("response_id") or "default")
        transcript = str(event.get("transcript") or event.get("text") or "").strip()
        if not transcript:
            fragments = self._agent_transcript_fragments.pop(item_id, None)
            if fragments:
                transcript = "".join(fragments).strip()
        if not transcript:
            fragments = self._agent_text_fragments.pop(item_id, None)
            if fragments:
                transcript = "".join(fragments).strip()
        else:
            self._agent_transcript_fragments.pop(item_id, None)
            self._agent_text_fragments.pop(item_id, None)
        if transcript:
            await self._on_transcript("agent", transcript)

    async def stop(self) -> None:
        if not self.is_active and not self._ws:
            return
        self.is_active = False
        if self._ws:
            try:
                await self._ws.close()
            except Exception as exc:
                logger.error("Error closing agent OpenAI session [%s]: %s", self.session_id, exc)

        if self._response_task and not self._response_task.done():
            self._response_task.cancel()
            try:
                await self._response_task
            except asyncio.CancelledError:
                pass
