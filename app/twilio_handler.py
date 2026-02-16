"""
Twilio voice integration — outbound calls and Media Streams TwiML.
"""

import logging

from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream

from app.config import (
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN,
    TWILIO_API_SID,
    TWILIO_API_SECRET,
    TWILIO_FROM_NUMBER,
    PUBLIC_URL,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Twilio REST client
# ---------------------------------------------------------------------------

def _get_client() -> Client:
    """Return a configured Twilio REST client."""
    if TWILIO_API_SID and TWILIO_API_SECRET:
        return Client(TWILIO_API_SID, TWILIO_API_SECRET, TWILIO_ACCOUNT_SID)
    return Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


# ---------------------------------------------------------------------------
# Outbound calling
# ---------------------------------------------------------------------------

def _media_stream_ws_url() -> str:
    """Derive the wss:// media-stream URL from PUBLIC_URL."""
    return (
        PUBLIC_URL
        .replace("https://", "wss://")
        .replace("http://", "ws://")
        + "/twilio/media-stream"
    )


def initiate_outbound_call(to_number: str) -> str:
    """
    Place an outbound PSTN call via Twilio.

    When the callee answers, Twilio will open a Media Streams WebSocket
    back to our /twilio/media-stream endpoint.

    Returns the Twilio Call SID.
    """
    client = _get_client()
    ws_url = _media_stream_ws_url()

    twiml_xml = (
        "<Response>"
        "<Connect>"
        f'<Stream url="{ws_url}" />'
        "</Connect>"
        "</Response>"
    )

    call = client.calls.create(
        to=to_number,
        from_=TWILIO_FROM_NUMBER,
        twiml=twiml_xml,
    )
    logger.info("Twilio outbound call created: sid=%s  to=%s", call.sid, to_number)
    return call.sid


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
        client = _get_client()
        client.calls(call_sid).update(status="completed")
        logger.info("Twilio call %s hung up", call_sid)
    except Exception as e:
        logger.error("Error hanging up Twilio call %s: %s", call_sid, e)


def fetch_call_status(call_sid: str) -> dict:
    """Fetch call details from the Twilio API."""
    try:
        client = _get_client()
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
