"""OpenAI Realtime sessions for live speech translation."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Awaitable, Callable
from urllib.parse import quote

import websockets

from app.config import (
    OPENAI_API_KEY,
    OPENAI_REALTIME_TRANSLATE_MODEL,
)
from app.language_support import openai_language_code

logger = logging.getLogger(__name__)

OPENAI_REALTIME_TRANSLATION_URL = "wss://api.openai.com/v1/realtime/translations"


def _json_dumps(payload: dict) -> str:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=True)


def _require_openai_api_key() -> str:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is required for OpenAI Realtime sessions")
    return OPENAI_API_KEY


class OpenAIRealtimeTranslationSession:
    """One OpenAI Realtime translation stream for a single language direction."""

    def __init__(
        self,
        session_id: str,
        target_language: str,
        model: str = OPENAI_REALTIME_TRANSLATE_MODEL,
        on_audio_output: Callable[[bytes], Awaitable[None]] | None = None,
    ) -> None:
        self.session_id = session_id
        self.target_language = openai_language_code(target_language)
        self.model = model
        self.audio_output_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self.is_active = False
        self._died_unexpectedly = False
        self._ws = None
        self._response_task: asyncio.Task | None = None
        self._on_audio_output = on_audio_output

    async def start(self) -> None:
        api_key = _require_openai_api_key()
        url = (
            f"{OPENAI_REALTIME_TRANSLATION_URL}"
            f"?model={quote(self.model, safe='')}"
        )
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
            name=f"openai-translate-rx-{self.session_id}",
        )
        logger.info(
            "OpenAI translation session %s started (model=%s target=%s)",
            self.session_id,
            self.model,
            self.target_language,
        )

    def _session_update_event(self) -> dict:
        return {
            "type": "session.update",
            "session": {
                "audio": {
                    "output": {
                        "language": self.target_language,
                    },
                },
            },
        }

    def _audio_append_event(self, pcm_audio: bytes) -> dict:
        return {
            "type": "session.input_audio_buffer.append",
            "audio": base64.b64encode(pcm_audio).decode("ascii"),
        }

    async def _send(self, event: dict) -> None:
        if not self._ws:
            return
        await self._ws.send(_json_dumps(event))

    async def send_audio(self, pcm_audio: bytes) -> None:
        if not self.is_active:
            return
        await self._send(self._audio_append_event(pcm_audio))

    async def _process_responses(self) -> None:
        try:
            while self.is_active and self._ws:
                raw = await self._ws.recv()
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("[%s] non-JSON OpenAI event", self.session_id)
                    continue
                await self._handle_event(event)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            if self.is_active:
                logger.error(
                    "OpenAI translation response loop error [%s]: %s",
                    self.session_id,
                    exc,
                )
                self.is_active = False
                self._died_unexpectedly = True
        finally:
            logger.info("OpenAI translation response loop ended [%s]", self.session_id)

    async def _handle_event(self, event: dict) -> None:
        event_type = event.get("type")
        if event_type == "session.output_audio.delta":
            delta = event.get("delta")
            if not delta:
                return
            audio_bytes = base64.b64decode(delta)
            await self.audio_output_queue.put(audio_bytes)
            if self._on_audio_output:
                await self._on_audio_output(audio_bytes)
            return

        if event_type in {
            "session.output_transcript.delta",
            "session.input_transcript.delta",
        }:
            logger.debug(
                "[%s] %s: %s",
                self.session_id,
                event_type,
                event.get("delta", ""),
            )
            return

        if event_type == "error":
            logger.error("[%s] OpenAI translation error: %s", self.session_id, event)

    async def close(self) -> None:
        if not self.is_active and not self._ws:
            return
        self.is_active = False
        if self._ws:
            try:
                await self._ws.close()
            except Exception as exc:
                logger.error(
                    "Error closing OpenAI translation websocket [%s]: %s",
                    self.session_id,
                    exc,
                )

        if self._response_task and not self._response_task.done():
            self._response_task.cancel()
            try:
                await self._response_task
            except asyncio.CancelledError:
                pass
        logger.info("OpenAI translation session %s closed", self.session_id)
