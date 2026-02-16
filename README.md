# Habla — Real-Time Phone Call Translation

Habla lets an English speaker make phone calls to Spanish phone numbers (schools, businesses, delivery services, etc.) with real-time bidirectional speech translation powered by **Amazon Nova 2 Sonic**.

Built for the **Amazon Nova AI Hackathon**.

## Architecture

```
┌─────────────┐     WebSocket      ┌──────────────────┐    Twilio Media     ┌─────────┐     PSTN Call      ┌──────────────┐
│   iOS App   │◄──────────────────►│  Python Backend   │◄───Streams (WS)───►│ Twilio  │◄────────────────►│ Spanish Phone │
│ (English    │   (PCM 16kHz)      │                   │   (mulaw 8kHz)     │  Voice  │    (regular       │  (school,     │
│  speaker)   │                    │  Two Nova 2 Sonic │                    │   API   │     phone call)   │   business)   │
│             │                    │  sessions running │                    │         │                   │               │
└─────────────┘                    └──────────────────┘                    └─────────┘                   └──────────────┘
```

**Two Nova 2 Sonic sessions run per call:**
- **Session A (EN→ES):** iOS mic → Nova translates → Spanish audio to phone speaker
- **Session B (ES→EN):** Phone mic → Nova translates → English audio to iOS speaker

## Quick Start

### Prerequisites

- Python 3.11+
- AWS credentials with Bedrock access (us-east-1)
- Twilio account with a phone number
- `ngrok` or `localtunnel` for exposing your local server

### Setup

```bash
# Create virtualenv
python -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your AWS and Twilio credentials
```

### Run

```bash
# Start the server
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# In another terminal, expose via ngrok
ngrok http 8000
# Copy the https URL and set it as PUBLIC_URL in .env
```

### Make a Call

```bash
# Initiate a translated call
curl -X POST http://localhost:8000/call \
  -H "Content-Type: application/json" \
  -d '{"to": "+34612345678"}'

# Returns: {"call_sid": "CA...", "status": "initiating"}
```

Then connect the iOS app WebSocket to `ws://localhost:8000/ws/{call_sid}` and start streaming PCM audio.

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/call` | Initiate an outbound translated call |
| `POST` | `/call/{sid}/end` | End an active call |
| `GET` | `/call/{sid}/status` | Get call status |
| `POST` | `/twilio/webhook` | Twilio webhook (returns TwiML) |
| `WS` | `/ws/{call_sid}` | iOS app audio WebSocket (binary PCM 16kHz) |
| `WS` | `/twilio/media-stream` | Twilio Media Streams WebSocket |

## Docker

```bash
cp .env.example .env
# Edit .env
docker compose up --build
```

## Project Structure

```
habla-core/
├── app/
│   ├── main.py                 # FastAPI app, all endpoints
│   ├── config.py               # Environment variables, constants
│   ├── models.py               # Pydantic request/response models
│   ├── call_manager.py         # Active call state registry
│   ├── nova_sonic.py           # Nova 2 Sonic bidirectional streaming client
│   ├── twilio_handler.py       # Twilio REST API + TwiML generation
│   ├── audio_utils.py          # mulaw↔PCM conversion, resampling
│   └── translation_bridge.py   # Orchestrates both Nova sessions + audio routing
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── .env.example
```

## Tech Stack

- **Python 3.12** + FastAPI + uvicorn
- **Amazon Nova 2 Sonic** (`amazon.nova-2-sonic-v1:0`) via `InvokeModelWithBidirectionalStream`
- **Twilio** Programmable Voice + Media Streams
- **asyncio** for concurrent bidirectional audio streaming
