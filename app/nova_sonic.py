"""
Nova 2 Sonic bidirectional streaming client.

Wraps the aws-sdk-bedrock-runtime Python SDK to manage a persistent
bidirectional audio stream with the Amazon Nova 2 Sonic model.
"""

import asyncio
import base64
import json
import uuid
import logging
from typing import Optional, Callable, Awaitable

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

from app.config import AWS_REGION, NOVA_MODEL_ID

logger = logging.getLogger(__name__)


class NovaSonicSession:
    """
    Manages a single Nova 2 Sonic bidirectional streaming session.

    Each session has:
      - A system prompt (e.g. "translate English to Spanish")
      - A continuous audio input stream (USER role)
      - An async queue for audio output chunks
    """

    def __init__(
        self,
        session_id: str,
        system_prompt: str,
        voice_id: str = "matthew",
        input_sample_rate: int = 16000,
        output_sample_rate: int = 24000,
        on_audio_output: Optional[Callable[[bytes], Awaitable[None]]] = None,
    ):
        self.session_id = session_id
        self.system_prompt = system_prompt
        self.voice_id = voice_id
        self.input_sample_rate = input_sample_rate
        self.output_sample_rate = output_sample_rate

        self.model_id = NOVA_MODEL_ID
        self.region = AWS_REGION

        self.client: Optional[BedrockRuntimeClient] = None
        self.stream = None
        self.is_active = False

        # Unique identifiers for prompt / content tracking
        self.prompt_name = str(uuid.uuid4())
        self.content_name = str(uuid.uuid4())
        self.audio_content_name = str(uuid.uuid4())

        # Consumers can read translated audio from this queue
        self.audio_output_queue: asyncio.Queue[bytes] = asyncio.Queue()

        self._response_task: Optional[asyncio.Task] = None
        self._on_audio_output = on_audio_output
        self._died_unexpectedly = False

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def _initialize_client(self) -> None:
        config = Config(
            endpoint_uri=f"https://bedrock-runtime.{self.region}.amazonaws.com",
            region=self.region,
            aws_credentials_identity_resolver=EnvironmentCredentialsResolver(),
            auth_scheme_resolver=HTTPAuthSchemeResolver(),
            auth_schemes={
                "aws.auth#sigv4": SigV4AuthScheme(service="bedrock")
            },
        )
        self.client = BedrockRuntimeClient(config=config)

    # ------------------------------------------------------------------
    # Event helpers
    # ------------------------------------------------------------------

    async def _send(self, event_dict: dict) -> None:
        payload = json.dumps(event_dict).encode("utf-8")
        event = InvokeModelWithBidirectionalStreamInputChunk(
            value=BidirectionalInputPayloadPart(bytes_=payload)
        )
        await self.stream.input_stream.send(event)

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Open the bidirectional stream and send the setup event sequence."""
        if not self.client:
            self._initialize_client()

        self.stream = await self.client.invoke_model_with_bidirectional_stream(
            InvokeModelWithBidirectionalStreamOperationInput(
                model_id=self.model_id
            )
        )
        self.is_active = True

        # 1 ── session start
        await self._send({
            "event": {
                "sessionStart": {
                    "inferenceConfiguration": {
                        # Translation call mode prefers short, fast utterances.
                        "maxTokens": 320,
                        "topP": 0.8,
                        "temperature": 0.3,
                    },
                    "turnDetectionConfiguration": {
                        "endpointingSensitivity": "HIGH",
                    },
                }
            }
        })

        # 2 ── prompt start (output configuration)
        await self._send({
            "event": {
                "promptStart": {
                    "promptName": self.prompt_name,
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
        })

        # 3 ── system prompt content start
        await self._send({
            "event": {
                "contentStart": {
                    "promptName": self.prompt_name,
                    "contentName": self.content_name,
                    "type": "TEXT",
                    "interactive": True,
                    "role": "SYSTEM",
                    "textInputConfiguration": {
                        "mediaType": "text/plain",
                    },
                }
            }
        })

        # 4 ── system prompt text
        await self._send({
            "event": {
                "textInput": {
                    "promptName": self.prompt_name,
                    "contentName": self.content_name,
                    "content": self.system_prompt,
                }
            }
        })

        # 5 ── system prompt content end
        await self._send({
            "event": {
                "contentEnd": {
                    "promptName": self.prompt_name,
                    "contentName": self.content_name,
                }
            }
        })

        # 6 ── audio input content start (keeps the stream open for audio)
        await self._send({
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
        })

        # start background response reader
        self._response_task = asyncio.create_task(
            self._process_responses(), name=f"nova-rx-{self.session_id}"
        )
        logger.info("Nova session %s started (voice=%s)", self.session_id, self.voice_id)

    # ------------------------------------------------------------------
    # Audio input (send to model)
    # ------------------------------------------------------------------

    async def send_audio(self, pcm_audio: bytes) -> None:
        """Send a chunk of PCM audio to the model."""
        if not self.is_active:
            return
        b64 = base64.b64encode(pcm_audio).decode("utf-8")
        await self._send({
            "event": {
                "audioInput": {
                    "promptName": self.prompt_name,
                    "contentName": self.audio_content_name,
                    "content": b64,
                }
            }
        })

    # ------------------------------------------------------------------
    # Response processing
    # ------------------------------------------------------------------

    async def _process_responses(self) -> None:
        """Read events from the model stream in a loop."""
        try:
            while self.is_active:
                output = await self.stream.await_output()
                result = await output[1].receive()

                if result.value and result.value.bytes_:
                    raw = result.value.bytes_.decode("utf-8")
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        logger.warning("[%s] non-JSON response: %s", self.session_id, raw[:120])
                        continue

                    if "event" in data:
                        await self._handle_event(data["event"])

        except Exception as e:
            if self.is_active:
                logger.error("Nova response loop error [%s]: %s", self.session_id, e)
                self.is_active = False
                self._died_unexpectedly = True
        finally:
            logger.info("Nova response loop ended [%s]", self.session_id)

    async def _handle_event(self, event: dict) -> None:
        # ── audio output (the translated speech)
        if "audioOutput" in event:
            audio_bytes = base64.b64decode(event["audioOutput"]["content"])
            await self.audio_output_queue.put(audio_bytes)
            if self._on_audio_output:
                try:
                    await self._on_audio_output(audio_bytes)
                except Exception as e:
                    logger.error("audio output callback error [%s]: %s", self.session_id, e)

        # ── content lifecycle
        elif "contentStart" in event:
            cs = event["contentStart"]
            additional = cs.get("additionalModelFields")
            if additional:
                try:
                    fields = json.loads(additional) if isinstance(additional, str) else additional
                    stage = fields.get("generationStage", "")
                    logger.debug("[%s] contentStart stage=%s", self.session_id, stage)
                except Exception:
                    pass

        elif "contentEnd" in event:
            reason = event["contentEnd"].get("stopReason", "")
            if reason == "INTERRUPTED":
                logger.info("[%s] barge-in detected", self.session_id)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Gracefully close the stream."""
        if not self.is_active:
            return
        self.is_active = False
        logger.info("Closing Nova session %s …", self.session_id)

        try:
            await self._send({
                "event": {
                    "contentEnd": {
                        "promptName": self.prompt_name,
                        "contentName": self.audio_content_name,
                    }
                }
            })
            await self._send({
                "event": {"promptEnd": {"promptName": self.prompt_name}}
            })
            await self._send({"event": {"sessionEnd": {}}})
            await self.stream.input_stream.close()
        except Exception as e:
            logger.error("Error during Nova session close [%s]: %s", self.session_id, e)

        if self._response_task and not self._response_task.done():
            # Give the CRT event loop a moment to fire its _on_complete
            # callbacks before we cancel the response reader; this avoids
            # the "InvalidStateError: CANCELLED" from awscrt internals.
            try:
                await asyncio.wait_for(self._response_task, timeout=2.0)
            except asyncio.TimeoutError:
                self._response_task.cancel()
                try:
                    await self._response_task
                except asyncio.CancelledError:
                    pass

        logger.info("Nova session %s closed", self.session_id)
