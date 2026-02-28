"""Transcript storage and call-language->English translation for Agent Mode."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from aws_sdk_bedrock_runtime.client import BedrockRuntimeClient
from aws_sdk_bedrock_runtime.models import InvokeModelInput
from aws_sdk_bedrock_runtime.config import (
    Config,
    HTTPAuthSchemeResolver,
    SigV4AuthScheme,
)
from smithy_aws_core.identity import EnvironmentCredentialsResolver

from app.config import AWS_REGION

logger = logging.getLogger(__name__)


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
        region: str = AWS_REGION,
        source_language_label: str = "the call language",
    ) -> None:
        self.entries: list[TranscriptEntry] = []
        self._region = region
        self._source_language_label = source_language_label
        self._bedrock: BedrockRuntimeClient | None = None

    def _client(self) -> BedrockRuntimeClient:
        if self._bedrock:
            return self._bedrock

        config = Config(
            endpoint_uri=f"https://bedrock-runtime.{self._region}.amazonaws.com",
            region=self._region,
            aws_credentials_identity_resolver=EnvironmentCredentialsResolver(),
            auth_scheme_resolver=HTTPAuthSchemeResolver(),
            auth_schemes={
                "aws.auth#sigv4": SigV4AuthScheme(service="bedrock"),
            },
        )
        self._bedrock = BedrockRuntimeClient(config=config)
        return self._bedrock

    def add_entry(self, role: str, text_original: str) -> TranscriptEntry:
        entry = TranscriptEntry(role=role, text_original=text_original)
        self.entries.append(entry)
        return entry

    async def translate_to_english(self, source_text: str) -> str:
        """Translate transcript chunks to English with Nova 2 Lite."""
        prompt = (
            f"Translate the following text from {self._source_language_label} to English. "
            "Return only the translation, nothing else.\n\n"
            f"{source_text}"
        )

        body = {
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                }
            ],
            "max_tokens": 300,
        }

        response = await self._client().invoke_model(
            InvokeModelInput(
                model_id="amazon.nova-2-lite-v1:0",
                content_type="application/json",
                accept="application/json",
                body=json.dumps(body).encode("utf-8"),
            )
        )

        raw = json.loads(response.body.decode("utf-8"))
        text = self._extract_text(raw).strip()
        return text or source_text

    def _extract_text(self, payload: dict) -> str:
        # Nova schemas can vary across model families; keep parsing defensive.
        if isinstance(payload.get("output"), dict):
            output = payload["output"]
            msg = output.get("message") or {}
            content = msg.get("content") or []
            if content and isinstance(content, list):
                first = content[0]
                if isinstance(first, dict) and isinstance(first.get("text"), str):
                    return first["text"]

        if isinstance(payload.get("content"), list) and payload["content"]:
            first = payload["content"][0]
            if isinstance(first, dict) and isinstance(first.get("text"), str):
                return first["text"]

        for key in ("outputText", "completion", "generated_text", "text"):
            value = payload.get(key)
            if isinstance(value, str):
                return value

        logger.warning("Unexpected translation payload shape: keys=%s", list(payload.keys()))
        return ""
