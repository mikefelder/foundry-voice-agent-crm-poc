"""The tool registry.

One declaration per tool, carrying everything every surface needs: the agent-facing
description, the input schema, the response type, the handler, and whether it
mutates. REST routes, the OpenAPI document and the MCP tool list are generated
from this tuple, which is what stops them drifting apart.

Descriptions are written for a driving rep: they say when to reach for the tool,
not how it is implemented.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from crm_companion.crm.models import (
    Account,
    Contact,
    Opportunity,
    PipelineSummary,
    StageResolution,
    TaskRecord,
    UserResolution,
    WriteResult,
)
from crm_companion.tools import handlers, schemas

__all__ = ["TOOLS", "ToolSpec", "get_tool", "read_tools", "tool_names", "write_tools"]

Handler = Callable[[Any, Any], Awaitable[Any]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    params: type[BaseModel]
    result: Any
    handler: Handler
    is_write: bool = False


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="search_accounts",
        description=(
            "Find an account by the name the rep said. Start here; every other "
            "account tool needs the ID this returns."
        ),
        params=schemas.SearchAccountsParams,
        result=list[Account],
        handler=handlers.search_accounts,
    ),
    ToolSpec(
        name="get_account",
        description="Account detail by ID.",
        params=schemas.GetAccountParams,
        result=Account,
        handler=handlers.get_account,
    ),
    ToolSpec(
        name="get_pipeline_summary",
        description=(
            "How many open opportunities, how many past due, and the oldest entry "
            "date. Use this for any 'how many' question - never count records aloud."
        ),
        params=schemas.GetAccountParams,
        result=PipelineSummary,
        handler=handlers.get_pipeline_summary,
    ),
    ToolSpec(
        name="list_open_opportunities",
        description="Open opportunities for an account, soonest close date first.",
        params=schemas.ListOpportunitiesParams,
        result=list[Opportunity],
        handler=handlers.list_open_opportunities,
    ),
    ToolSpec(
        name="list_past_due_opportunities",
        description=(
            "Open opportunities whose close date has passed, oldest first. Read "
            "these one at a time, waiting for the rep's cue between each."
        ),
        params=schemas.ListOpportunitiesParams,
        result=list[Opportunity],
        handler=handlers.list_past_due_opportunities,
    ),
    ToolSpec(
        name="get_opportunity",
        description="Opportunity detail by ID, including the current notes.",
        params=schemas.GetOpportunityParams,
        result=Opportunity,
        handler=handlers.get_opportunity,
    ),
    ToolSpec(
        name="list_contacts",
        description="Contacts at an account.",
        params=schemas.ListContactsParams,
        result=list[Contact],
        handler=handlers.list_contacts,
    ),
    ToolSpec(
        name="get_contact",
        description="Contact detail by ID.",
        params=schemas.GetContactParams,
        result=Contact,
        handler=handlers.get_contact,
    ),
    ToolSpec(
        name="list_tasks",
        description="Open tasks for the running user, soonest due date first.",
        params=schemas.ListTasksParams,
        result=list[TaskRecord],
        handler=handlers.list_tasks,
    ),
    ToolSpec(
        name="resolve_user",
        description=(
            "Turn a spoken name into a user who can actually be notified. If the "
            "result is not exactly one match, ask the rep which person they meant."
        ),
        params=schemas.ResolveUserParams,
        result=UserResolution,
        handler=handlers.resolve_user,
    ),
    ToolSpec(
        name="resolve_stage",
        description=(
            "Turn spoken stage shorthand into the org's real stage name. More than "
            "one match means ask which one - never pick for the rep."
        ),
        params=schemas.ResolveStageParams,
        result=StageResolution,
        handler=handlers.resolve_stage,
    ),
    ToolSpec(
        name="preview_opportunity_update",
        description=(
            "Show exactly what a change would do, without writing. Call this before "
            "any opportunity write, read the diff back, and only then write. Note "
            "text must be read back word for word. Returns the token the write needs."
        ),
        params=schemas.PreviewOpportunityUpdateParams,
        result=schemas.OpportunityPreview,
        handler=handlers.preview_opportunity_update,
    ),
    ToolSpec(
        name="update_opportunity",
        description=(
            "Write stage, close date or amount, each as the value it should become. "
            "Requires the confirmation_token from preview_opportunity_update and a "
            "spoken yes."
        ),
        params=schemas.UpdateOpportunityParams,
        result=WriteResult,
        handler=handlers.update_opportunity,
        is_write=True,
    ),
    ToolSpec(
        name="update_opportunity_notes",
        description=(
            "Write the comments and customer need fields. Requires the "
            "confirmation_token from preview_opportunity_update. Read the text back "
            "word for word first - it is manufactured from, not summarised."
        ),
        params=schemas.UpdateOpportunityNotesParams,
        result=WriteResult,
        handler=handlers.update_opportunity_notes,
        is_write=True,
    ),
    ToolSpec(
        name="create_task",
        description="Create a follow-up task. Saying the same thing twice creates one task.",
        params=schemas.CreateTaskParams,
        result=WriteResult,
        handler=handlers.create_task,
        is_write=True,
    ),
    ToolSpec(
        name="post_chatter_update",
        description=(
            "Post to a record's Chatter feed. Mentions must be user IDs from "
            "resolve_user, or the person is never notified."
        ),
        params=schemas.PostChatterUpdateParams,
        result=WriteResult,
        handler=handlers.post_chatter_update,
        is_write=True,
    ),
)

_BY_NAME = {tool.name: tool for tool in TOOLS}


def get_tool(name: str) -> ToolSpec:
    try:
        return _BY_NAME[name]
    except KeyError:
        raise KeyError(f"unknown tool: {name}") from None


def tool_names() -> tuple[str, ...]:
    return tuple(_BY_NAME)


def read_tools() -> tuple[ToolSpec, ...]:
    return tuple(tool for tool in TOOLS if not tool.is_write)


def write_tools() -> tuple[ToolSpec, ...]:
    return tuple(tool for tool in TOOLS if tool.is_write)
