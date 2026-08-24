"""Text-mode agent check over Voice Live.

Deliberately not the Responses API: ``gpt-realtime`` is a realtime model and the
Responses API rejects it outright. Going over Voice Live with audio switched off
exercises the transport the product actually uses, so a pass here means the
agent, the model and the session are wired correctly, leaving only microphone
and playback to prove later.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from azure.ai.voicelive.aio import connect
from azure.ai.voicelive.models import (
    ClientEventConversationItemCreate,
    ClientEventResponseCreate,
    ClientEventSessionUpdate,
    Modality,
    RequestSession,
    RequestTextContentPart,
    UserMessageItem,
)
from azure.identity.aio import DefaultAzureCredential

from crm_companion.config import Settings, get_settings

__all__ = ["PROBES", "run"]

# The second probe is the important one: with no tools attached yet, the agent
# must say it cannot answer rather than inventing a number.
PROBES = (
    "Good morning.",
    "How many open opportunities does Contoso Building Supply have?",
)


async def _ask(connection, prompt: str) -> str:
    await connection.send(
        ClientEventConversationItemCreate(
            item=UserMessageItem(content=[RequestTextContentPart(text=prompt)])
        )
    )
    await connection.send(ClientEventResponseCreate())

    parts: list[str] = []
    while True:
        event = await connection.recv()
        kind = str(getattr(event, "type", ""))
        if kind.endswith("response.text.delta"):
            parts.append(event.delta)
        elif kind.endswith("response.done"):
            return "".join(parts).strip()
        elif kind.endswith("error"):
            raise RuntimeError(str(getattr(event, "error", "Voice Live reported an error")))


async def _run(settings: Settings, prompts: tuple[str, ...]) -> int:
    credential = DefaultAzureCredential()
    try:
        async with connect(
            credential=credential,
            endpoint=settings.voicelive_endpoint,
            api_version=settings.voicelive_api_version,
            agent_name=settings.agent_name,
            project_name=settings.project_name,
        ) as connection:
            await connection.send(
                ClientEventSessionUpdate(
                    session=RequestSession(modalities=[Modality.TEXT]),
                )
            )
            for prompt in prompts:
                reply = await _ask(connection, prompt)
                print(f"\n> {prompt}\n  {reply}\n  [{len(reply.split())} words]")
    finally:
        await credential.close()
    return 0


def run(settings: Settings | None = None, *, prompts: tuple[str, ...] = PROBES) -> int:
    settings = settings or get_settings()
    settings.require_foundry()
    settings.require_voicelive()
    return asyncio.run(_run(settings, prompts))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt", action="append", help="override the built-in probes")
    args = parser.parse_args()
    return run(prompts=tuple(args.prompt) if args.prompt else PROBES)


if __name__ == "__main__":
    sys.exit(main())
