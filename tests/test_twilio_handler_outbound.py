from app import twilio_handler


def test_initiate_outbound_call_builds_twiml_and_returns_ids(monkeypatch):
    monkeypatch.setattr(twilio_handler, "PUBLIC_URL", "https://example.com")

    def fake_create_outbound_call(to_number, from_number, twiml):
        assert to_number == "+349999"
        assert from_number == "+1202"
        assert 'wss://example.com/twilio/media-stream' in twiml
        assert "<Connect>" in twiml
        return "CA123", "+1202"

    monkeypatch.setattr(
        twilio_handler, "create_outbound_call", fake_create_outbound_call
    )

    call_sid, caller_id = twilio_handler.initiate_outbound_call(
        "+349999", "+1202"
    )
    assert call_sid == "CA123"
    assert caller_id == "+1202"
