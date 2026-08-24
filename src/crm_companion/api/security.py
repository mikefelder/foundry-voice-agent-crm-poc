"""API key authentication.

Declared as an OpenAPI security scheme rather than a header parameter: a header
parameter shows up in the agent's tool schema as something to fill in, and a
model inventing a credential is not a thing that should be representable.

Fails closed: when no key is configured every request is rejected, because the
alternative - serving CRM writes unauthenticated - is the worse failure.
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable

from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader

from crm_companion.config import Settings

__all__ = ["API_KEY_HEADER", "api_key_guard"]

API_KEY_HEADER = "x-api-key"

_scheme = APIKeyHeader(name=API_KEY_HEADER, auto_error=False)


def api_key_guard(settings: Settings) -> Callable[..., Awaitable[None]]:
    configured = settings.tool_api_key

    async def verify(provided: str | None = Security(_scheme)) -> None:
        expected = configured.get_secret_value() if configured else ""
        # compare_digest on bytes so a non-ASCII header cannot raise instead of rejecting.
        if not expected or not secrets.compare_digest(
            (provided or "").encode("utf-8"), expected.encode("utf-8")
        ):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing API key")

    return verify
