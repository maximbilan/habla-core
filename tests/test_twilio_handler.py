from app import twilio_handler
import pytest


def test_media_stream_ws_url_uses_public_url(monkeypatch):
    monkeypatch.setattr(twilio_handler, "PUBLIC_URL", "https://example.com")
    assert twilio_handler._media_stream_ws_url() == "wss://example.com/twilio/media-stream"


def test_generate_media_stream_twiml_contains_stream_url(monkeypatch):
    monkeypatch.setattr(twilio_handler, "PUBLIC_URL", "http://localhost:8000")
    twiml = twilio_handler.generate_media_stream_twiml()
    assert "ws://localhost:8000/twilio/media-stream" in twiml


def test_media_stream_ws_url_rejects_blank_public_url(monkeypatch):
    monkeypatch.setattr(twilio_handler, "PUBLIC_URL", "")

    with pytest.raises(RuntimeError, match="PUBLIC_URL"):
        twilio_handler._media_stream_ws_url()
