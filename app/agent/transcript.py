"""Transcript storage and call-language -> English translation for Agent Mode."""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.config import OPENAI_API_KEY, OPENAI_TEXT_TRANSLATION_MODEL

logger = logging.getLogger(__name__)

OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"


@dataclass
class TranscriptEntry:
    role: str
    text_original: str
    text_en: str | None = None
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class TranscriptService:
    """Accumulates transcript entries and translates chunks to English."""

    def __init__(
        self,
        source_language_label: str = "the call language",
        model: str = OPENAI_TEXT_TRANSLATION_MODEL,
    ) -> None:
        self.entries: list[TranscriptEntry] = []
        self._source_language_label = source_language_label
        self._model = model

    def add_entry(self, role: str, text_original: str) -> TranscriptEntry:
        entry = TranscriptEntry(role=role, text_original=text_original)
        self.entries.append(entry)
        return entry

    async def translate_to_english(self, source_text: str) -> str:
        """Translate transcript chunks to English with OpenAI Responses."""
        if not OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY is required for transcript translation")

        prompt = (
            f"Translate the following text from {self._source_language_label} to English. "
            "Return only the translation, nothing else.\n\n"
            f"{source_text}"
        )
        body = {
            "model": self._model,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": prompt,
                        }
                    ],
                }
            ],
            "max_output_tokens": 300,
        }

        raw = await asyncio.to_thread(self._post_json, body)
        text = self._extract_text(raw).strip()
        return text or source_text

    def _post_json(self, body: dict) -> dict:
        request = urllib.request.Request(
            OPENAI_RESPONSES_URL,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"OpenAI transcript translation failed: {detail}") from exc

    def _extract_text(self, payload: dict) -> str:
        output_text = payload.get("output_text")
        if isinstance(output_text, str):
            return output_text

        output = payload.get("output")
        if isinstance(output, list):
            chunks: list[str] = []
            for item in output:
                if not isinstance(item, dict):
                    continue
                content = item.get("content")
                if not isinstance(content, list):
                    continue
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    text = part.get("text")
                    if isinstance(text, str):
                        chunks.append(text)
            if chunks:
                return "".join(chunks)

        logger.warning("Unexpected OpenAI translation payload shape: keys=%s", list(payload.keys()))
        return ""
