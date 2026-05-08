import os
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Twilio
# ---------------------------------------------------------------------------
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER", "")
TWILIO_API_SID = os.getenv("TWILIO_API_SID", "")
TWILIO_API_SECRET = os.getenv("TWILIO_API_SECRET", "")

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
SERVER_HOST = os.getenv("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.getenv("SERVER_PORT", "8000"))
PUBLIC_URL = os.getenv("PUBLIC_URL", "http://localhost:8000")

# ---------------------------------------------------------------------------
# Habla Accounts service (shared caller-id ownership)
# ---------------------------------------------------------------------------
HABLA_ACCOUNTS_BASE_URL = os.getenv("HABLA_ACCOUNTS_BASE_URL", "").strip()
HABLA_ACCOUNTS_SERVICE_TOKEN = os.getenv("HABLA_ACCOUNTS_SERVICE_TOKEN", "").strip()
HABLA_ACCOUNTS_TIMEOUT_SECONDS = float(os.getenv("HABLA_ACCOUNTS_TIMEOUT_SECONDS", "5"))

# ---------------------------------------------------------------------------
# Request auth (iOS -> backend)
# ---------------------------------------------------------------------------
# Authorization token format:
#   HMAC-SHA256(HABLA_SECRET, HABLA_APP_BUNDLE_ID)
#
# Auth is enabled when HABLA_SECRET is set.
HABLA_SECRET = os.getenv("HABLA_SECRET", "").strip()
HABLA_APP_BUNDLE_ID = os.getenv("HABLA_APP_BUNDLE_ID", "com.maximbilan.habla-ios").strip()

# ---------------------------------------------------------------------------
# OpenAI Realtime
# ---------------------------------------------------------------------------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_REALTIME_TRANSLATE_MODEL = os.getenv(
    "OPENAI_REALTIME_TRANSLATE_MODEL",
    "gpt-realtime-translate",
).strip()
OPENAI_REALTIME_AGENT_MODEL = os.getenv(
    "OPENAI_REALTIME_AGENT_MODEL",
    "gpt-realtime-2",
).strip()
OPENAI_REALTIME_AGENT_VOICE = os.getenv(
    "OPENAI_REALTIME_AGENT_VOICE",
    "cedar",
).strip()
OPENAI_REALTIME_AGENT_TRANSCRIPTION_MODEL = os.getenv(
    "OPENAI_REALTIME_AGENT_TRANSCRIPTION_MODEL",
    "gpt-4o-mini-transcribe",
).strip()
OPENAI_TEXT_TRANSLATION_MODEL = os.getenv(
    "OPENAI_TEXT_TRANSLATION_MODEL",
    "gpt-5.4-mini",
).strip()
OPENAI_AUDIO_SAMPLE_RATE = int(os.getenv("OPENAI_AUDIO_SAMPLE_RATE", "24000"))

# Audio constants
INPUT_SAMPLE_RATE = 16000
OUTPUT_SAMPLE_RATE = 24000
