import urllib.parse

from twilio.base.exceptions import TwilioRestException
from fastapi import APIRouter, Depends, HTTPException

from app.caller_id.models import (
    CallerIdDeleteResponse,
    CallerIdEntry,
    CallerIdListResponse,
    CallerIdStatusResponse,
    CallerIdVerifyRequest,
    CallerIdVerifyResponse,
)
from app.caller_id.ownership_client import (
    OwnershipServiceError,
    claim_sid,
    delete_claim,
    get_claim,
    list_claimed_sids,
)
from app.caller_id.service import (
    SPAIN_DELIVERABILITY_NOTE,
    get_twilio_client,
    parse_twilio_error,
)
from app.request_auth import require_device_id

router = APIRouter(prefix="/caller-id", tags=["caller-id"])


@router.post("/verify/start", response_model=CallerIdVerifyResponse)
async def start_verification(
    request: CallerIdVerifyRequest,
    device_id: str = Depends(require_device_id),
):
    client = get_twilio_client()

    try:
        existing = client.outgoing_caller_ids.list(phone_number=request.phone_number)
        if existing:
            sid = existing[0].sid
            try:
                claimed = claim_sid(sid=sid, phone_number=request.phone_number, device_id=device_id)
            except OwnershipServiceError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc

            if not claimed:
                return CallerIdVerifyResponse(
                    status="error",
                    phone_number=request.phone_number,
                    message=(
                        "This phone number is already linked to another device. "
                        "Verify a different number on this device."
                    ),
                    note=SPAIN_DELIVERABILITY_NOTE,
                )
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
async def check_status(
    phone_number: str,
    device_id: str = Depends(require_device_id),
):
    client = get_twilio_client()
    decoded_number = urllib.parse.unquote(phone_number)

    existing = client.outgoing_caller_ids.list(phone_number=decoded_number)
    if existing:
        sid = existing[0].sid
        try:
            claimed = claim_sid(sid=sid, phone_number=decoded_number, device_id=device_id)
        except OwnershipServiceError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        if not claimed:
            return CallerIdStatusResponse(
                phone_number=decoded_number,
                verified=False,
                note=SPAIN_DELIVERABILITY_NOTE,
            )
        return CallerIdStatusResponse(
            phone_number=decoded_number,
            verified=True,
            friendly_name=existing[0].friendly_name,
            sid=sid,
            note=SPAIN_DELIVERABILITY_NOTE,
        )

    return CallerIdStatusResponse(
        phone_number=decoded_number,
        verified=False,
        note=SPAIN_DELIVERABILITY_NOTE,
    )


@router.get("/list", response_model=CallerIdListResponse)
async def list_all(device_id: str = Depends(require_device_id)):
    client = get_twilio_client()
    caller_ids = client.outgoing_caller_ids.list()
    try:
        owned_sids = list_claimed_sids(device_id)
    except OwnershipServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return CallerIdListResponse(
        caller_ids=[
            CallerIdEntry(
                phone_number=cid.phone_number,
                friendly_name=cid.friendly_name,
                sid=cid.sid,
                date_created=cid.date_created.isoformat() if cid.date_created else None,
            )
            for cid in caller_ids
            if cid.sid in owned_sids
        ],
        note=SPAIN_DELIVERABILITY_NOTE,
    )


@router.delete("/{sid}", response_model=CallerIdDeleteResponse)
async def delete(
    sid: str,
    device_id: str = Depends(require_device_id),
):
    try:
        claim = get_claim(sid=sid)
    except OwnershipServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if not claim or claim.device_id != device_id:
        raise HTTPException(status_code=404, detail="Caller ID not found")

    client = get_twilio_client()
    client.outgoing_caller_ids(sid).delete()
    try:
        delete_claim(sid=sid, device_id=device_id)
    except OwnershipServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return CallerIdDeleteResponse(status="deleted", sid=sid)
