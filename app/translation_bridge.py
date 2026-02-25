"""
Translation bridge — the heart of Habla.

For each active phone call this module runs TWO concurrent Nova 2 Sonic
sessions and routes audio between the four endpoints:

    iOS app  ←→  Session A (source→target)  ←→  Twilio phone call
    iOS app  ←→  Session B (target→source)  ←→  Twilio phone call

Audio pipeline:
    iOS mic  (PCM 16 kHz) → Session A → PCM 24 kHz → resample 8 kHz → mulaw → Twilio
    Twilio   (mulaw 8 kHz) → PCM → resample 16 kHz → Session B → PCM 24 kHz → resample 16 kHz → iOS
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from fastapi import WebSocket

from app.nova_sonic import NovaSonicSession
from app.audio_utils import (
    decode_twilio_media,
    encode_twilio_media,
    mulaw_8k_to_pcm_16k,
    pcm_24k_to_mulaw_8k,
    pcm_24k_to_pcm_16k,
)
from app.config import (
    INPUT_SAMPLE_RATE,
    OUTPUT_SAMPLE_RATE,
)
from app.language_support import (
    build_translation_system_prompt,
    voice_id_for_language,
)

logger = logging.getLogger(__name__)

QUEUE_READ_TIMEOUT = 1.0  # seconds
MAX_SESSION_RETRIES = 3
RETRY_DELAY = 1.0  # seconds


class TranslationBridge:
    """Manages the two Nova sessions and all audio routing for one call."""

    def __init__(
        self,
        call_sid: str,
        source_language: str,
        target_language: str,
        voice_gender: str | None = None,
    ) -> None:
        self.call_sid = call_sid
        self.source_language = source_language
        self.target_language = target_language
        self.voice_gender = voice_gender

        self.session_a: Optional[NovaSonicSession] = None  # source → target
        self.session_b: Optional[NovaSonicSession] = None  # target → source

        self.ios_ws: Optional[WebSocket] = None
        self.twilio_ws: Optional[WebSocket] = None
        self.twilio_stream_sid: Optional[str] = None

        self._tasks: list[asyncio.Task] = []
        self._closed = False

    # ------------------------------------------------------------------
    # Session A — source ➜ target  (iOS mic → phone speaker)
    # ------------------------------------------------------------------

    async def start_session_a(self, ios_ws: WebSocket) -> None:
        """Spin up Session A once the iOS app connects."""
        self.ios_ws = ios_ws

        self.session_a = NovaSonicSession(
            session_id=f"{self.call_sid}-{self.source_language}-{self.target_language}-a",
            system_prompt=build_translation_system_prompt(
                self.source_language, self.target_language
            ),
            voice_id=self._voice_id_for_language(self.target_language),
            input_sample_rate=INPUT_SAMPLE_RATE,
            output_sample_rate=OUTPUT_SAMPLE_RATE,
        )
        await self.session_a.start()

        self._tasks.append(
            asyncio.create_task(
                self._route_a_to_twilio(),
                name=f"{self.call_sid}-a→twilio",
            )
        )
        logger.info(
            "[%s] Session A started (%s→%s)",
            self.call_sid,
            self.source_language,
            self.target_language,
        )

    # ------------------------------------------------------------------
    # Session B — target ➜ source  (phone mic → iOS speaker)
    # ------------------------------------------------------------------

    async def start_session_b(
        self, twilio_ws: WebSocket, stream_sid: str
    ) -> None:
        """Spin up Session B once Twilio Media Streams connects."""
        self.twilio_ws = twilio_ws
        self.twilio_stream_sid = stream_sid

        self.session_b = NovaSonicSession(
            session_id=f"{self.call_sid}-{self.target_language}-{self.source_language}-b",
            system_prompt=build_translation_system_prompt(
                self.target_language, self.source_language
            ),
            voice_id=self._voice_id_for_language(self.source_language),
            input_sample_rate=INPUT_SAMPLE_RATE,
            output_sample_rate=OUTPUT_SAMPLE_RATE,
        )
        await self.session_b.start()

        self._tasks.append(
            asyncio.create_task(
                self._route_b_to_ios(),
                name=f"{self.call_sid}-b→ios",
            )
        )
        logger.info(
            "[%s] Session B started (%s→%s)",
            self.call_sid,
            self.target_language,
            self.source_language,
        )

    # ------------------------------------------------------------------
    # Inbound audio handlers (called by the WebSocket endpoints)
    # ------------------------------------------------------------------

    async def handle_ios_audio(self, pcm_16k: bytes) -> None:
        """iOS app sent a PCM 16 kHz chunk — forward to Session A."""
        if self.session_a and self.session_a.is_active:
            await self.session_a.send_audio(pcm_16k)

    async def handle_twilio_media(self, payload: str) -> None:
        """Twilio sent a base64 mulaw chunk — decode, resample, forward to Session B."""
        if not self.session_b or not self.session_b.is_active:
            return
        mulaw_bytes = decode_twilio_media(payload)
        pcm_16k = mulaw_8k_to_pcm_16k(mulaw_bytes)
        await self.session_b.send_audio(pcm_16k)

    # ------------------------------------------------------------------
    # Routing coroutines (run as background tasks)
    # ------------------------------------------------------------------

    async def _route_a_to_twilio(self) -> None:
        """Drain Session A output queue → convert → send to Twilio WS."""
        retries = 0
        try:
            while not self._closed:
                if not self._is_running(self.session_a):
                    if self.session_a and self.session_a._died_unexpectedly and retries < MAX_SESSION_RETRIES:
                        retries += 1
                        logger.warning("[%s] Session A died, retry %d/%d", self.call_sid, retries, MAX_SESSION_RETRIES)
                        await asyncio.sleep(RETRY_DELAY)
                        if not await self._restart_session_a():
                            break
                        continue
                    break

                chunk = await self._dequeue(self.session_a)
                if chunk is None:
                    continue

                if not self.twilio_ws or not self.twilio_stream_sid:
                    continue

                mulaw = pcm_24k_to_mulaw_8k(chunk)
                msg = json.dumps({
                    "event": "media",
                    "streamSid": self.twilio_stream_sid,
                    "media": {"payload": encode_twilio_media(mulaw)},
                })
                try:
                    await self.twilio_ws.send_text(msg)
                except Exception as e:
                    logger.error("[%s] send to Twilio failed: %s", self.call_sid, e)
                    break
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("[%s] route A→Twilio error: %s", self.call_sid, e)

    async def _route_b_to_ios(self) -> None:
        """Drain Session B output queue → resample → send to iOS WS."""
        retries = 0
        try:
            while not self._closed:
                if not self._is_running(self.session_b):
                    if self.session_b and self.session_b._died_unexpectedly and retries < MAX_SESSION_RETRIES:
                        retries += 1
                        logger.warning("[%s] Session B died, retry %d/%d", self.call_sid, retries, MAX_SESSION_RETRIES)
                        await asyncio.sleep(RETRY_DELAY)
                        if not await self._restart_session_b():
                            break
                        continue
                    break

                chunk = await self._dequeue(self.session_b)
                if chunk is None:
                    continue

                if not self.ios_ws:
                    continue

                pcm_16k = pcm_24k_to_pcm_16k(chunk)
                try:
                    await self.ios_ws.send_bytes(pcm_16k)
                except Exception as e:
                    logger.error("[%s] send to iOS failed: %s", self.call_sid, e)
                    break
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("[%s] route B→iOS error: %s", self.call_sid, e)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _is_running(self, session: Optional[NovaSonicSession]) -> bool:
        return not self._closed and session is not None and session.is_active

    async def _dequeue(
        self, session: Optional[NovaSonicSession]
    ) -> Optional[bytes]:
        if session is None:
            return None
        try:
            return await asyncio.wait_for(
                session.audio_output_queue.get(), timeout=QUEUE_READ_TIMEOUT
            )
        except asyncio.TimeoutError:
            return None

    def _voice_id_for_language(self, language_code: str) -> str:
        return voice_id_for_language(language_code, self.voice_gender)

    async def _restart_session_a(self) -> bool:
        """Restart Session A after an unexpected failure."""
        try:
            if self.session_a:
                try:
                    await self.session_a.close()
                except Exception:
                    pass

            self.session_a = NovaSonicSession(
                session_id=f"{self.call_sid}-{self.source_language}-{self.target_language}-a",
                system_prompt=build_translation_system_prompt(
                    self.source_language, self.target_language
                ),
                voice_id=self._voice_id_for_language(self.target_language),
                input_sample_rate=INPUT_SAMPLE_RATE,
                output_sample_rate=OUTPUT_SAMPLE_RATE,
            )
            await self.session_a.start()
            logger.info(
                "[%s] Session A restarted (%s→%s)",
                self.call_sid,
                self.source_language,
                self.target_language,
            )
            return True
        except Exception as e:
            logger.error("[%s] Failed to restart Session A: %s", self.call_sid, e)
            return False

    async def _restart_session_b(self) -> bool:
        """Restart Session B after an unexpected failure."""
        try:
            if self.session_b:
                try:
                    await self.session_b.close()
                except Exception:
                    pass

            self.session_b = NovaSonicSession(
                session_id=f"{self.call_sid}-{self.target_language}-{self.source_language}-b",
                system_prompt=build_translation_system_prompt(
                    self.target_language, self.source_language
                ),
                voice_id=self._voice_id_for_language(self.source_language),
                input_sample_rate=INPUT_SAMPLE_RATE,
                output_sample_rate=OUTPUT_SAMPLE_RATE,
            )
            await self.session_b.start()
            logger.info(
                "[%s] Session B restarted (%s→%s)",
                self.call_sid,
                self.target_language,
                self.source_language,
            )
            return True
        except Exception as e:
            logger.error("[%s] Failed to restart Session B: %s", self.call_sid, e)
            return False

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Tear down both Nova sessions and cancel routing tasks."""
        if self._closed:
            return
        self._closed = True
        logger.info("[%s] Shutting down translation bridge", self.call_sid)

        for task in self._tasks:
            if not task.done():
                task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

        for label, session in [("A", self.session_a), ("B", self.session_b)]:
            if session:
                try:
                    await session.close()
                except Exception as e:
                    logger.error(
                        "[%s] Error closing session %s: %s", self.call_sid, label, e
                    )

        logger.info("[%s] Translation bridge closed", self.call_sid)
