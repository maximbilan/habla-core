# Habla Core

OpenAI Realtime backend for Habla app.

This service supports both iOS modes:

- **Live Call Mode**: low-latency bidirectional phone-call translation
- **Agent Mode**: autonomous caller agent with transcript + verified-facts signals

System architecture and sequence diagrams: [`architecture.md`](architecture.md)  
Direct Agent Mode flow diagram: [`architecture.md#61-agent-mode-runtime-sequence`](architecture.md#61-agent-mode-runtime-sequence).

## Current Implementation Summary

### Live Call Mode (fast audio path)

- Uses two OpenAI `gpt-realtime-translate` sessions per call:
  - iOS -> callee language (to Twilio/PSTN)
  - callee -> iOS language (back to app)
- Streams audio in both directions continuously
- Optimized for latency: translation call mode focuses on audio forwarding

### Agent Mode

- Twilio call orchestration with `gpt-realtime-2` model-driven agent conversation.
- WebSocket events for:
  - call status
  - agent status (`listening/thinking/speaking`)
  - transcript and transcript updates
  - critical confirmations
  - verified facts summary

### Caller ID Isolation

- Caller ID verification/list/delete endpoints are provided
- Ownership is enforced per device via `X-Habla-Device-ID`
- Shared ownership state is delegated to `habla-accounts` (`HABLA_ACCOUNTS_*`)

## API Surface

### Translation

- `GET /`
- `GET /translation/languages`
- `POST /call`
- `POST /call/{sid}/end`
- `GET /call/{sid}/status`
- `POST /twilio/webhook` (compatibility TwiML endpoint)
- `WS /ws/{call_sid}`
- `WS /twilio/media-stream`

### Agent

- `POST /agent/call`
- `POST /agent/call/{call_sid}/end`
- `GET /agent/call/{call_sid}/status`
- `POST /agent/twilio/webhook/{call_sid}`
- `WS /agent/ws/{call_sid}`
- `WS /agent/twilio/media-stream/{call_sid}`

### Caller ID

- `POST /caller-id/verify/start`
- `GET /caller-id/verify/status/{phone_number}`
- `GET /caller-id/list`
- `DELETE /caller-id/{sid}`

## Request Authentication

If `HABLA_SECRET` is set, iOS-facing REST + WS routes require:

- `Authorization: HMAC_SHA256(HABLA_SECRET, HABLA_APP_BUNDLE_ID)`

Caller ID ownership-sensitive routes also require:

- `X-Habla-Device-ID`

## Supported Languages

- `en-US`, `en-GB`, `en-AU`, `en-IN`
- `es-US`, `fr-FR`, `de-DE`, `it-IT`, `pt-BR`, `hi-IN`

## Local Development

### 1) Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# test-only deps
pip install pytest httpx
```

### 2) Configure

```bash
cp .env.example .env
```

Required groups:

- `OPENAI_API_KEY`
- Twilio credentials + `TWILIO_FROM_NUMBER`
- `PUBLIC_URL` reachable by Twilio

Optional/conditional:

- `HABLA_SECRET`, `HABLA_APP_BUNDLE_ID`
- `HABLA_ACCOUNTS_BASE_URL`, `HABLA_ACCOUNTS_SERVICE_TOKEN`, `HABLA_ACCOUNTS_TIMEOUT_SECONDS`
- `OPENAI_REALTIME_TRANSLATE_MODEL`, `OPENAI_REALTIME_AGENT_MODEL`
- `OPENAI_REALTIME_AGENT_VOICE`, `OPENAI_REALTIME_AGENT_TRANSCRIPTION_MODEL`
- `OPENAI_TEXT_TRANSLATION_MODEL`

### 3) Run

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

## Tests

```bash
python -m compileall app tests
PYTHONPATH=. pytest -q
```

## Deployment (EC2)

Main branch deploy is handled by `.github/workflows/deploy-ec2.yml`:

- runs syntax validation
- rsyncs source to EC2
- installs dependencies in server venv
- restarts `habla-core` systemd service
- runs local health check (`http://127.0.0.1:8000/`)

Required GitHub variables/secrets are defined in the workflow (`EC2_*`, `EC2_SSH_PRIVATE_KEY`).

## Repository Layout

```text
app/
  main.py
  config.py
  models.py
  call_manager.py
  translation_bridge.py
  openai_realtime.py
  audio_utils.py
  request_auth.py
  twilio_handler.py
  language_support.py
  caller_id/
  agent/
```
