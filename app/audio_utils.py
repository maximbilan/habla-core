"""
Audio codec conversion utilities.

Handles mulaw <-> PCM conversion and sample rate resampling for the
Twilio (mulaw 8 kHz) <-> model audio (PCM 24 kHz) <-> iOS (PCM 16 kHz)
audio pipeline.
"""

import base64

try:
    import audioop
except ImportError:
    import audioop_lts as audioop  # type: ignore[no-redef]


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def mulaw_to_pcm(mulaw_data: bytes) -> bytes:
    """Decode mulaw to 16-bit linear PCM."""
    return audioop.ulaw2lin(mulaw_data, 2)


def pcm_to_mulaw(pcm_data: bytes) -> bytes:
    """Encode 16-bit linear PCM to mulaw."""
    return audioop.lin2ulaw(pcm_data, 2)


def resample(pcm_data: bytes, from_rate: int, to_rate: int) -> bytes:
    """Resample 16-bit mono PCM between sample rates."""
    if from_rate == to_rate:
        return pcm_data
    converted, _ = audioop.ratecv(pcm_data, 2, 1, from_rate, to_rate, None)
    return converted


# ---------------------------------------------------------------------------
# Pipeline helpers
# ---------------------------------------------------------------------------

def mulaw_8k_to_pcm_16k(mulaw_data: bytes) -> bytes:
    """Twilio mulaw 8 kHz  ->  PCM 16-bit 16 kHz."""
    pcm_8k = mulaw_to_pcm(mulaw_data)
    return resample(pcm_8k, 8000, 16000)


def mulaw_8k_to_pcm_24k(mulaw_data: bytes) -> bytes:
    """Twilio mulaw 8 kHz  ->  PCM 16-bit 24 kHz (OpenAI input)."""
    pcm_8k = mulaw_to_pcm(mulaw_data)
    return resample(pcm_8k, 8000, 24000)


def pcm_16k_to_pcm_24k(pcm_16k: bytes) -> bytes:
    """PCM 16-bit 16 kHz  ->  PCM 16-bit 24 kHz."""
    return resample(pcm_16k, 16000, 24000)


def pcm_24k_to_mulaw_8k(pcm_24k: bytes) -> bytes:
    """Model output PCM 24 kHz  ->  mulaw 8 kHz (Twilio playback)."""
    pcm_8k = resample(pcm_24k, 24000, 8000)
    return pcm_to_mulaw(pcm_8k)


def pcm_24k_to_pcm_16k(pcm_24k: bytes) -> bytes:
    """Model output PCM 24 kHz  ->  PCM 16 kHz (iOS app speaker)."""
    return resample(pcm_24k, 24000, 16000)


# ---------------------------------------------------------------------------
# Twilio base64 helpers
# ---------------------------------------------------------------------------

def decode_twilio_media(payload: str) -> bytes:
    """Base64-decode a Twilio media payload to raw mulaw bytes."""
    return base64.b64decode(payload)


def encode_twilio_media(audio_data: bytes) -> str:
    """Base64-encode raw audio bytes for a Twilio media message."""
    return base64.b64encode(audio_data).decode("utf-8")
