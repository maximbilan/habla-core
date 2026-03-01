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
from collections import deque
from dataclasses import dataclass
import json
import logging
import time
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
AUDIO_INPUT_QUEUE_MAX_CHUNKS = 24
AUDIO_INPUT_QUEUE_TIMEOUT = 0.2
AUDIO_OUTPUT_QUEUE_MAX_CHUNKS = 48


@dataclass(slots=True)
class _LatencyTrace:
    trace_id: int
    direction: str
    ingress_recv_monotonic: float
    model_send_monotonic: float


@dataclass(slots=True)
class _QueuedAudio:
    audio: bytes
    queued_monotonic: float
    trace: _LatencyTrace | None = None
    first_output_monotonic: float | None = None


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
        self._session_a_input_queue: asyncio.Queue[bytes] = asyncio.Queue(
            maxsize=AUDIO_INPUT_QUEUE_MAX_CHUNKS
        )
        self._session_b_input_queue: asyncio.Queue[bytes] = asyncio.Queue(
            maxsize=AUDIO_INPUT_QUEUE_MAX_CHUNKS
        )
        self._session_a_input_enqueued_at: deque[float] = deque()
        self._session_b_input_enqueued_at: deque[float] = deque()
        self._twilio_output_queue: asyncio.Queue[_QueuedAudio] = asyncio.Queue(
            maxsize=AUDIO_OUTPUT_QUEUE_MAX_CHUNKS
        )
        self._ios_output_queue: asyncio.Queue[_QueuedAudio] = asyncio.Queue(
            maxsize=AUDIO_OUTPUT_QUEUE_MAX_CHUNKS
        )
        self._trace_seq_a = 0
        self._trace_seq_b = 0
        self._active_trace_a: _LatencyTrace | None = None
        self._active_trace_b: _LatencyTrace | None = None

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
                self._pump_session_a_input(),
                name=f"{self.call_sid}-ios→a-input",
            )
        )
        self._tasks.append(
            asyncio.create_task(
                self._route_a_to_twilio(),
                name=f"{self.call_sid}-a→twilio",
            )
        )
        self._tasks.append(
            asyncio.create_task(
                self._send_twilio_output(),
                name=f"{self.call_sid}-twilio-send",
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
                self._pump_session_b_input(),
                name=f"{self.call_sid}-twilio→b-input",
            )
        )
        self._tasks.append(
            asyncio.create_task(
                self._route_b_to_ios(),
                name=f"{self.call_sid}-b→ios",
            )
        )
        self._tasks.append(
            asyncio.create_task(
                self._send_ios_output(),
                name=f"{self.call_sid}-ios-send",
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
            self._enqueue_input_audio(
                self._session_a_input_queue,
                self._session_a_input_enqueued_at,
                pcm_16k,
            )

    async def handle_twilio_media(self, payload: str) -> None:
        """Twilio sent a base64 mulaw chunk — decode, resample, forward to Session B."""
        if not self.session_b or not self.session_b.is_active:
            return
        mulaw_bytes = decode_twilio_media(payload)
        pcm_16k = mulaw_8k_to_pcm_16k(mulaw_bytes)
        self._enqueue_input_audio(
            self._session_b_input_queue,
            self._session_b_input_enqueued_at,
            pcm_16k,
        )

    # ------------------------------------------------------------------
    # Routing coroutines (run as background tasks)
    # ------------------------------------------------------------------

    async def _route_a_to_twilio(self) -> None:
        """Drain Session A output queue and enqueue for Twilio sender task."""
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

                now = time.monotonic()
                trace = self._active_trace_a
                first_output_monotonic: float | None = None
                if trace is not None:
                    first_output_monotonic = now
                    self._log_first_model_audio(trace, first_output_monotonic)
                    self._active_trace_a = None

                self._enqueue_output_audio(
                    self._twilio_output_queue,
                    _QueuedAudio(
                        audio=chunk,
                        queued_monotonic=now,
                        trace=trace,
                        first_output_monotonic=first_output_monotonic,
                    ),
                )
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("[%s] route A→Twilio error: %s", self.call_sid, e)

    async def _pump_session_a_input(self) -> None:
        try:
            while not self._closed:
                if not self._is_running(self.session_a):
                    await asyncio.sleep(0.02)
                    continue

                payload = await self._read_input_chunk(
                    self._session_a_input_queue,
                    self._session_a_input_enqueued_at,
                )
                if payload is None:
                    continue
                chunk, ingress_monotonic = payload
                model_send_monotonic = time.monotonic()
                if self._active_trace_a is None:
                    self._trace_seq_a += 1
                    self._active_trace_a = _LatencyTrace(
                        trace_id=self._trace_seq_a,
                        direction="ios_to_twilio",
                        ingress_recv_monotonic=ingress_monotonic,
                        model_send_monotonic=model_send_monotonic,
                    )
                    logger.debug(
                        "[%s] latency trace=%d dir=%s ingress_to_model_send_ms=%.1f",
                        self.call_sid,
                        self._active_trace_a.trace_id,
                        self._active_trace_a.direction,
                        (model_send_monotonic - ingress_monotonic) * 1000.0,
                    )
                await self.session_a.send_audio(chunk)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("[%s] route iOS→A error: %s", self.call_sid, e)

    async def _pump_session_b_input(self) -> None:
        try:
            while not self._closed:
                if not self._is_running(self.session_b):
                    await asyncio.sleep(0.02)
                    continue

                payload = await self._read_input_chunk(
                    self._session_b_input_queue,
                    self._session_b_input_enqueued_at,
                )
                if payload is None:
                    continue
                chunk, ingress_monotonic = payload
                model_send_monotonic = time.monotonic()
                if self._active_trace_b is None:
                    self._trace_seq_b += 1
                    self._active_trace_b = _LatencyTrace(
                        trace_id=self._trace_seq_b,
                        direction="twilio_to_ios",
                        ingress_recv_monotonic=ingress_monotonic,
                        model_send_monotonic=model_send_monotonic,
                    )
                    logger.debug(
                        "[%s] latency trace=%d dir=%s ingress_to_model_send_ms=%.1f",
                        self.call_sid,
                        self._active_trace_b.trace_id,
                        self._active_trace_b.direction,
                        (model_send_monotonic - ingress_monotonic) * 1000.0,
                    )
                await self.session_b.send_audio(chunk)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("[%s] route Twilio→B error: %s", self.call_sid, e)

    async def _route_b_to_ios(self) -> None:
        """Drain Session B output queue and enqueue for iOS sender task."""
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

                now = time.monotonic()
                trace = self._active_trace_b
                first_output_monotonic: float | None = None
                if trace is not None:
                    first_output_monotonic = now
                    self._log_first_model_audio(trace, first_output_monotonic)
                    self._active_trace_b = None

                self._enqueue_output_audio(
                    self._ios_output_queue,
                    _QueuedAudio(
                        audio=chunk,
                        queued_monotonic=now,
                        trace=trace,
                        first_output_monotonic=first_output_monotonic,
                    ),
                )
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("[%s] route B→iOS error: %s", self.call_sid, e)

    async def _send_twilio_output(self) -> None:
        try:
            while not self._closed:
                queued = await self._read_output_chunk(self._twilio_output_queue)
                if queued is None:
                    continue
                if not self.twilio_ws or not self.twilio_stream_sid:
                    continue

                mulaw = pcm_24k_to_mulaw_8k(queued.audio)
                msg = json.dumps(
                    {
                        "event": "media",
                        "streamSid": self.twilio_stream_sid,
                        "media": {"payload": encode_twilio_media(mulaw)},
                    }
                )
                try:
                    await self.twilio_ws.send_text(msg)
                    self._log_ws_send_latency(
                        queued=queued,
                        sink="twilio",
                        ws_send_monotonic=time.monotonic(),
                    )
                except Exception as e:
                    logger.error("[%s] send to Twilio failed: %s", self.call_sid, e)
                    break
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("[%s] twilio sender task error: %s", self.call_sid, e)

    async def _send_ios_output(self) -> None:
        try:
            while not self._closed:
                queued = await self._read_output_chunk(self._ios_output_queue)
                if queued is None:
                    continue
                if not self.ios_ws:
                    continue

                pcm_16k = pcm_24k_to_pcm_16k(queued.audio)
                try:
                    await self.ios_ws.send_bytes(pcm_16k)
                    self._log_ws_send_latency(
                        queued=queued,
                        sink="ios",
                        ws_send_monotonic=time.monotonic(),
                    )
                except Exception as e:
                    logger.error("[%s] send to iOS failed: %s", self.call_sid, e)
                    break
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("[%s] ios sender task error: %s", self.call_sid, e)

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

    async def _read_input_chunk(
        self,
        queue: asyncio.Queue[bytes],
        enqueued_at: deque[float],
    ) -> Optional[tuple[bytes, float]]:
        try:
            chunk = await asyncio.wait_for(queue.get(), timeout=AUDIO_INPUT_QUEUE_TIMEOUT)
        except asyncio.TimeoutError:
            return None
        ingress_monotonic = enqueued_at.popleft() if enqueued_at else time.monotonic()
        return chunk, ingress_monotonic

    async def _read_output_chunk(
        self, queue: asyncio.Queue[_QueuedAudio]
    ) -> _QueuedAudio | None:
        try:
            return await asyncio.wait_for(queue.get(), timeout=AUDIO_INPUT_QUEUE_TIMEOUT)
        except asyncio.TimeoutError:
            return None

    def _enqueue_input_audio(
        self,
        queue: asyncio.Queue[bytes],
        enqueued_at: deque[float],
        chunk: bytes,
    ) -> None:
        received_monotonic = time.monotonic()
        try:
            queue.put_nowait(chunk)
            enqueued_at.append(received_monotonic)
        except asyncio.QueueFull:
            try:
                _ = queue.get_nowait()
                if enqueued_at:
                    enqueued_at.popleft()
            except asyncio.QueueEmpty:
                pass
            try:
                queue.put_nowait(chunk)
                enqueued_at.append(received_monotonic)
            except asyncio.QueueFull:
                pass

    def _enqueue_output_audio(
        self,
        queue: asyncio.Queue[_QueuedAudio],
        chunk: _QueuedAudio,
    ) -> None:
        try:
            queue.put_nowait(chunk)
        except asyncio.QueueFull:
            try:
                _ = queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                queue.put_nowait(chunk)
            except asyncio.QueueFull:
                pass

    def _log_first_model_audio(self, trace: _LatencyTrace, first_output_monotonic: float) -> None:
        logger.info(
            "[%s] latency dir=%s trace=%d ingress_to_model_send_ms=%.1f model_to_first_audio_ms=%.1f ingress_to_first_audio_ms=%.1f",
            self.call_sid,
            trace.direction,
            trace.trace_id,
            (trace.model_send_monotonic - trace.ingress_recv_monotonic) * 1000.0,
            (first_output_monotonic - trace.model_send_monotonic) * 1000.0,
            (first_output_monotonic - trace.ingress_recv_monotonic) * 1000.0,
        )

    def _log_ws_send_latency(
        self,
        *,
        queued: _QueuedAudio,
        sink: str,
        ws_send_monotonic: float,
    ) -> None:
        queue_to_ws_send_ms = (ws_send_monotonic - queued.queued_monotonic) * 1000.0
        if queued.trace is None or queued.first_output_monotonic is None:
            logger.debug(
                "[%s] latency sink=%s queue_to_ws_send_ms=%.1f",
                self.call_sid,
                sink,
                queue_to_ws_send_ms,
            )
            return

        logger.info(
            "[%s] latency dir=%s trace=%d first_audio_to_ws_send_ms=%.1f end_to_end_first_audio_ms=%.1f queue_to_ws_send_ms=%.1f sink=%s",
            self.call_sid,
            queued.trace.direction,
            queued.trace.trace_id,
            (ws_send_monotonic - queued.first_output_monotonic) * 1000.0,
            (ws_send_monotonic - queued.trace.ingress_recv_monotonic) * 1000.0,
            queue_to_ws_send_ms,
            sink,
        )

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
