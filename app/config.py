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
# Nova 2 Sonic
# ---------------------------------------------------------------------------
NOVA_MODEL_ID = "amazon.nova-2-sonic-v1:0"
NOVA_VOICE_ID_EN = os.getenv("NOVA_VOICE_ID_EN", "matthew")
NOVA_VOICE_ID_ES = os.getenv("NOVA_VOICE_ID_ES", "lupe")

# Audio constants
INPUT_SAMPLE_RATE = 16000   # PCM input to Nova
OUTPUT_SAMPLE_RATE = 24000  # PCM output from Nova
TWILIO_SAMPLE_RATE = 8000   # Twilio mulaw sample rate

# ---------------------------------------------------------------------------
# Translation system prompts
# ---------------------------------------------------------------------------
EN_TO_ES_SYSTEM_PROMPT = (
    "You are a real-time voice translator. Listen to the user speaking in "
    "English and respond by saying the exact same message translated into "
    "natural, conversational Spanish. Do not add any commentary, greetings, "
    "or explanations. Only output the Spanish translation of what was said. "
    "Maintain the same tone and intent. If the user pauses, wait for them "
    "to continue."
)

ES_TO_EN_SYSTEM_PROMPT = (
    "You are a real-time voice translator. Listen to the user speaking in "
    "Spanish and respond by saying the exact same message translated into "
    "natural, conversational English. Do not add any commentary, greetings, "
    "or explanations. Only output the English translation of what was said. "
    "Maintain the same tone and intent. If the user pauses, wait for them "
    "to continue."
)
