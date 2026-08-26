"""WebSocket relay between the browser and Voice Live.

The browser never talks to Voice Live directly. That keeps Entra out of the page
- no MSAL, no app registration, no token in client storage - and preserves the
property that matters: the client streams audio and renders audio, and every
tool call still happens server-side.

Control messages are JSON, audio frames are binary, in both directions.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets

from azure.ai.voicelive.aio import connect
from azure.ai.voicelive.models import (
    ClientEventConversationItemCreate,
    ClientEventInputAudioBufferAppend,
    ClientEventResponseCancel,
    ClientEventResponseCreate,
    ClientEventSessionUpdate,
    RequestTextContentPart,
    UserMessageItem,
)
from azure.identity.aio import DefaultAzureCredential
from fastapi import WebSocket, WebSocketDisconnect

from crm_companion.agent.voicelive_config import build_session
from crm_companion.api.links import subscribe
from crm_companion.config import Settings

__all__ = ["relay"]

logger = logging.getLogger(__name__)

AUTH_TIMEOUT_SECONDS = 10


async def _authenticate(websocket: WebSocket, settings: Settings) -> bool:
    """First frame must carry the key; browsers cannot set WebSocket headers."""
    expected = settings.tool_api_key.get_secret_value() if settings.tool_api_key else ""
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=AUTH_TIMEOUT_SECONDS)
        supplied = json.loads(raw).get("key", "")
    except (TimeoutError, ValueError, KeyError):
        return False
    return bool(expected) and secrets.compare_digest(str(supplied), expected)


async def _browser_to_voicelive(websocket: WebSocket, connection) -> None:
    while True:
        message = await websocket.receive()
        if message.get("type") == "websocket.disconnect":
            return

        if (audio := message.get("bytes")) is not None:
            await connection.send(ClientEventInputAudioBufferAppend(audio=audio))
            continue

        if (text := message.get("text")) is None:
            continue

        payload = json.loads(text)
        if payload.get("type") == "text" and payload.get("text"):
            await connection.send(
                ClientEventConversationItemCreate(
                    item=UserMessageItem(content=[RequestTextContentPart(text=payload["text"])])
                )
            )
            await connection.send(ClientEventResponseCreate())


async def _voicelive_to_browser(websocket: WebSocket, connection) -> None:
    responding = False

    async def say(kind: str, **fields) -> None:
        await websocket.send_text(json.dumps({"type": kind, **fields}))

    while True:
        event = await connection.recv()
        kind = str(getattr(event, "type", ""))

        if kind.endswith("input_audio_buffer.speech_started"):
            await say("speech_started")
            if responding:
                responding = False
                await connection.send(ClientEventResponseCancel())
        elif kind.endswith("response.created"):
            responding = True
        elif kind.endswith("response.done"):
            responding = False
            await say("turn_end")
        elif kind.endswith("response.audio.delta"):
            await websocket.send_bytes(event.delta)
        elif kind.endswith("conversation.item.input_audio_transcription.completed"):
            await say("transcript", role="user", text=event.transcript)
        elif kind.endswith("response.audio_transcript.done"):
            await say("transcript", role="agent", text=event.transcript)
        elif kind.endswith("response.text.done"):
            await say("transcript", role="agent", text=event.text)
        elif kind.endswith("error"):
            await say("error", text=str(getattr(event, "error", "unknown")))


async def _links_to_browser(websocket: WebSocket, queue) -> None:
    while True:
        link = await queue.get()
        await websocket.send_text(json.dumps({"type": "link", **link}))


async def relay(websocket: WebSocket, settings: Settings) -> None:
    await websocket.accept()
    if not await _authenticate(websocket, settings):
        await websocket.close(code=4401, reason="invalid or missing API key")
        return

    credential = DefaultAzureCredential()
    try:
        async with connect(
            credential=credential,
            endpoint=settings.voicelive_endpoint,
            api_version=settings.voicelive_api_version,
            agent_name=settings.agent_name,
            project_name=settings.project_name,
        ) as connection:
            await connection.send(ClientEventSessionUpdate(session=build_session(settings)))
            await websocket.send_text(json.dumps({"type": "ready"}))

            with subscribe() as links:
                pump = asyncio.gather(
                    _browser_to_voicelive(websocket, connection),
                    _voicelive_to_browser(websocket, connection),
                    _links_to_browser(websocket, links),
                )
                try:
                    await pump
                finally:
                    pump.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await pump
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001 - the socket must close cleanly regardless
        logger.exception("voice relay failed")
        with contextlib.suppress(Exception):
            await websocket.send_text(json.dumps({"type": "error", "text": str(exc)}))
    finally:
        await credential.close()
        with contextlib.suppress(Exception):
            await websocket.close()
