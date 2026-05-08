# Architecture

## 1. Scope

- Live call translation APIs and media bridges
- Agent mode orchestration APIs/websockets
- Caller-ID verification APIs with device-scoped ownership enforcement

## 2. System Context

```mermaid
flowchart LR
    iOS[iOS Client] <-- REST + WS --> API[FastAPI app.main]
    API <-- Twilio Voice + Media Streams --> Twilio[(Twilio)]
    API <-- Realtime WebSocket --> OpenAI[(OpenAI Realtime)]
    API <-- Ownership API --> Accounts[habla-accounts]
```

## 3. Process Architecture

Main modules:

- `app/main.py`: route definitions and WS endpoints
- `app/call_manager.py`: in-memory translation call registry
- `app/translation_bridge.py`: dual-session audio routing pipeline
- `app/openai_realtime.py`: OpenAI realtime translation session wrapper
- `app/agent/*`: agent-mode lifecycle, transcript, critical-info tracking
- `app/caller_id/*`: Twilio caller-id + ownership integration
- `app/request_auth.py`: REST/WS authorization and device-id helpers

## 4. Live Call Translation Design

### 4.1 Dual-session model

Per active translation call, `TranslationBridge` runs two model sessions:

- Session A: iOS speech (source) -> target-language audio -> Twilio callee
- Session B: Twilio callee speech (target) -> source-language audio -> iOS

### 4.2 Audio and transport path

```mermaid
sequenceDiagram
    participant iOS
    participant Core as habla-core
    participant OaiA as OpenAI Translate A
    participant OaiB as OpenAI Translate B
    participant Twilio
    participant Callee

    iOS->>Core: WS /ws/{call_sid} PCM16@16k
    Core->>OaiA: PCM16@24k audio input
    OaiA-->>Core: translated PCM16@24k output
    Core->>Twilio: media stream mulaw@8k
    Twilio->>Callee: PSTN audio

    Callee->>Twilio: PSTN speech
    Twilio-->>Core: WS /twilio/media-stream mulaw@8k
    Core->>OaiB: PCM16@24k audio input
    OaiB-->>Core: translated PCM16@24k output
    Core-->>iOS: PCM16@16k
```

### 4.3 Latency/queue design

`translation_bridge.py` uses bounded queues and drop-oldest behavior under pressure:

- input queues per session
- output queues for Twilio and iOS sinks

This keeps end-to-end latency bounded during burst/backpressure scenarios.

It also logs per-direction latency checkpoints:

- ingress -> model send
- model send -> first output audio
- first output audio -> websocket send

## 5. Translation Call Lifecycle

1. `POST /call` validates languages and voice gender
2. Backend initiates Twilio outbound call (`twilio_handler.initiate_outbound_call`) with inline TwiML that opens `/twilio/media-stream`
3. `CallManager` creates `CallState` with `TranslationBridge`
4. iOS connects `WS /ws/{call_sid}` and starts session A
5. Twilio connects `WS /twilio/media-stream` and starts session B
6. Audio flows bidirectionally until hangup/disconnect
7. `cleanup_call` closes bridge tasks/sessions and websockets (idempotent lock)

`POST /twilio/webhook` remains available as a compatibility TwiML endpoint, but the primary outbound path currently sends TwiML directly in call creation.

## 6. Agent Mode Architecture

Agent mode is implemented independently from translation call bridge (`app/agent/*`).

Core components:

- `AgentCallManager`: state machine + orchestration
- `AgentOpenAIRealtimeSession`: `gpt-realtime-2` session for autonomous dialog
- `TranscriptService`: transcript + async EN translation
- `CriticalInfoTracker`: high-risk extraction, confirmations, verified summary

iOS agent WS receives structured events (`status`, `agent_status`, transcript events, critical confirmation, verified summary).

### 6.1 Agent Mode Runtime Sequence

```mermaid
sequenceDiagram
    participant iOS as iOS Client
    participant API as FastAPI (app.main)
    participant Twilio as Twilio Voice + Media Streams
    participant Manager as AgentCallManager
    participant OpenAI as AgentOpenAIRealtimeSession (gpt-realtime-2)
    participant Transcript as TranscriptService
    participant FactTracker as CriticalInfoTracker

    iOS->>API: POST /agent/call
    API->>Twilio: create outbound call
    API->>Manager: agent_calls.create(call_sid, config)

    Twilio->>API: POST /agent/twilio/webhook/{call_sid}
    API-->>Twilio: TwiML Connect/Stream
    Twilio->>API: WS /agent/twilio/media-stream/{call_sid}
    API->>Manager: on_twilio_start(stream_sid)
    Manager->>OpenAI: ensure model session

    Twilio->>API: media payload (mulaw 8k)
    API->>Manager: handle_twilio_media(payload)
    Manager->>OpenAI: send_audio(PCM16@24k)
    OpenAI-->>Manager: audio deltas + transcript/status events
    Manager-->>Twilio: agent audio (mulaw 8k)

    iOS->>API: WS /agent/ws/{call_sid}
    iOS->>API: instruction / end_conversation / end_call
    API->>Manager: inject_instruction(...) / end_call()
    Manager->>Transcript: add entry + async translate_to_english()
    Manager->>FactTracker: observe_text() / observe_translation_pair()
    Manager-->>iOS: status + agent_status + transcript + confirmations + verified summary
```

## 7. Caller-ID and Ownership Architecture

Caller-id flow combines Twilio verified outgoing caller-ids with ownership claims in `habla-accounts`:

- verify/start or verify/status confirms Twilio side
- claim SID to `X-Habla-Device-ID`
- list returns Twilio caller-ids filtered by owned SIDs
- delete requires ownership match and removes both Twilio and claim record

This prevents one device from using another device's verified caller-id mapping.

## 8. Security Model

### 8.1 Request auth

If `HABLA_SECRET` is configured:

- iOS REST + iOS websocket routes require `Authorization`
- expected token: `HMAC_SHA256(HABLA_SECRET, HABLA_APP_BUNDLE_ID)` (raw token or `Bearer <token>`)

Twilio webhook/media endpoints are intentionally unauthenticated and validated by Twilio call context.

### 8.2 Device-scoped operations

Caller-id ownership-sensitive endpoints require:

- `X-Habla-Device-ID`

## 9. Deployment Architecture (EC2)

Current CI deploy model (`.github/workflows/deploy-ec2.yml`):

- push to `main` triggers syntax validation
- source sync to EC2 via `rsync`
- dependency installation in server venv
- `systemctl restart habla-core`
- local health probe on `127.0.0.1:8000`

Runtime state is in-memory for active calls; process restart drops active session state.

## 10. Operational Considerations

- Horizontal scale requires externalized call/session state or sticky routing
- Twilio media streams and iOS WS must land on same process owning the call state
- Bounded queue strategy prioritizes recency over completeness during overload
- Agent mode contains richer logic and higher CPU/network cost than translation call mode
