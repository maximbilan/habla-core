import asyncio

from app.agent import transcript
from app.agent.transcript import TranscriptService


def test_extract_responses_output_text():
    service = TranscriptService()

    assert service._extract_text({"output_text": "Hello"}) == "Hello"


def test_extract_responses_nested_output_text():
    service = TranscriptService()

    payload = {
        "output": [
            {
                "content": [
                    {"type": "output_text", "text": "Hello"},
                    {"type": "output_text", "text": " there"},
                ]
            }
        ]
    }

    assert service._extract_text(payload) == "Hello there"


def test_translate_to_english_posts_to_openai(monkeypatch):
    service = TranscriptService(source_language_label="Spanish", model="gpt-test")
    captured: dict = {}

    def fake_post(body: dict) -> dict:
        captured.update(body)
        return {"output_text": "Good morning"}

    monkeypatch.setattr(transcript, "OPENAI_API_KEY", "test-key")
    service._post_json = fake_post  # type: ignore[method-assign]

    result = asyncio.run(service.translate_to_english("Buenos dias"))

    assert result == "Good morning"
    assert captured["model"] == "gpt-test"
    assert "Buenos dias" in captured["input"][0]["content"][0]["text"]
