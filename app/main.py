"""
Habla — FastAPI backend for real-time phone call translation.

Endpoints:
    REST
        POST /call               Initiate an outbound translated call
        POST /call/{sid}/end     End an active call
        GET  /call/{sid}/status  Get call status
        POST /twilio/webhook     Twilio webhook (returns TwiML)

    WebSocket
        WS /ws/{call_sid}           iOS app audio stream
        WS /twilio/media-stream     Twilio Media Streams
"""

import json
import logging

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import Response

from app.config import SERVER_HOST, SERVER_PORT, TWILIO_FROM_NUMBER
from app.models import CallRequest, CallResponse, CallStatusResponse, EndCallResponse
from app.call_manager import CallManager, CallStatus
from app.translation_bridge import TranslationBridge
from app.twilio_handler import (
    initiate_outbound_call,
    hangup_call,
    fetch_call_status,
    generate_media_stream_twiml,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App + shared state
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Habla",
    description="Real-time phone call translation powered by Amazon Nova 2 Sonic",
    version="0.1.0",
)
call_manager = CallManager()


# ===================================================================
# REST endpoints
# ===================================================================

@app.get("/")
async def health():
    return {"service": "habla", "status": "running"}


@app.post("/call", response_model=CallResponse)
async def create_call(req: CallRequest):
    """Initiate an outbound translated call to a Spanish phone number."""
    try:
        call_sid = initiate_outbound_call(req.to)
    except Exception as e:
        logger.error("Failed to initiate outbound call: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

    state = call_manager.create_call(call_sid, req.to, TWILIO_FROM_NUMBER)
    state.bridge = TranslationBridge(call_sid)

    return CallResponse(call_sid=call_sid, status=CallStatus.INITIATING)


@app.post("/call/{call_sid}/end", response_model=EndCallResponse)
async def end_call(call_sid: str):
    """Hang up and clean up an active call."""
    state = call_manager.get_call(call_sid)
    if not state:
        raise HTTPException(status_code=404, detail="Call not found")

    hangup_call(call_sid)
    await call_manager.cleanup_call(call_sid)
    return EndCallResponse(call_sid=call_sid, status=CallStatus.COMPLETED)


@app.get("/call/{call_sid}/status", response_model=CallStatusResponse)
async def get_call_status(call_sid: str):
    """Return current call status."""
    state = call_manager.get_call(call_sid)
    if state:
        return CallStatusResponse(
            call_sid=call_sid,
            status=state.status.value,
            to=state.to_number,
            from_=state.from_number,
        )

    twilio_info = fetch_call_status(call_sid)
    return CallStatusResponse(
        call_sid=call_sid,
        status=twilio_info.get("status", "unknown"),
        to=twilio_info.get("to", ""),
        from_=twilio_info.get("from_", ""),
    )


@app.post("/twilio/webhook")
async def twilio_webhook(request: Request):
    """
    Called by Twilio when the outbound call connects.

    Returns TwiML instructing Twilio to open a Media Stream WebSocket
    back to /twilio/media-stream.
    """
    twiml = generate_media_stream_twiml()
    return Response(content=twiml, media_type="text/xml")


# ===================================================================
# WebSocket — iOS app
# ===================================================================

@app.websocket("/ws/{call_sid}")
async def ios_websocket(ws: WebSocket, call_sid: str):
    """
    iOS app connects here to stream audio.

    Receives: raw PCM 16-bit 16 kHz binary frames (user's English speech)
    Sends:    raw PCM 16-bit 16 kHz binary frames (translated English audio)
    """
    await ws.accept()
    logger.info("[%s] iOS WebSocket connected", call_sid)

    state = call_manager.get_call(call_sid)
    if not state or not state.bridge:
        logger.error("[%s] No active call for iOS WebSocket", call_sid)
        await ws.close(code=4004, reason="Call not found")
        return

    state.ios_ws = ws
    bridge = state.bridge

    # Start the EN→ES Nova session
    try:
        await bridge.start_session_a(ws)
    except Exception as e:
        logger.error("[%s] Failed to start Session A: %s", call_sid, e)
        await ws.close(code=1011, reason="Translation session failed to start")
        return

    state.status = CallStatus.IN_PROGRESS

    # Main receive loop — forward iOS audio to Session A
    try:
        while True:
            data = await ws.receive_bytes()
            await bridge.handle_ios_audio(data)
    except WebSocketDisconnect:
        logger.info("[%s] iOS WebSocket disconnected", call_sid)
    except Exception as e:
        logger.error("[%s] iOS WebSocket error: %s", call_sid, e)
    finally:
        await call_manager.cleanup_call(call_sid)


# ===================================================================
# WebSocket — Twilio Media Streams
# ===================================================================

@app.websocket("/twilio/media-stream")
async def twilio_media_stream(ws: WebSocket):
    """
    Twilio connects here to stream phone call audio.

    Receives: JSON messages with base64-encoded mulaw audio
    Sends:    JSON media messages with base64-encoded mulaw audio
    """
    await ws.accept()
    logger.info("Twilio Media Stream WebSocket connected")

    call_sid: str | None = None
    stream_sid: str | None = None

    try:
        while True:
            raw = await ws.receive_text()
            data = json.loads(raw)
            event_type = data.get("event")

            # ── stream start
            if event_type == "start":
                stream_sid = data["start"]["streamSid"]
                call_sid = data["start"]["callSid"]
                logger.info(
                    "Twilio stream started  stream=%s  call=%s",
                    stream_sid,
                    call_sid,
                )

                state = call_manager.get_call(call_sid)
                if not state or not state.bridge:
                    logger.error("No call state for Twilio stream %s", call_sid)
                    break

                state.twilio_ws = ws
                state.twilio_stream_sid = stream_sid

                # Start the ES→EN Nova session
                await state.bridge.start_session_b(ws, stream_sid)

            # ── audio media
            elif event_type == "media":
                if not call_sid:
                    continue
                state = call_manager.get_call(call_sid)
                if state and state.bridge:
                    payload = data["media"]["payload"]
                    await state.bridge.handle_twilio_media(payload)

            # ── stream stop
            elif event_type == "stop":
                logger.info("Twilio stream stopped  call=%s", call_sid)
                break

            # ── connected (informational)
            elif event_type == "connected":
                logger.info("Twilio Media Stream event: connected")

    except WebSocketDisconnect:
        logger.info("Twilio Media Stream disconnected  call=%s", call_sid)
    except Exception as e:
        logger.error("Twilio Media Stream error: %s", e)
    finally:
        if call_sid:
            await call_manager.cleanup_call(call_sid)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=SERVER_HOST,
        port=SERVER_PORT,
        reload=True,
    )
