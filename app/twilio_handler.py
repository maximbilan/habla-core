"""
Twilio voice integration — outbound calls and Media Streams TwiML.
"""

import logging
from urllib.parse import urlparse

from twilio.twiml.voice_response import VoiceResponse, Connect, Stream

from app.config import (
    PUBLIC_URL,
)
from app.caller_id.service import create_outbound_call, get_twilio_client

logger = logging.getLogger(__name__)


def _normalized_public_url() -> str:
    public_url = PUBLIC_URL.strip().rstrip("/")
    parsed = urlparse(public_url)
    if not public_url or parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError(
            "PUBLIC_URL must be an absolute http(s) URL before starting Twilio calls"
        )
    return public_url


def _media_stream_ws_url() -> str:
    """Derive the wss:// media-stream URL from PUBLIC_URL."""
    return (
        _normalized_public_url()
        .replace("https://", "wss://")
        .replace("http://", "ws://")
        + "/twilio/media-stream"
    )


def initiate_outbound_call(
    to_number: str,
    from_number: str | None = None,
    device_id: str | None = None,
) -> tuple[str, str]:
    """
    Place an outbound PSTN call via Twilio.

    When the callee answers, Twilio will open a Media Streams WebSocket
    back to our /twilio/media-stream endpoint.

    Returns the Twilio Call SID.
    """
    ws_url = _media_stream_ws_url()

    twiml_xml = (
        "<Response>"
        "<Connect>"
        f'<Stream url="{ws_url}" />'
        "</Connect>"
        "</Response>"
    )

    call_sid, caller_id = create_outbound_call(
        to_number=to_number,
        from_number=from_number,
        device_id=device_id,
        twiml=twiml_xml,
    )
    logger.info("Twilio outbound call created: sid=%s  to=%s", call_sid, to_number)
    return call_sid, caller_id


# ---------------------------------------------------------------------------
# TwiML generation (for the webhook flow)
# ---------------------------------------------------------------------------

def generate_media_stream_twiml() -> str:
    """Return TwiML that tells Twilio to connect a Media Stream."""
    ws_url = _media_stream_ws_url()
    response = VoiceResponse()
    connect = Connect()
    stream = Stream(url=ws_url)
    connect.append(stream)
    response.append(connect)
    return str(response)


# ---------------------------------------------------------------------------
# Call management
# ---------------------------------------------------------------------------

def hangup_call(call_sid: str) -> None:
    """Hang up a live Twilio call."""
    try:
        client = get_twilio_client()
        client.calls(call_sid).update(status="completed")
        logger.info("Twilio call %s hung up", call_sid)
    except Exception as e:
        logger.error("Error hanging up Twilio call %s: %s", call_sid, e)


def fetch_call_status(call_sid: str) -> dict:
    """Fetch call details from the Twilio API."""
    try:
        client = get_twilio_client()
        call = client.calls(call_sid).fetch()
        return {
            "call_sid": call.sid,
            "status": call.status,
            "to": call.to,
            "from_": getattr(call, "from_", ""),
            "duration": call.duration,
        }
    except Exception as e:
        logger.error("Error fetching Twilio call %s: %s", call_sid, e)
        return {"call_sid": call_sid, "status": "unknown", "error": str(e)}
