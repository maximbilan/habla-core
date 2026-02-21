"""Single Nova 2 Sonic session used by Agent Mode."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from typing import Awaitable, Callable

from aws_sdk_bedrock_runtime.client import (
    BedrockRuntimeClient,
    InvokeModelWithBidirectionalStreamOperationInput,
)
from aws_sdk_bedrock_runtime.models import (
    InvokeModelWithBidirectionalStreamInputChunk,
    BidirectionalInputPayloadPart,
)
from aws_sdk_bedrock_runtime.config import (
    Config,
    HTTPAuthSchemeResolver,
    SigV4AuthScheme,
)
from smithy_aws_core.identity import EnvironmentCredentialsResolver

from app.config import AWS_REGION, INPUT_SAMPLE_RATE, OUTPUT_SAMPLE_RATE

logger = logging.getLogger(__name__)


class AgentNovaSession:
    """Manages one bidirectional Nova stream for autonomous agent calls."""

    def __init__(
        self,
        session_id: str,
        system_prompt: str,
        voice_id: str,
        on_audio_output: Callable[[bytes], Awaitable[None]],
        on_transcript: Callable[[str, str], Awaitable[None]],
        on_agent_status: Callable[[str], Awaitable[None]],
        input_sample_rate: int = INPUT_SAMPLE_RATE,
        output_sample_rate: int = OUTPUT_SAMPLE_RATE,
    ) -> None:
        self.session_id = session_id
        self.system_prompt = system_prompt
        self.voice_id = voice_id
        self.input_sample_rate = input_sample_rate
        self.output_sample_rate = output_sample_rate

        self._on_audio_output = on_audio_output
        self._on_transcript = on_transcript
        self._on_agent_status = on_agent_status

        self._bedrock: BedrockRuntimeClient | None = None
        self.stream = None
        self.is_active = False
        self._died_unexpectedly = False

        self.prompt_name = str(uuid.uuid4())
        self.content_name = str(uuid.uuid4())
        self.audio_content_name = str(uuid.uuid4())

        self._response_task: asyncio.Task | None = None
        self._text_roles: dict[str, str] = {}

    def _init_client(self) -> None:
        config = Config(
            endpoint_uri=f"https://bedrock-runtime.{AWS_REGION}.amazonaws.com",
            region=AWS_REGION,
            aws_credentials_identity_resolver=EnvironmentCredentialsResolver(),
            auth_scheme_resolver=HTTPAuthSchemeResolver(),
            auth_schemes={
                "aws.auth#sigv4": SigV4AuthScheme(service="bedrock"),
            },
        )
        self._bedrock = BedrockRuntimeClient(config=config)

    async def send_event(self, payload: str) -> None:
        event = InvokeModelWithBidirectionalStreamInputChunk(
            value=BidirectionalInputPayloadPart(bytes_=payload.encode("utf-8"))
        )
        await self.stream.input_stream.send(event)

    async def _send(self, event: dict) -> None:
        await self.send_event(json.dumps(event))

    async def start(self) -> None:
        if not self._bedrock:
            self._init_client()

        self.stream = await self._bedrock.invoke_model_with_bidirectional_stream(
            InvokeModelWithBidirectionalStreamOperationInput(
                model_id="amazon.nova-2-sonic-v1:0"
            )
        )
        self.is_active = True

        await self._send(
            {
                "event": {
                    "sessionStart": {
                        "inferenceConfiguration": {
                            "maxTokens": 768,
                            "topP": 0.95,
                            "temperature": 0.7,
                        },
                        "turnDetectionConfiguration": {
                            "endpointingSensitivity": "LOW",
                        },
                    }
                }
            }
        )

        await self._send(
            {
                "event": {
                    "promptStart": {
                        "promptName": self.prompt_name,
                        "textOutputConfiguration": {"mediaType": "text/plain"},
                        "audioOutputConfiguration": {
                            "mediaType": "audio/lpcm",
                            "sampleRateHertz": self.output_sample_rate,
                            "sampleSizeBits": 16,
                            "channelCount": 1,
                            "voiceId": self.voice_id,
                            "encoding": "base64",
                            "audioType": "SPEECH",
                        },
                    }
                }
            }
        )

        await self._send(
            {
                "event": {
                    "contentStart": {
                        "promptName": self.prompt_name,
                        "contentName": self.content_name,
                        "type": "TEXT",
                        "interactive": True,
                        "role": "SYSTEM",
                        "textInputConfiguration": {"mediaType": "text/plain"},
                    }
                }
            }
        )

        await self._send(
            {
                "event": {
                    "textInput": {
                        "promptName": self.prompt_name,
                        "contentName": self.content_name,
                        "content": self.system_prompt,
                    }
                }
            }
        )

        await self._send(
            {
                "event": {
                    "contentEnd": {
                        "promptName": self.prompt_name,
                        "contentName": self.content_name,
                    }
                }
            }
        )

        await self._send(
            {
                "event": {
                    "contentStart": {
                        "promptName": self.prompt_name,
                        "contentName": self.audio_content_name,
                        "type": "AUDIO",
                        "interactive": True,
                        "role": "USER",
                        "audioInputConfiguration": {
                            "mediaType": "audio/lpcm",
                            "sampleRateHertz": self.input_sample_rate,
                            "sampleSizeBits": 16,
                            "channelCount": 1,
                            "audioType": "SPEECH",
                            "encoding": "base64",
                        },
                    }
                }
            }
        )

        self._response_task = asyncio.create_task(self._process_responses())
        logger.info("Agent Nova session started: %s", self.session_id)

    async def send_audio(self, pcm_audio: bytes) -> None:
        if not self.is_active:
            return
        b64 = base64.b64encode(pcm_audio).decode("utf-8")
        await self._send(
            {
                "event": {
                    "audioInput": {
                        "promptName": self.prompt_name,
                        "contentName": self.audio_content_name,
                        "content": b64,
                    }
                }
            }
        )

    async def inject_instruction(self, instruction_text: str) -> None:
        if not self.is_active:
            return

        content_name = str(uuid.uuid4())
        content_start = {
            "event": {
                "contentStart": {
                    "promptName": self.prompt_name,
                    "contentName": content_name,
                    "type": "TEXT",
                    "interactive": True,
                    "role": "USER",
                    "textInputConfiguration": {
                        "mediaType": "text/plain",
                    },
                }
            }
        }
        await self._send(content_start)

        text_input = {
            "event": {
                "textInput": {
                    "promptName": self.prompt_name,
                    "contentName": content_name,
                    "content": f"[Additional instruction from caller]: {instruction_text}",
                }
            }
        }
        await self._send(text_input)

        content_end = {
            "event": {
                "contentEnd": {
                    "promptName": self.prompt_name,
                    "contentName": content_name,
                }
            }
        }
        await self._send(content_end)

    async def _process_responses(self) -> None:
        try:
            while self.is_active:
                output = await self.stream.await_output()
                result = await output[1].receive()
                if not (result.value and result.value.bytes_):
                    continue

                try:
                    payload = json.loads(result.value.bytes_.decode("utf-8"))
                except json.JSONDecodeError:
                    continue

                event = payload.get("event")
                if event:
                    await self._handle_event(event)
        except Exception as exc:
            if self.is_active:
                logger.error("Agent Nova response loop error [%s]: %s", self.session_id, exc)
                self._died_unexpectedly = True
                self.is_active = False
        finally:
            logger.info("Agent Nova response loop ended [%s]", self.session_id)

    async def _handle_event(self, event: dict) -> None:
        if "audioOutput" in event:
            await self._on_agent_status("speaking")
            audio_bytes = base64.b64decode(event["audioOutput"]["content"])
            await self._on_audio_output(audio_bytes)
            return

        if "contentStart" in event:
            content_start = event["contentStart"]
            if content_start.get("type") == "TEXT":
                self._text_roles[content_start.get("contentName", "")] = content_start.get("role", "")

            role = str(content_start.get("role", "")).upper()
            if role in {"USER", "CALLER"}:
                await self._on_agent_status("listening")
            elif role in {"ASSISTANT", "MODEL"}:
                await self._on_agent_status("thinking")
            return

        if "textOutput" in event:
            text_out = event["textOutput"]
            text = text_out.get("content", "").strip()
            if not text:
                return

            content_name = text_out.get("contentName", "")
            model_role = str(self._text_roles.get(content_name, "")).upper()
            role = "agent" if model_role in {"ASSISTANT", "MODEL"} else "callee"
            await self._on_transcript(role, text)
            return

    async def stop(self) -> None:
        if not self.is_active:
            return
        self.is_active = False

        try:
            await self._send(
                {
                    "event": {
                        "contentEnd": {
                            "promptName": self.prompt_name,
                            "contentName": self.audio_content_name,
                        }
                    }
                }
            )
            await self._send({"event": {"promptEnd": {"promptName": self.prompt_name}}})
            await self._send({"event": {"sessionEnd": {}}})
            await self.stream.input_stream.close()
        except Exception as exc:
            logger.error("Error closing agent session [%s]: %s", self.session_id, exc)

        if self._response_task and not self._response_task.done():
            try:
                await asyncio.wait_for(self._response_task, timeout=2.0)
            except asyncio.TimeoutError:
                self._response_task.cancel()
                try:
                    await self._response_task
                except asyncio.CancelledError:
                    pass
