"""Lifecycle manager for one Agent Mode call."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from twilio.rest import Client
from fastapi import WebSocket

from app.agent.agent_bridge import AgentBridge
from app.agent.agent_nova_session import AgentNovaSession
from app.agent.prompts import build_agent_prompt
from app.agent.transcript import TranscriptService
from app.config import (
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN,
    TWILIO_API_SID,
    TWILIO_API_SECRET,
    TWILIO_FROM_NUMBER,
    PUBLIC_URL,
    NOVA_VOICE_ID_ES,
)

logger = logging.getLogger(__name__)


def _twilio_client() -> Client:
    if TWILIO_API_SID and TWILIO_API_SECRET:
        return Client(TWILIO_API_SID, TWILIO_API_SECRET, TWILIO_ACCOUNT_SID)
    return Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


@dataclass
class AgentCallConfig:
    to_number: str
    from_number: str | None
    prompt: str
    user_name: str
    language: str = "es"


class AgentCallManager:
    """Orchestrates Twilio stream, Nova stream, and iOS text websocket."""

    def __init__(self, call_sid: str, config: AgentCallConfig) -> None:
        self.call_sid = call_sid
        self.config = config
        self.status = "initiating"

        self.nova_session: AgentNovaSession | None = None
        self.ios_websocket: WebSocket | None = None
        self.bridge = AgentBridge(call_sid)
        self.transcript = TranscriptService()

        self._ending = False
        self._end_lock = asyncio.Lock()

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
            await self.start_nova_session()

    async def start_nova_session(self) -> None:
        system_prompt = build_agent_prompt(self.config.prompt, self.config.user_name)

        self.nova_session = AgentNovaSession(
            session_id=f"agent-{self.call_sid}",
            system_prompt=system_prompt,
            voice_id=NOVA_VOICE_ID_ES,
            on_audio_output=self.handle_nova_audio,
            on_transcript=self.handle_transcript,
            on_agent_status=self.handle_agent_status,
        )
        await self.nova_session.start()

        # Kick off the conversation once the callee is connected.
        await self.nova_session.inject_instruction(
            "La llamada se ha conectado. Preséntese y comience la conversación ahora."
        )

    async def ensure_nova_session(self) -> bool:
        if self.nova_session and self.nova_session.is_active:
            return True

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
        entry = self.transcript.add_entry(role, text_es)

        await self._send_ios(
            {
                "type": "transcript",
                "role": role,
                "text_es": text_es,
                "text_en": None,
                "timestamp": entry.timestamp,
            }
        )

        async def _translate_and_emit() -> None:
            try:
                translated = await self.transcript.translate_to_english(text_es)
                entry.text_en = translated
                await self._send_ios(
                    {
                        "type": "transcript_update",
                        "role": role,
                        "text_es": text_es,
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
        await self.inject_instruction(farewell_instruction or "Despídase con cortesía y cierre la llamada.")

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
                _twilio_client().calls(self.call_sid).update(status="completed")
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


def initiate_agent_outbound_call(to_number: str, from_number: str | None = None) -> str:
    """Create outbound Twilio call for Agent Mode."""
    client = _twilio_client()
    caller_id = from_number or TWILIO_FROM_NUMBER

    call = client.calls.create(
        to=to_number,
        from_=caller_id,
        url=f"{PUBLIC_URL}/agent/twilio/webhook/pending",
        method="POST",
    )
    logger.info("Agent outbound call created: sid=%s to=%s", call.sid, to_number)
    return call.sid
