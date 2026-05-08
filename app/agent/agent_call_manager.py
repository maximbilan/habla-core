"""Lifecycle manager for one Agent Mode call."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import urlparse
from fastapi import WebSocket

from app.agent.agent_bridge import AgentBridge
from app.agent.agent_openai_session import AgentOpenAIRealtimeSession
from app.agent.prompts import build_agent_prompt
from app.agent.transcript import TranscriptService
from app.config import (
    PUBLIC_URL,
)
from app.caller_id.service import create_outbound_call, get_twilio_client
from app.language_support import (
    DEFAULT_TARGET_LANGUAGE,
    resolve_supported_language,
)
from app.agent.critical_info import CriticalInfoTracker

logger = logging.getLogger(__name__)

MAX_NOVA_RESTART_ATTEMPTS = 5
MIN_NOVA_RESTART_INTERVAL_SECONDS = 1.5
TRANSCRIPT_DUPLICATE_SUPPRESSION_SECONDS = 20.0
AUTO_END_AFTER_FAREWELL_SECONDS = 5.0
LISTEN_FIRST_GUIDANCE_MIN_INTERVAL_SECONDS = 1.2
RUNTIME_COACHING_COOLDOWN_SECONDS = 0.75
MAX_RECENT_AGENT_TURNS = 6
AGENT_REPETITION_WINDOW = 4
AGENT_REPETITION_SIMILARITY_THRESHOLD = 0.84


def _normalized_public_url() -> str:
    public_url = PUBLIC_URL.strip().rstrip("/")
    parsed = urlparse(public_url)
    if not public_url or parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError(
            "PUBLIC_URL must be an absolute http(s) URL before starting Twilio calls"
        )
    return public_url


def agent_media_stream_ws_base() -> str:
    return _normalized_public_url().replace("https://", "wss://").replace("http://", "ws://")


@dataclass
class AgentCallConfig:
    to_number: str
    from_number: str | None
    prompt: str
    user_name: str
    language: str = DEFAULT_TARGET_LANGUAGE
    voice_gender: str | None = None


class AgentCallManager:
    """Orchestrates Twilio stream, OpenAI Realtime stream, and iOS text websocket."""

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

        self.nova_session: AgentOpenAIRealtimeSession | None = None
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
        self._auto_end_task: asyncio.Task | None = None
        self._has_callee_uttered = False
        self._last_runtime_coaching_monotonic = 0.0
        self._last_listen_guidance_monotonic = 0.0
        self._last_callee_transcript_normalized = ""
        self._recent_agent_turns_normalized: list[str] = []
        self._quality_metrics: dict[str, int | float] = {
            "callee_turns": 0,
            "agent_turns": 0,
            "agent_words": 0,
            "repeat_guard_triggers": 0,
            "listen_first_guidance": 0,
        }
        self._critical_tracker = CriticalInfoTracker()
        self._translation_tasks: set[asyncio.Task] = set()

    def status_payload(self) -> dict:
        return {
            "call_sid": self.call_sid,
            "status": self.status,
            "transcript": [
                {
                    "role": e.role,
                    "text_original": e.text_original,
                    "text_en": e.text_en,
                    "timestamp": e.timestamp,
                }
                for e in self.transcript.entries
            ],
            "quality_metrics": self._quality_metrics_payload(),
            "verified_facts": self._critical_tracker.summary_facts(),
        }

    async def attach_ios_websocket(self, ws: WebSocket) -> None:
        self.ios_websocket = ws
        await self._send_ios({"type": "status", "status": self.status})
        for entry in self.transcript.entries:
            await self._send_ios(
                {
                    "type": "transcript",
                    "role": entry.role,
                    "text_original": entry.text_original,
                    "text_en": entry.text_en,
                    "timestamp": entry.timestamp,
                }
            )

        await self._send_verified_facts_summary()

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

        self.nova_session = AgentOpenAIRealtimeSession(
            session_id=f"agent-{self.call_sid}",
            system_prompt=system_prompt,
            callee_language=self._callee_language.code,
            on_audio_output=self.handle_nova_audio,
            on_transcript=self.handle_transcript,
            on_agent_status=self.handle_agent_status,
        )
        await self.nova_session.start()

        # Kick off the conversation only once per call to avoid repeated monologues
        # after session restarts.
        if not self._opening_instruction_sent:
            await self.nova_session.inject_instruction(
                "The call is connected. Start with a brief greeting and the specific request from the user. Keep it to one concise turn, ask at most one focused question, then pause and wait for the callee. Do not ask 'How can I help you?'. Do not mention translation, interpreters, system details, or delays unless explicitly asked."
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
                    "[%s] OpenAI session restart limit reached (%d attempts)",
                    self.call_sid,
                    self._nova_restart_attempts,
                )
                self.status = "failed"
                await self._send_ios({"type": "status", "status": "failed"})
                return False

            # Prevent a tight restart loop when OpenAI fails immediately.
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
                logger.error("[%s] failed to start/restart OpenAI session: %s", self.call_sid, exc)
                self.status = "failed"
                await self._send_ios({"type": "status", "status": "failed"})
                return False

    async def handle_twilio_media(self, payload: str) -> None:
        if not await self.ensure_nova_session():
            return

        await self.bridge.forward_twilio_media_to_nova(payload, self.nova_session.send_audio)

    async def handle_nova_audio(self, audio_data: bytes) -> None:
        await self.bridge.forward_nova_audio_to_twilio(audio_data)

    async def handle_transcript(self, role: str, text_original: str) -> None:
        text = text_original.strip()
        if not text:
            return

        if role == "callee":
            self._has_callee_uttered = True

        if self._is_control_transcript_payload(text):
            logger.info("[%s] dropping control transcript payload: %s", self.call_sid, text)
            return

        if not self._should_emit_transcript(role, text):
            logger.info("[%s] dropping duplicate transcript payload: role=%s", self.call_sid, role)
            return

        entry = self.transcript.add_entry(role, text)

        await self._send_critical_confirmations(
            self._critical_tracker.observe_text(role=role, text=text)
        )
        await self._send_verified_facts_summary()

        if role == "callee":
            self._quality_metrics["callee_turns"] += 1
            await self._maybe_inject_listen_first_guidance(text)
        elif role == "agent":
            self._quality_metrics["agent_turns"] += 1
            self._quality_metrics["agent_words"] += self._word_count(text)
            if self._is_repetitive_agent_turn(text):
                self._quality_metrics["repeat_guard_triggers"] += 1
                await self._inject_runtime_instruction(
                    "You are repeating prior phrasing. Rephrase naturally in one short sentence, acknowledge the callee's latest point, and ask at most one focused follow-up question.",
                    force=True,
                )

        await self._send_ios(
            {
                "type": "transcript",
                "role": role,
                "text_original": text,
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
                        "text_original": text,
                        "text_en": translated,
                        "timestamp": entry.timestamp,
                    }
                )

                await self._send_critical_confirmations(
                    self._critical_tracker.observe_translation_pair(
                        source_text=text,
                        translated_text=translated,
                    )
                )
                await self._send_verified_facts_summary()
            except Exception as exc:
                logger.error("[%s] translation failed: %s", self.call_sid, exc)

        self._track_translation_task(asyncio.create_task(_translate_and_emit()))

        if role == "agent" and self._has_callee_uttered and self._should_auto_end_after_agent_turn(text):
            self._schedule_auto_end_after_farewell()

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
                    logger.error("[%s] error closing OpenAI session: %s", self.call_sid, exc)

            await self._wait_for_translation_tasks(timeout=1.2)
            await self._send_verified_facts_summary()

            self.status = "ended"
            await self._send_ios({"type": "status", "status": "ended"})

    async def _send_ios(self, payload: dict) -> None:
        if not self.ios_websocket:
            return
        try:
            await self.ios_websocket.send_json(payload)
        except Exception:
            self.ios_websocket = None

    async def _send_critical_confirmations(self, prompts) -> None:
        for prompt in prompts:
            await self._send_ios(prompt.to_payload())

    async def _send_verified_facts_summary(self) -> None:
        summary = self._critical_tracker.summary_payload()
        facts = summary.get("facts", [])
        if not facts:
            return
        await self._send_ios(summary)

    def _track_translation_task(self, task: asyncio.Task) -> None:
        self._translation_tasks.add(task)
        task.add_done_callback(lambda done: self._translation_tasks.discard(done))

    async def _wait_for_translation_tasks(self, *, timeout: float) -> None:
        if not self._translation_tasks:
            return
        pending = [task for task in self._translation_tasks if not task.done()]
        if not pending:
            return
        done, _ = await asyncio.wait(pending, timeout=timeout)
        for task in done:
            self._translation_tasks.discard(task)

    def _quality_metrics_payload(self) -> dict[str, int | float]:
        agent_turns = int(self._quality_metrics.get("agent_turns", 0))
        agent_words = int(self._quality_metrics.get("agent_words", 0))
        avg_words = (agent_words / agent_turns) if agent_turns else 0.0

        return {
            "callee_turns": int(self._quality_metrics.get("callee_turns", 0)),
            "agent_turns": agent_turns,
            "avg_agent_words_per_turn": round(avg_words, 2),
            "repeat_guard_triggers": int(self._quality_metrics.get("repeat_guard_triggers", 0)),
            "listen_first_guidance": int(self._quality_metrics.get("listen_first_guidance", 0)),
        }

    async def _maybe_inject_listen_first_guidance(self, callee_text: str) -> None:
        normalized = self._normalize_transcript_for_dedupe(callee_text)
        if not normalized:
            return

        now = asyncio.get_running_loop().time()
        if (
            normalized == self._last_callee_transcript_normalized
            and now - self._last_listen_guidance_monotonic < TRANSCRIPT_DUPLICATE_SUPPRESSION_SECONDS
        ):
            return

        if (
            self._last_listen_guidance_monotonic > 0
            and now - self._last_listen_guidance_monotonic < LISTEN_FIRST_GUIDANCE_MIN_INTERVAL_SECONDS
        ):
            return

        self._last_callee_transcript_normalized = normalized
        self._last_listen_guidance_monotonic = now
        self._quality_metrics["listen_first_guidance"] += 1
        snippet = self._truncate_for_instruction(callee_text.replace('"', "'"), max_len=180)

        await self._inject_runtime_instruction(
            (
                f'The callee just said: "{snippet}". In your next response, acknowledge that point first, '
                "then continue with one concise sentence or one focused follow-up question. "
                "Do not restart the full request or repeat your previous wording."
            ),
            force=False,
        )

    async def _inject_runtime_instruction(self, instruction: str, *, force: bool) -> None:
        if not instruction.strip():
            return
        if not self.nova_session or not self.nova_session.is_active:
            return

        now = asyncio.get_running_loop().time()
        if (
            not force
            and self._last_runtime_coaching_monotonic > 0
            and now - self._last_runtime_coaching_monotonic < RUNTIME_COACHING_COOLDOWN_SECONDS
        ):
            return

        self._last_runtime_coaching_monotonic = now
        try:
            await self.nova_session.inject_instruction(instruction.strip())
        except Exception as exc:
            logger.error("[%s] failed injecting runtime instruction: %s", self.call_sid, exc)

    def _is_repetitive_agent_turn(self, text: str) -> bool:
        normalized = self._normalize_transcript_for_dedupe(text)
        if not normalized:
            return False

        recent = self._recent_agent_turns_normalized[-AGENT_REPETITION_WINDOW:]
        repetitive = any(
            self._agent_turn_similarity(normalized, previous)
            >= AGENT_REPETITION_SIMILARITY_THRESHOLD
            for previous in recent
        )

        self._recent_agent_turns_normalized.append(normalized)
        if len(self._recent_agent_turns_normalized) > MAX_RECENT_AGENT_TURNS:
            self._recent_agent_turns_normalized = self._recent_agent_turns_normalized[
                -MAX_RECENT_AGENT_TURNS:
            ]
        return repetitive

    def _agent_turn_similarity(self, left: str, right: str) -> float:
        if not left or not right:
            return 0.0
        if left == right:
            return 1.0
        if left in right or right in left:
            shorter = min(len(left), len(right))
            longer = max(len(left), len(right))
            if longer > 0:
                return shorter / longer

        left_tokens = set(left.split())
        right_tokens = set(right.split())
        if not left_tokens or not right_tokens:
            return 0.0

        overlap = len(left_tokens.intersection(right_tokens))
        return overlap / max(len(left_tokens), len(right_tokens))

    def _word_count(self, text: str) -> int:
        return len(re.findall(r"\w+", text, flags=re.UNICODE))

    def _truncate_for_instruction(self, text: str, max_len: int) -> str:
        if len(text) <= max_len:
            return text
        return f"{text[: max_len - 3].rstrip()}..."

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

    def _schedule_auto_end_after_farewell(self) -> None:
        if self._ending:
            return
        if self._auto_end_task and not self._auto_end_task.done():
            return

        async def _auto_end_later() -> None:
            await asyncio.sleep(AUTO_END_AFTER_FAREWELL_SECONDS)
            if not self._ending:
                await self.end_call()

        logger.info("[%s] scheduling automatic call end after farewell", self.call_sid)
        self._auto_end_task = asyncio.create_task(_auto_end_later())

    def _should_auto_end_after_agent_turn(self, text: str) -> bool:
        normalized = self._normalize_for_intent_detection(text)
        if not normalized:
            return False

        if "?" in text:
            return False

        continuation_markers = (
            "podria",
            "puede ",
            "pueden ",
            "necesito ",
            "falta",
            "confirm",
            "cuando",
            "donde",
            "como ",
            "cual",
            "cuanto",
            "help you",
            "can you",
            "would you",
            "please",
        )
        if any(marker in normalized for marker in continuation_markers):
            return False

        goodbye_markers = (
            "adios",
            "hasta luego",
            "hasta pronto",
            "me despido",
            "goodbye",
            "bye",
            "have a good day",
            "have a great day",
            "au revoir",
            "arrivederci",
            "tschuss",
            "tchau",
        )
        closing_markers = (
            "eso es todo",
            "con eso seria todo",
            "no necesito nada mas",
            "nada mas por ahora",
            "quedamos asi",
            "muchas gracias por su ayuda",
            "gracias por su tiempo",
        )

        return any(marker in normalized for marker in goodbye_markers) or any(
            marker in normalized for marker in closing_markers
        )

    def _normalize_for_intent_detection(self, text: str) -> str:
        normalized = " ".join(text.strip().lower().split())
        normalized = "".join(
            ch for ch in unicodedata.normalize("NFKD", normalized) if not unicodedata.combining(ch)
        )
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


def initiate_agent_outbound_call(
    to_number: str,
    from_number: str | None = None,
    device_id: str | None = None,
) -> tuple[str, str]:
    """Create outbound Twilio call for Agent Mode."""
    call_sid, caller_id = create_outbound_call(
        to_number=to_number,
        from_number=from_number,
        device_id=device_id,
        webhook_url=f"{_normalized_public_url()}/agent/twilio/webhook/pending",
        method="POST",
    )
    logger.info("Agent outbound call created: sid=%s to=%s", call_sid, to_number)
    return call_sid, caller_id
