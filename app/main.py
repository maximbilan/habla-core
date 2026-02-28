"""
Habla — FastAPI backend for real-time phone call translation.

Endpoints:
    REST
        GET  /translation/languages  List supported translation languages
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
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream

from app.config import SERVER_HOST, SERVER_PORT, PUBLIC_URL
from app.models import CallRequest, CallResponse, CallStatusResponse, EndCallResponse
from app.call_manager import CallManager, CallStatus
from app.translation_bridge import TranslationBridge
from app.language_support import (
    DEFAULT_SOURCE_LANGUAGE,
    DEFAULT_TARGET_LANGUAGE,
    normalize_voice_gender,
    resolve_supported_language,
    resolve_translation_languages,
    supported_languages_payload,
)
from app.agent import AgentCallConfig, agent_calls, initiate_agent_outbound_call
from app.agent.goal_tracker import normalize_goal_required_fields
from app.caller_id.router import router as caller_id_router
from app.request_auth import (
    auth_enabled,
    optional_device_id,
    require_authorized_request,
    require_authorized_websocket,
)
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
app.include_router(caller_id_router, dependencies=[Depends(require_authorized_request)])

if auth_enabled():
    logger.info("Backend request auth enabled for iOS requests")
else:
    logger.warning("Backend request auth disabled (HABLA_SECRET is not set)")


class AgentCallRequest(BaseModel):
    class GoalSchema(BaseModel):
        objective: str = ""
        required_fields: list[str] = Field(default_factory=list)

    to: str
    from_: str | None = Field(default=None, alias="from")
    prompt: str
    user_name: str = "Caller"
    language: str = DEFAULT_TARGET_LANGUAGE
    voice_gender: str | None = None
    goal_schema: GoalSchema | None = None

    model_config = {"populate_by_name": True}


class AgentCallResponse(BaseModel):
    call_sid: str
    status: str


class TranslationLanguage(BaseModel):
    code: str
    name: str
    locale: str
    default_voice_id: str


class TranslationLanguageListResponse(BaseModel):
    default_source_language: str
    default_target_language: str
    supported_languages: list[TranslationLanguage]


def _extract_form_field_from_urlencoded_body(body: bytes, field_name: str) -> str | None:
    if not body:
        return None

    try:
        parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True)
    except UnicodeDecodeError:
        return None

    values = parsed.get(field_name)
    if not values:
        return None
    return values[-1]


async def _extract_twilio_call_sid(request: Request, fallback_call_sid: str) -> str:
    content_type = (request.headers.get("content-type") or "").lower()

    if "application/x-www-form-urlencoded" in content_type:
        call_sid = _extract_form_field_from_urlencoded_body(await request.body(), "CallSid")
        return call_sid or fallback_call_sid

    # Fallback for environments where form parsing middleware is present.
    try:
        form = await request.form()
    except AssertionError:
        return fallback_call_sid
    except Exception:
        logger.exception("Failed to parse Twilio webhook form body")
        return fallback_call_sid

    call_sid = form.get("CallSid")
    return str(call_sid) if call_sid else fallback_call_sid


def _should_process_agent_media_track(track: str | None) -> bool:
    if not track:
        return True
    normalized = track.strip().lower()
    return normalized not in {"outbound", "outbound_track"}


def _normalize_goal_schema(req: AgentCallRequest) -> tuple[str, list[str]]:
    if not req.goal_schema:
        return "", []

    objective = (req.goal_schema.objective or "").strip() or req.prompt.strip()
    required_fields = normalize_goal_required_fields(req.goal_schema.required_fields)
    return objective, required_fields


# ===================================================================
# REST endpoints
# ===================================================================

@app.get("/")
async def health():
    return {"service": "habla", "status": "running"}


@app.get(
    "/translation/languages",
    response_model=TranslationLanguageListResponse,
    dependencies=[Depends(require_authorized_request)],
)
async def get_translation_languages():
    return TranslationLanguageListResponse(
        default_source_language=DEFAULT_SOURCE_LANGUAGE,
        default_target_language=DEFAULT_TARGET_LANGUAGE,
        supported_languages=supported_languages_payload(),
    )


@app.post("/call", response_model=CallResponse, dependencies=[Depends(require_authorized_request)])
async def create_call(
    req: CallRequest,
    device_id: str | None = Depends(optional_device_id),
):
    """Initiate an outbound translated call."""
    try:
        source_language, target_language = resolve_translation_languages(
            req.source_language,
            req.target_language,
        )
        voice_gender = normalize_voice_gender(req.voice_gender)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    try:
        call_sid, caller_id = initiate_outbound_call(
            req.to,
            req.from_,
            device_id=device_id,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to initiate outbound call: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

    state = call_manager.create_call(
        call_sid,
        req.to,
        caller_id,
        source_language=source_language,
        target_language=target_language,
    )
    state.bridge = TranslationBridge(
        call_sid=call_sid,
        source_language=source_language,
        target_language=target_language,
        voice_gender=voice_gender,
    )

    return CallResponse(call_sid=call_sid, status=CallStatus.INITIATING)


@app.post(
    "/call/{call_sid}/end",
    response_model=EndCallResponse,
    dependencies=[Depends(require_authorized_request)],
)
async def end_call(call_sid: str):
    """Hang up and clean up an active call."""
    state = call_manager.get_call(call_sid)
    if not state:
        raise HTTPException(status_code=404, detail="Call not found")

    hangup_call(call_sid)
    await call_manager.cleanup_call(call_sid)
    return EndCallResponse(call_sid=call_sid, status=CallStatus.COMPLETED)


@app.get(
    "/call/{call_sid}/status",
    response_model=CallStatusResponse,
    dependencies=[Depends(require_authorized_request)],
)
async def get_call_status(call_sid: str):
    """Return current call status."""
    state = call_manager.get_call(call_sid)
    if state:
        return CallStatusResponse(
            call_sid=call_sid,
            status=state.status.value,
            to=state.to_number,
            from_=state.from_number,
            source_language=state.source_language,
            target_language=state.target_language,
        )

    twilio_info = fetch_call_status(call_sid)
    return CallStatusResponse(
        call_sid=call_sid,
        status=twilio_info.get("status", "unknown"),
        to=twilio_info.get("to", ""),
        from_=twilio_info.get("from_", ""),
        source_language=DEFAULT_SOURCE_LANGUAGE,
        target_language=DEFAULT_TARGET_LANGUAGE,
    )


@app.post("/twilio/webhook")
async def twilio_webhook():
    """
    Called by Twilio when the outbound call connects.

    Returns TwiML instructing Twilio to open a Media Stream WebSocket
    back to /twilio/media-stream.
    """
    twiml = generate_media_stream_twiml()
    return Response(content=twiml, media_type="text/xml")


# ===================================================================
# REST endpoints — Agent Mode
# ===================================================================

@app.post("/agent/call", response_model=AgentCallResponse, dependencies=[Depends(require_authorized_request)])
async def create_agent_call(
    req: AgentCallRequest,
    device_id: str | None = Depends(optional_device_id),
):
    language = resolve_supported_language(req.language)
    if not language:
        raise HTTPException(status_code=422, detail=f"Unsupported language '{req.language}'")
    try:
        voice_gender = normalize_voice_gender(req.voice_gender)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    goal_objective, goal_required_fields = _normalize_goal_schema(req)

    try:
        call_sid, caller_id = initiate_agent_outbound_call(
            req.to,
            req.from_,
            device_id=device_id,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to initiate agent outbound call: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

    agent_calls.create(
        call_sid=call_sid,
        config=AgentCallConfig(
            to_number=req.to,
            from_number=caller_id,
            prompt=req.prompt,
            user_name=req.user_name,
            language=language.code,
            voice_gender=voice_gender,
            goal_objective=goal_objective,
            goal_required_fields=goal_required_fields,
        ),
    )
    return AgentCallResponse(call_sid=call_sid, status="initiating")


@app.post("/agent/call/{call_sid}/end", dependencies=[Depends(require_authorized_request)])
async def end_agent_call(call_sid: str):
    manager = agent_calls.get(call_sid)
    if not manager:
        raise HTTPException(status_code=404, detail="Agent call not found")
    await manager.end_call()
    return {"status": "ended"}


@app.get("/agent/call/{call_sid}/status", dependencies=[Depends(require_authorized_request)])
async def get_agent_call_status(call_sid: str):
    manager = agent_calls.get(call_sid)
    if not manager:
        raise HTTPException(status_code=404, detail="Agent call not found")
    return manager.status_payload()


@app.post("/agent/twilio/webhook/{call_sid}")
async def agent_twilio_webhook(call_sid: str, request: Request):
    twilio_call_sid = await _extract_twilio_call_sid(request, call_sid)
    ws_base = PUBLIC_URL.replace("https://", "wss://").replace("http://", "ws://")

    response = VoiceResponse()
    connect = Connect()
    stream = Stream(url=f"{ws_base}/agent/twilio/media-stream/{twilio_call_sid}")
    connect.append(stream)
    response.append(connect)
    return Response(content=str(response), media_type="application/xml")


# ===================================================================
# WebSocket — iOS app
# ===================================================================

@app.websocket("/ws/{call_sid}")
async def ios_websocket(ws: WebSocket, call_sid: str):
    """
    iOS app connects here to stream audio.

    Receives: raw PCM 16-bit 16 kHz binary frames (source-language speech)
    Sends:    raw PCM 16-bit 16 kHz binary frames (translated source-language audio)
    """
    require_authorized_websocket(ws)
    await ws.accept()
    logger.info("[%s] iOS WebSocket connected", call_sid)

    state = call_manager.get_call(call_sid)
    if not state or not state.bridge:
        logger.error("[%s] No active call for iOS WebSocket", call_sid)
        await ws.close(code=4004, reason="Call not found")
        return

    state.ios_ws = ws
    bridge = state.bridge

    # Start Session A (source→target)
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

                # Start Session B (target→source)
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
        if "not connected" in str(e).lower():
            logger.info("Twilio Media Stream closed externally  call=%s", call_sid)
        else:
            logger.error("Twilio Media Stream error: %s", e)
    finally:
        if call_sid:
            await call_manager.cleanup_call(call_sid)


# ===================================================================
# WebSocket — Agent Mode
# ===================================================================

@app.websocket("/agent/ws/{call_sid}")
async def agent_ios_websocket(ws: WebSocket, call_sid: str):
    require_authorized_websocket(ws)
    await ws.accept()
    manager = agent_calls.get(call_sid)
    if not manager:
        await ws.close(code=4004, reason="Agent call not found")
        return

    await manager.attach_ios_websocket(ws)

    try:
        while True:
            raw = await ws.receive_text()
            data = json.loads(raw)
            msg_type = data.get("type")

            if msg_type == "instruction":
                await manager.inject_instruction(str(data.get("text", "")))
            elif msg_type == "end_conversation":
                await manager.end_conversation(str(data.get("text", "")))
            elif msg_type == "end_call":
                await manager.end_call()
    except WebSocketDisconnect:
        manager.detach_ios_websocket(ws)
    except Exception as e:
        logger.error("[%s] Agent iOS WebSocket error: %s", call_sid, e)
        manager.detach_ios_websocket(ws)


@app.websocket("/agent/twilio/media-stream/{call_sid}")
async def agent_twilio_media_stream(ws: WebSocket, call_sid: str):
    await ws.accept()
    manager = agent_calls.get(call_sid)
    stream_call_sid = call_sid

    try:
        while True:
            data = json.loads(await ws.receive_text())
            event_type = data.get("event")

            if event_type == "start":
                stream_call_sid = data.get("start", {}).get("callSid", call_sid)
                manager = agent_calls.get(stream_call_sid) or manager
                if not manager:
                    logger.error("No manager found for agent call %s", stream_call_sid)
                    break

                stream_sid = data.get("start", {}).get("streamSid", "")
                await manager.on_twilio_start(ws, stream_sid)

            elif event_type == "media" and manager:
                track = data.get("media", {}).get("track")
                if not _should_process_agent_media_track(track):
                    continue
                payload = data.get("media", {}).get("payload")
                if payload:
                    await manager.handle_twilio_media(payload)

            elif event_type == "stop" and manager:
                await manager.end_call()
                break
    except WebSocketDisconnect:
        if manager:
            await manager.end_call()
    except Exception as e:
        logger.error("[%s] Agent Twilio WS error: %s", stream_call_sid, e)
        if manager:
            await manager.end_call()


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
