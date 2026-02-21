"""
In-memory call state manager.

Tracks every active call and its associated WebSocket connections,
Nova sessions, and translation bridge.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Dict, Optional

from fastapi import WebSocket
from app.language_support import DEFAULT_SOURCE_LANGUAGE, DEFAULT_TARGET_LANGUAGE

if TYPE_CHECKING:
    from app.translation_bridge import TranslationBridge

logger = logging.getLogger(__name__)


class CallStatus(str, Enum):
    INITIATING = "initiating"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


@dataclass
class CallState:
    call_sid: str
    to_number: str
    from_number: str
    source_language: str = DEFAULT_SOURCE_LANGUAGE
    target_language: str = DEFAULT_TARGET_LANGUAGE
    status: CallStatus = CallStatus.INITIATING

    # WebSocket handles
    ios_ws: Optional[WebSocket] = None
    twilio_ws: Optional[WebSocket] = None
    twilio_stream_sid: Optional[str] = None

    # Translation bridge (owns the two Nova sessions)
    bridge: Optional[TranslationBridge] = None

    _cleanup_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class CallManager:
    """Thread-safe registry of active calls."""

    def __init__(self) -> None:
        self._calls: Dict[str, CallState] = {}

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create_call(
        self,
        call_sid: str,
        to_number: str,
        from_number: str,
        source_language: str = DEFAULT_SOURCE_LANGUAGE,
        target_language: str = DEFAULT_TARGET_LANGUAGE,
    ) -> CallState:
        state = CallState(
            call_sid=call_sid,
            to_number=to_number,
            from_number=from_number,
            source_language=source_language,
            target_language=target_language,
        )
        self._calls[call_sid] = state
        logger.info(
            "Call created: %s  %s → %s  (%s→%s)",
            call_sid,
            from_number,
            to_number,
            source_language,
            target_language,
        )
        return state

    def get_call(self, call_sid: str) -> Optional[CallState]:
        return self._calls.get(call_sid)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def cleanup_call(self, call_sid: str) -> None:
        """Tear down all resources for a call (idempotent)."""
        state = self.get_call(call_sid)
        if not state:
            return

        async with state._cleanup_lock:
            if state.status == CallStatus.COMPLETED:
                return

            logger.info("Cleaning up call %s", call_sid)
            state.status = CallStatus.COMPLETED

            # 1. close translation bridge (Nova sessions + routing tasks)
            if state.bridge:
                try:
                    await state.bridge.close()
                except Exception as e:
                    logger.error("Bridge close error for %s: %s", call_sid, e)

            # 2. close WebSockets
            for label, ws in [("iOS", state.ios_ws), ("Twilio", state.twilio_ws)]:
                if ws:
                    try:
                        await ws.close()
                    except Exception:
                        pass

            self._calls.pop(call_sid, None)
            logger.info("Call %s fully cleaned up", call_sid)
