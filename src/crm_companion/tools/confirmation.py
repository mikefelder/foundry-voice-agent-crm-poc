"""Write confirmation tokens.

`preview_opportunity_update` issues a token over the exact values it previewed;
the write tools refuse anything else. That makes previewing a precondition of
writing rather than an instruction the model may skip - which it does, in
practice, on the fields that read most like a statement of fact.

Signed with the tool API key, so the agent cannot mint one from a tool result.
Stateless by design: no session store, and the token binds to the values rather
than to a conversation.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import date
from decimal import Decimal
from typing import Any

from crm_companion.config import get_settings
from crm_companion.tools import ToolError

__all__ = ["issue_token", "require_token"]


def _canonical(tool: str, values: dict[str, Any]) -> bytes:
    normalized: dict[str, Any] = {}
    for key, value in values.items():
        if value is None:
            normalized[key] = None
        elif isinstance(value, Decimal):
            # 42000 and 42000.0 must hash alike; the agent may echo either.
            normalized[key] = format(value.normalize(), "f")
        elif isinstance(value, date):
            normalized[key] = value.isoformat()
        else:
            normalized[key] = str(value)
    payload = {"tool": tool, "values": normalized}
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _secret() -> bytes:
    key = get_settings().tool_api_key
    # Without an API key the surface rejects every request, so tokens are moot.
    return (key.get_secret_value() if key else "").encode("utf-8")


def issue_token(tool: str, values: dict[str, Any]) -> str:
    return hmac.new(_secret(), _canonical(tool, values), hashlib.sha256).hexdigest()


def require_token(supplied: str, tool: str, values: dict[str, Any]) -> None:
    expected = issue_token(tool, values)
    if not hmac.compare_digest(supplied, expected):
        raise ToolError(
            "these values were not previewed; call preview_opportunity_update, "
            "read the change back, and use the token it returns"
        )
