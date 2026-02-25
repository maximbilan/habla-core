from app.models import CallRequest, CallStatusResponse


def test_call_request_accepts_from_alias():
    req = CallRequest.model_validate({"to": "+349999", "from": "+1202"})
    assert req.to == "+349999"
    assert req.from_ == "+1202"
    assert req.source_language == "en-US"
    assert req.target_language == "es-US"
    assert req.voice_gender is None


def test_call_request_accepts_language_fields():
    req = CallRequest.model_validate(
        {
            "to": "+349999",
            "from": "+1202",
            "source_language": "fr-FR",
            "target_language": "de-DE",
            "voice_gender": "male",
        }
    )
    assert req.source_language == "fr-FR"
    assert req.target_language == "de-DE"
    assert req.voice_gender == "male"


def test_call_status_response_serializes_from_alias():
    resp = CallStatusResponse(
        call_sid="CA1",
        status="ringing",
        to="+349999",
        from_="+1202",
        source_language="en-US",
        target_language="es-US",
    )
    payload = resp.model_dump(by_alias=True)
    assert payload["from_"] == "+1202"
    assert payload["source_language"] == "en-US"
    assert payload["target_language"] == "es-US"
