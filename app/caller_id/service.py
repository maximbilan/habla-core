from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

from fastapi import HTTPException

from app.config import (
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN,
    TWILIO_API_SID,
    TWILIO_API_SECRET,
    TWILIO_FROM_NUMBER,
)

SPAIN_DELIVERABILITY_NOTE = (
    "Spain note: personal calls with verified Spanish mobile caller IDs usually work, "
    "but some carriers may filter or rewrite caller ID. If this happens, use the Twilio "
    "number. Spanish landlines (9XX) may have better deliverability."
)


def get_twilio_client() -> Client:
    if TWILIO_API_SID and TWILIO_API_SECRET:
        return Client(TWILIO_API_SID, TWILIO_API_SECRET, TWILIO_ACCOUNT_SID)
    return Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


def resolve_outbound_caller_id(client: Client, from_number: str | None) -> str:
    """Return verified caller ID to use for outbound calls."""
    if not from_number:
        return TWILIO_FROM_NUMBER

    existing = client.outgoing_caller_ids.list(phone_number=from_number)
    if not existing:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Phone number {from_number} is not verified. "
                "Verify it first via POST /caller-id/verify/start"
            ),
        )
    return from_number


def create_outbound_call(
    *,
    to_number: str,
    from_number: str | None = None,
    webhook_url: str | None = None,
    twiml: str | None = None,
    method: str = "POST",
) -> tuple[str, str]:
    """Create a Twilio outbound call with optional custom caller ID."""
    client = get_twilio_client()
    caller_id = resolve_outbound_caller_id(client, from_number)

    create_kwargs: dict = {"to": to_number, "from_": caller_id}
    if webhook_url:
        create_kwargs.update({"url": webhook_url, "method": method})
    elif twiml:
        create_kwargs["twiml"] = twiml
    else:
        raise ValueError("Either webhook_url or twiml must be provided")

    call = client.calls.create(**create_kwargs)
    return call.sid, caller_id


def parse_twilio_error(exc: Exception) -> str:
    if isinstance(exc, TwilioRestException) and exc.msg:
        return exc.msg
    return str(exc) or "Unknown Twilio error"
