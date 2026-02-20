from pydantic import BaseModel, Field
from typing import Optional


class CallRequest(BaseModel):
    """Request body for POST /call."""
    to: str  # E.164 format phone number, e.g. "+34612345678"
    from_: str | None = Field(default=None, alias="from")

    model_config = {"populate_by_name": True}


class CallResponse(BaseModel):
    """Response for POST /call."""
    call_sid: str
    status: str


class CallStatusResponse(BaseModel):
    """Response for GET /call/{call_sid}/status."""
    call_sid: str
    status: str
    to: str
    from_: Optional[str] = None


class EndCallResponse(BaseModel):
    """Response for POST /call/{call_sid}/end."""
    call_sid: str
    status: str
