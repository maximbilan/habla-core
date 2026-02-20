import urllib.parse

from twilio.base.exceptions import TwilioRestException
from fastapi import APIRouter

from app.caller_id.models import (
    CallerIdDeleteResponse,
    CallerIdEntry,
    CallerIdListResponse,
    CallerIdStatusResponse,
    CallerIdVerifyRequest,
    CallerIdVerifyResponse,
)
from app.caller_id.service import (
    SPAIN_DELIVERABILITY_NOTE,
    get_twilio_client,
    parse_twilio_error,
)

router = APIRouter(prefix="/caller-id", tags=["caller-id"])


@router.post("/verify/start", response_model=CallerIdVerifyResponse)
async def start_verification(request: CallerIdVerifyRequest):
    client = get_twilio_client()

    try:
        existing = client.outgoing_caller_ids.list(phone_number=request.phone_number)
        if existing:
            return CallerIdVerifyResponse(
                status="already_verified",
                phone_number=request.phone_number,
                note=SPAIN_DELIVERABILITY_NOTE,
            )

        validation_request = client.validation_requests.create(
            phone_number=request.phone_number,
            friendly_name=request.friendly_name or request.phone_number,
        )
        return CallerIdVerifyResponse(
            status="verification_started",
            phone_number=request.phone_number,
            validation_code=validation_request.validation_code,
            call_sid=validation_request.call_sid,
            note=SPAIN_DELIVERABILITY_NOTE,
        )
    except TwilioRestException as exc:
        return CallerIdVerifyResponse(
            status="error",
            phone_number=request.phone_number,
            message=(
                "Unable to verify this number. "
                f"Twilio error: {parse_twilio_error(exc)}"
            ),
            note=SPAIN_DELIVERABILITY_NOTE,
        )


@router.get("/verify/status/{phone_number}", response_model=CallerIdStatusResponse)
async def check_status(phone_number: str):
    client = get_twilio_client()
    decoded_number = urllib.parse.unquote(phone_number)

    existing = client.outgoing_caller_ids.list(phone_number=decoded_number)
    if existing:
        return CallerIdStatusResponse(
            phone_number=decoded_number,
            verified=True,
            friendly_name=existing[0].friendly_name,
            sid=existing[0].sid,
            note=SPAIN_DELIVERABILITY_NOTE,
        )

    return CallerIdStatusResponse(
        phone_number=decoded_number,
        verified=False,
        note=SPAIN_DELIVERABILITY_NOTE,
    )


@router.get("/list", response_model=CallerIdListResponse)
async def list_all():
    client = get_twilio_client()
    caller_ids = client.outgoing_caller_ids.list()

    return CallerIdListResponse(
        caller_ids=[
            CallerIdEntry(
                phone_number=cid.phone_number,
                friendly_name=cid.friendly_name,
                sid=cid.sid,
                date_created=cid.date_created.isoformat() if cid.date_created else None,
            )
            for cid in caller_ids
        ],
        note=SPAIN_DELIVERABILITY_NOTE,
    )


@router.delete("/{sid}", response_model=CallerIdDeleteResponse)
async def delete(sid: str):
    client = get_twilio_client()
    client.outgoing_caller_ids(sid).delete()
    return CallerIdDeleteResponse(status="deleted", sid=sid)
