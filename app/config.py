import os
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# AWS
# ---------------------------------------------------------------------------
# AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY are read from the environment
# automatically by the Smithy SDK's EnvironmentCredentialsResolver in
# nova_sonic.py.  load_dotenv() above ensures .env values are available.
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")

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
# Request auth (iOS -> backend)
# ---------------------------------------------------------------------------
# Authorization token format:
#   HMAC-SHA256(HABLA_SECRET, HABLA_APP_BUNDLE_ID)
#
# Auth is enabled when HABLA_SECRET is set.
HABLA_SECRET = os.getenv("HABLA_SECRET", "").strip()
HABLA_APP_BUNDLE_ID = os.getenv("HABLA_APP_BUNDLE_ID", "com.maximbilan.habla-ios").strip()

# ---------------------------------------------------------------------------
# Nova 2 Sonic
# ---------------------------------------------------------------------------
NOVA_MODEL_ID = "amazon.nova-2-sonic-v1:0"
# Keep EN/ES voice overrides for backwards compatibility.
NOVA_VOICE_ID_EN = os.getenv("NOVA_VOICE_ID_EN", "matthew")
NOVA_VOICE_ID_ES = os.getenv("NOVA_VOICE_ID_ES", "lupe")

# Audio constants
INPUT_SAMPLE_RATE = 16000   # PCM input to Nova
OUTPUT_SAMPLE_RATE = 24000  # PCM output from Nova
