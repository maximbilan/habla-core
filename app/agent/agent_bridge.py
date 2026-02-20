"""Audio bridge between Twilio Media Streams and Agent Nova session."""

from __future__ import annotations

import json
import logging

from fastapi import WebSocket

from app.audio_utils import (
    decode_twilio_media,
    encode_twilio_media,
    mulaw_8k_to_pcm_16k,
    pcm_24k_to_mulaw_8k,
)

logger = logging.getLogger(__name__)


class AgentBridge:
    def __init__(self, call_sid: str) -> None:
        self.call_sid = call_sid
        self.twilio_ws: WebSocket | None = None
        self.stream_sid: str | None = None

    def attach_twilio(self, ws: WebSocket, stream_sid: str) -> None:
        self.twilio_ws = ws
        self.stream_sid = stream_sid

    async def forward_twilio_media_to_nova(self, payload: str, send_audio_cb) -> None:
        """Twilio mulaw 8k payload -> Nova PCM 16k."""
        mulaw = decode_twilio_media(payload)
        pcm_16k = mulaw_8k_to_pcm_16k(mulaw)
        await send_audio_cb(pcm_16k)

    async def forward_nova_audio_to_twilio(self, pcm_24k: bytes) -> None:
        """Nova PCM 24k -> Twilio mulaw 8k payload."""
        if not self.twilio_ws or not self.stream_sid:
            return

        mulaw = pcm_24k_to_mulaw_8k(pcm_24k)
        msg = json.dumps(
            {
                "event": "media",
                "streamSid": self.stream_sid,
                "media": {"payload": encode_twilio_media(mulaw)},
            }
        )

        try:
            await self.twilio_ws.send_text(msg)
        except Exception as exc:
            logger.error("[%s] failed forwarding Nova audio to Twilio: %s", self.call_sid, exc)
