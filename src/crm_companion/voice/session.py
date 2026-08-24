"""The Voice Live session loop.

Audio in, audio out, and one rule that matters more than the rest: the moment
the service reports speech, queued playback is dropped and the in-flight
response is cancelled. Without that the agent talks over the rep, and in a car
that is the difference between usable and not.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from azure.ai.voicelive.aio import connect
from azure.ai.voicelive.models import (
    ClientEventInputAudioBufferAppend,
    ClientEventResponseCancel,
    ClientEventSessionUpdate,
)
from azure.identity.aio import DefaultAzureCredential

from crm_companion.agent.voicelive_config import build_session
from crm_companion.config import Settings
from crm_companion.voice.audio import Microphone, Speaker

__all__ = ["SessionHandler", "run_session"]

Reporter = Callable[[str, str], None]


def _print(role: str, text: str) -> None:
    print(f"{role}: {text}", flush=True)


async def _pump_microphone(connection, microphone: Microphone) -> None:
    async for chunk in microphone:
        await connection.send(ClientEventInputAudioBufferAppend(audio=chunk))


class SessionHandler:
    """Routes server events to audio and transcript output."""

    def __init__(self, speaker: Speaker, *, report: Reporter = _print) -> None:
        self._speaker = speaker
        self._report = report
        self._response_active = False

    async def handle(self, connection, event) -> None:
        kind = str(getattr(event, "type", ""))

        if kind.endswith("input_audio_buffer.speech_started"):
            self._speaker.barge_in()
            # Cancelling with nothing in flight is an error, and road noise
            # triggers speech detection far more often than it triggers a turn.
            if self._response_active:
                self._response_active = False
                await connection.send(ClientEventResponseCancel())
        elif kind.endswith("response.created"):
            self._response_active = True
        elif kind.endswith("response.done"):
            self._response_active = False
        elif kind.endswith("response.audio.delta"):
            self._speaker.play(event.delta)
        elif kind.endswith("conversation.item.input_audio_transcription.completed"):
            self._report("you", event.transcript)
        elif kind.endswith("response.audio_transcript.done"):
            self._report("agent", event.transcript)
        elif kind.endswith("error"):
            self._report("error", str(getattr(event, "error", "unknown")))


async def run_session(settings: Settings, *, report: Reporter = _print) -> int:
    settings.require_foundry()
    settings.require_voicelive()

    credential = DefaultAzureCredential()
    microphone: Microphone | None = None
    speaker: Speaker | None = None
    pump: asyncio.Task | None = None

    try:
        async with connect(
            credential=credential,
            endpoint=settings.voicelive_endpoint,
            api_version=settings.voicelive_api_version,
            agent_name=settings.agent_name,
            project_name=settings.project_name,
        ) as connection:
            await connection.send(ClientEventSessionUpdate(session=build_session(settings)))

            microphone = Microphone()
            speaker = Speaker()
            handler = SessionHandler(speaker, report=report)
            pump = asyncio.create_task(_pump_microphone(connection, microphone))
            report("system", "listening - speak when ready, ctrl-c to stop")

            while True:
                await handler.handle(connection, await connection.recv())
    except (KeyboardInterrupt, asyncio.CancelledError):
        return 0
    finally:
        if pump is not None:
            pump.cancel()
        if microphone is not None:
            microphone.close()
        if speaker is not None:
            speaker.close()
        await credential.close()
    return 0
