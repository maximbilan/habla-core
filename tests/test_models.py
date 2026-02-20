from app.models import CallRequest, CallStatusResponse


def test_call_request_accepts_from_alias():
    req = CallRequest.model_validate({"to": "+349999", "from": "+1202"})
    assert req.to == "+349999"
    assert req.from_ == "+1202"


def test_call_status_response_serializes_from_alias():
    resp = CallStatusResponse(
        call_sid="CA1",
        status="ringing",
        to="+349999",
        from_="+1202",
    )
    payload = resp.model_dump(by_alias=True)
    assert payload["from"] == "+1202"
