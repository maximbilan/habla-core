"""Lifecycle manager for one Agent Mode call."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from fastapi import WebSocket

from app.agent.agent_bridge import AgentBridge
from app.agent.agent_nova_session import AgentNovaSession
from app.agent.prompts import build_agent_prompt
from app.agent.transcript import TranscriptService
from app.config import (
    PUBLIC_URL,
    NOVA_VOICE_ID_EN,
    NOVA_VOICE_ID_ES,
)
from app.caller_id.service import create_outbound_call, get_twilio_client
from app.language_support import (
    DEFAULT_TARGET_LANGUAGE,
    default_voice_id_for_language,
    resolve_supported_language,
)

logger = logging.getLogger(__name__)

MAX_NOVA_RESTART_ATTEMPTS = 5
MIN_NOVA_RESTART_INTERVAL_SECONDS = 1.5
TRANSCRIPT_DUPLICATE_SUPPRESSION_SECONDS = 20.0


@dataclass
class AgentCallConfig:
    to_number: str
    from_number: str | None
    prompt: str
    user_name: str
    language: str = DEFAULT_TARGET_LANGUAGE


class AgentCallManager:
    """Orchestrates Twilio stream, Nova stream, and iOS text websocket."""

    def __init__(self, call_sid: str, config: AgentCallConfig) -> None:
        self.call_sid = call_sid
        self.config = config
        self.status = "initiating"

        resolved_language = resolve_supported_language(config.language)
        if not resolved_language:
            resolved_language = resolve_supported_language(DEFAULT_TARGET_LANGUAGE)
        if not resolved_language:
            raise ValueError(
                f"Unsupported agent language '{config.language}' and invalid default '{DEFAULT_TARGET_LANGUAGE}'"
            )
        self._callee_language = resolved_language

        self.nova_session: AgentNovaSession | None = None
        self.ios_websocket: WebSocket | None = None
        self.bridge = AgentBridge(call_sid)
        self.transcript = TranscriptService(source_language_label=self._callee_language.label)

        self._ending = False
        self._end_lock = asyncio.Lock()
        self._nova_start_lock = asyncio.Lock()
        self._nova_restart_attempts = 0
        self._last_nova_start_monotonic = 0.0
        self._opening_instruction_sent = False
        self._recent_transcript_emit_monotonic: dict[str, float] = {}

    def status_payload(self) -> dict:
        return {
            "call_sid": self.call_sid,
            "status": self.status,
            "transcript": [
                {
                    "role": e.role,
                    "text_es": e.text_es,
                    "text_en": e.text_en,
                    "timestamp": e.timestamp,
                }
                for e in self.transcript.entries
            ],
        }

    async def attach_ios_websocket(self, ws: WebSocket) -> None:
        self.ios_websocket = ws
        await self._send_ios({"type": "status", "status": self.status})
        for entry in self.transcript.entries:
            await self._send_ios(
                {
                    "type": "transcript",
                    "role": entry.role,
                    "text_es": entry.text_es,
                    "text_en": entry.text_en,
                    "timestamp": entry.timestamp,
                }
            )

    def detach_ios_websocket(self, ws: WebSocket) -> None:
        if self.ios_websocket is ws:
            self.ios_websocket = None

    async def on_twilio_start(self, ws: WebSocket, stream_sid: str) -> None:
        self.bridge.attach_twilio(ws, stream_sid)
        self.status = "connected"
        await self._send_ios({"type": "status", "status": "connected"})

        if not self.nova_session or not self.nova_session.is_active:
            await self.ensure_nova_session()

    async def start_nova_session(self) -> None:
        system_prompt = build_agent_prompt(
            self.config.prompt,
            self.config.user_name,
            callee_language_code=self._callee_language.code,
            callee_language_label=self._callee_language.label,
        )

        self.nova_session = AgentNovaSession(
            session_id=f"agent-{self.call_sid}",
            system_prompt=system_prompt,
            voice_id=self._voice_id_for_language(self._callee_language.code),
            on_audio_output=self.handle_nova_audio,
            on_transcript=self.handle_transcript,
            on_agent_status=self.handle_agent_status,
        )
        await self.nova_session.start()

        # Kick off the conversation only once per call to avoid repeated monologues
        # after session restarts.
        if not self._opening_instruction_sent:
            await self.nova_session.inject_instruction(
                "The call is connected. Start with a brief greeting and the specific request from the user. You are asking for help from the callee, so do not ask 'How can I help you?'. After the request, pause and wait for a response."
            )
            self._opening_instruction_sent = True

    async def ensure_nova_session(self) -> bool:
        async with self._nova_start_lock:
            if self.nova_session and self.nova_session.is_active:
                return True

            loop = asyncio.get_running_loop()
            now = loop.time()

            if self._nova_restart_attempts >= MAX_NOVA_RESTART_ATTEMPTS:
                logger.error(
                    "[%s] Nova session restart limit reached (%d attempts)",
                    self.call_sid,
                    self._nova_restart_attempts,
                )
                self.status = "failed"
                await self._send_ios({"type": "status", "status": "failed"})
                return False

            # Prevent a tight restart loop when Nova fails immediately.
            if (
                self._last_nova_start_monotonic > 0
                and now - self._last_nova_start_monotonic < MIN_NOVA_RESTART_INTERVAL_SECONDS
            ):
                return False

            self._nova_restart_attempts += 1
            self._last_nova_start_monotonic = now

            try:
                await self.start_nova_session()
                return True
            except Exception as exc:
                logger.error("[%s] failed to start/restart Nova session: %s", self.call_sid, exc)
                self.status = "failed"
                await self._send_ios({"type": "status", "status": "failed"})
                return False

    async def handle_twilio_media(self, payload: str) -> None:
        if not await self.ensure_nova_session():
            return
        await self.bridge.forward_twilio_media_to_nova(payload, self.nova_session.send_audio)

    async def handle_nova_audio(self, audio_data: bytes) -> None:
        await self.bridge.forward_nova_audio_to_twilio(audio_data)

    async def handle_transcript(self, role: str, text_es: str) -> None:
        text = text_es.strip()
        if not text:
            return

        if self._is_control_transcript_payload(text):
            logger.info("[%s] dropping control transcript payload: %s", self.call_sid, text)
            return

        if not self._should_emit_transcript(role, text):
            logger.info("[%s] dropping duplicate transcript payload: role=%s", self.call_sid, role)
            return

        entry = self.transcript.add_entry(role, text)

        await self._send_ios(
            {
                "type": "transcript",
                "role": role,
                "text_es": text,
                "text_en": None,
                "timestamp": entry.timestamp,
            }
        )

        async def _translate_and_emit() -> None:
            try:
                translated = await self.transcript.translate_to_english(text)
                entry.text_en = translated
                await self._send_ios(
                    {
                        "type": "transcript_update",
                        "role": role,
                        "text_es": text,
                        "text_en": translated,
                        "timestamp": entry.timestamp,
                    }
                )
            except Exception as exc:
                logger.error("[%s] translation failed: %s", self.call_sid, exc)

        asyncio.create_task(_translate_and_emit())

    async def handle_agent_status(self, status: str) -> None:
        await self._send_ios({"type": "agent_status", "status": status})

    async def inject_instruction(self, instruction: str) -> None:
        if not instruction.strip():
            return
        if await self.ensure_nova_session():
            await self.nova_session.inject_instruction(instruction.strip())

    async def end_conversation(self, farewell_instruction: str) -> None:
        await self.inject_instruction(
            farewell_instruction or "Politely say goodbye and end the call."
        )

        async def _finish_later() -> None:
            await asyncio.sleep(2.5)
            await self.end_call()

        asyncio.create_task(_finish_later())

    async def end_call(self) -> None:
        async with self._end_lock:
            if self._ending:
                return
            self._ending = True

            try:
                get_twilio_client().calls(self.call_sid).update(status="completed")
            except Exception:
                pass

            if self.nova_session:
                try:
                    await self.nova_session.stop()
                except Exception as exc:
                    logger.error("[%s] error closing Nova session: %s", self.call_sid, exc)

            self.status = "ended"
            await self._send_ios({"type": "status", "status": "ended"})

    async def _send_ios(self, payload: dict) -> None:
        if not self.ios_websocket:
            return
        try:
            await self.ios_websocket.send_json(payload)
        except Exception:
            self.ios_websocket = None

    def _voice_id_for_language(self, language_code: str) -> str:
        if language_code == "en-US":
            return NOVA_VOICE_ID_EN
        if language_code == "es-US":
            return NOVA_VOICE_ID_ES
        return default_voice_id_for_language(language_code)

    def _should_emit_transcript(self, role: str, text: str) -> bool:
        normalized = self._normalize_transcript_for_dedupe(text)
        if not normalized:
            return False

        key = f"{role}|{normalized}"
        now = asyncio.get_running_loop().time()
        stale_cutoff = now - TRANSCRIPT_DUPLICATE_SUPPRESSION_SECONDS

        # Keep the dedupe map bounded by evicting stale entries.
        self._recent_transcript_emit_monotonic = {
            existing_key: emitted_at
            for existing_key, emitted_at in self._recent_transcript_emit_monotonic.items()
            if emitted_at >= stale_cutoff
        }

        previous_emit = self._recent_transcript_emit_monotonic.get(key)
        if previous_emit is not None and now - previous_emit < TRANSCRIPT_DUPLICATE_SUPPRESSION_SECONDS:
            return False

        self._recent_transcript_emit_monotonic[key] = now
        return True

    def _is_control_transcript_payload(self, text: str) -> bool:
        stripped = text.strip()
        lowered = stripped.lower()
        if lowered.startswith("[additional instruction from caller]"):
            return True

        if not (stripped.startswith("{") and stripped.endswith("}")):
            return False

        try:
            decoded = json.loads(stripped)
        except json.JSONDecodeError:
            return False

        if not isinstance(decoded, dict):
            return False

        known_control_keys = {"interrupted", "event", "type", "status", "reason"}
        return bool(known_control_keys.intersection(decoded.keys())) or len(decoded) <= 3

    def _normalize_transcript_for_dedupe(self, text: str) -> str:
        normalized = " ".join(text.strip().lower().split())
        normalized = re.sub(r"[^\w\s]", "", normalized)
        return normalized


class AgentCallRegistry:
    """In-memory state for active and recently-ended agent calls."""

    def __init__(self) -> None:
        self._calls: dict[str, AgentCallManager] = {}

    def create(self, call_sid: str, config: AgentCallConfig) -> AgentCallManager:
        manager = AgentCallManager(call_sid=call_sid, config=config)
        manager.status = "ringing"
        self._calls[call_sid] = manager
        return manager

    def get(self, call_sid: str) -> AgentCallManager | None:
        return self._calls.get(call_sid)


agent_calls = AgentCallRegistry()


def initiate_agent_outbound_call(to_number: str, from_number: str | None = None) -> tuple[str, str]:
    """Create outbound Twilio call for Agent Mode."""
    call_sid, caller_id = create_outbound_call(
        to_number=to_number,
        from_number=from_number,
        webhook_url=f"{PUBLIC_URL}/agent/twilio/webhook/pending",
        method="POST",
    )
    logger.info("Agent outbound call created: sid=%s to=%s", call_sid, to_number)
    return call_sid, caller_id
