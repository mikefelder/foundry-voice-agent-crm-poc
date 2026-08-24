"""The tool surface the agent calls.

``registry.py`` is the single source of truth: REST routes, the OpenAPI document
and the MCP tool list are all derived from it, so they cannot drift apart.
"""

from __future__ import annotations

__all__ = ["RecordNotFound", "ToolError"]


class ToolError(RuntimeError):
    """A tool could not be completed for a reason the agent should say aloud."""


class RecordNotFound(ToolError):
    """A record ID was well-formed but matched nothing."""
