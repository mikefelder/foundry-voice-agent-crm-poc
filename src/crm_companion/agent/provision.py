"""Create or update the Prompt Agent version.

The Voice Live session config travels in agent metadata rather than being sent
by the client, so every caller - CLI today, browser later - gets the same audio
and turn-detection behaviour without shipping its own copy. Metadata values are
capped, hence the chunking.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import (
    OpenApiFunctionDefinition,
    OpenApiProjectConnectionAuthDetails,
    OpenApiProjectConnectionSecurityScheme,
    OpenApiTool,
    PromptAgentDefinition,
)
from azure.identity import DefaultAzureCredential

from crm_companion.agent.instructions import INSTRUCTIONS
from crm_companion.agent.voicelive_config import build_session, chunk_metadata
from crm_companion.api.openapi import DEFAULT_SPEC_PATH
from crm_companion.config import Settings, get_settings

__all__ = [
    "SESSION_METADATA_KEY",
    "build_definition",
    "build_metadata",
    "build_tools",
    "provision",
]

SESSION_METADATA_KEY = "voicelive_session"

DESCRIPTION = "Hands-free CRM companion for a sales rep who is driving."


def build_tools(settings: Settings) -> list[Any]:
    """Empty until a connection is configured, so the agent can be created before deploy."""
    if not settings.tool_connection_id:
        return []

    spec = json.loads(DEFAULT_SPEC_PATH.read_text(encoding="utf-8"))
    return [
        OpenApiTool(
            openapi=OpenApiFunctionDefinition(
                name="crm_tools",
                description="Read and write the rep's CRM records.",
                spec=spec,
                auth=OpenApiProjectConnectionAuthDetails(
                    security_scheme=OpenApiProjectConnectionSecurityScheme(
                        project_connection_id=settings.tool_connection_id
                    )
                ),
            )
        )
    ]


def build_definition(
    settings: Settings, *, instructions: str = INSTRUCTIONS, tools: list[Any] | None = None
) -> PromptAgentDefinition:
    return PromptAgentDefinition(
        model=settings.model_deployment_name,
        instructions=instructions,
        tools=build_tools(settings) if tools is None else tools,
    )


def build_metadata(settings: Settings) -> dict[str, str]:
    """No instructions here: the agent definition owns them and Voice Live refuses overrides."""
    session = build_session(settings)
    payload = json.dumps(session.as_dict(), separators=(",", ":"), sort_keys=True)
    return chunk_metadata(SESSION_METADATA_KEY, payload)


def provision(settings: Settings | None = None, *, client: AIProjectClient | None = None) -> Any:
    settings = settings or get_settings()
    settings.require_foundry()

    owned = client is None
    client = client or AIProjectClient(
        endpoint=settings.project_endpoint,
        credential=DefaultAzureCredential(),
    )
    try:
        return client.agents.create_version(
            agent_name=settings.agent_name,
            definition=build_definition(settings),
            metadata=build_metadata(settings),
            description=DESCRIPTION,
        )
    finally:
        if owned:
            client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()

    version = provision()
    name = getattr(version, "name", None) or getattr(version, "agent_name", "?")
    print(f"agent   : {name}")
    print(f"version : {getattr(version, 'version', '?')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
