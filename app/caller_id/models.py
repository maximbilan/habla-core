from pydantic import BaseModel


class CallerIdVerifyRequest(BaseModel):
    phone_number: str
    friendly_name: str | None = None


class CallerIdVerifyResponse(BaseModel):
    status: str
    phone_number: str
    validation_code: str | None = None
    call_sid: str | None = None
    message: str | None = None
    note: str | None = None


class CallerIdStatusResponse(BaseModel):
    phone_number: str
    verified: bool
    friendly_name: str | None = None
    sid: str | None = None
    note: str | None = None


class CallerIdEntry(BaseModel):
    phone_number: str
    friendly_name: str | None
    sid: str
    date_created: str | None


class CallerIdListResponse(BaseModel):
    caller_ids: list[CallerIdEntry]
    note: str | None = None


class CallerIdDeleteResponse(BaseModel):
    status: str
    sid: str
