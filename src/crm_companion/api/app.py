"""FastAPI tool API.

Every route is generated from ``TOOLS``, so a tool cannot exist without an
endpoint or drift from the schema the agent was given.

This module deliberately does not use ``from __future__ import annotations``:
the generated endpoints carry their parameter model as a real annotation, and
deferring it to a string would leave FastAPI unable to resolve the schema.
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from crm_companion.api.links import publish
from crm_companion.api.realtime import relay
from crm_companion.api.security import api_key_guard
from crm_companion.config import Settings, get_settings
from crm_companion.crm.factory import provider_scope
from crm_companion.crm.provider import CrmProvider
from crm_companion.crm.salesforce_client import SalesforceError
from crm_companion.tools import RecordNotFound, ToolError
from crm_companion.tools.registry import TOOLS, ToolSpec

__all__ = ["create_app"]

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

TITLE = "CRM Sales Companion Tools"
VERSION = "1.0.0"
DESCRIPTION = (
    "Read and write a sales rep's CRM by voice. Counts come from the datastore, "
    "writes take absolute values only, and creates are idempotent."
)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    if settings.tool_api_key is None:
        logger.warning("TOOL_API_KEY is not set; every request will be rejected")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with provider_scope(settings) as provider:
            app.state.provider = provider
            yield

    app = FastAPI(
        title=TITLE,
        version=VERSION,
        description=DESCRIPTION,
        lifespan=lifespan,
        servers=[{"url": settings.tool_api_base_url}] if settings.tool_api_base_url else None,
    )

    guard = api_key_guard(settings)
    for tool in TOOLS:
        _register(app, tool, guard, settings)

    _register_error_handlers(app)

    @app.websocket("/ws/voice")
    async def voice(websocket: WebSocket) -> None:
        await relay(websocket, settings)

    if STATIC_DIR.is_dir():
        app.mount("/", _RevalidatedStatic(directory=STATIC_DIR, html=True), name="app")

    return app


# Labels are what the rep sees on screen, so they name the record, not the tool.
_WRITE_LABELS = {
    "update_opportunity": "Opportunity updated",
    "update_opportunity_notes": "Notes updated",
    "create_task": "Task created",
    "post_chatter_update": "Posted to Chatter",
}


def _register(app: FastAPI, tool: ToolSpec, guard, settings: Settings) -> None:
    params_model = tool.params

    async def endpoint(params: params_model, request: Request):
        provider: CrmProvider = request.app.state.provider
        result = await tool.handler(provider, params)
        if tool.is_write:
            _announce(tool.name, result, settings)
        return result

    app.post(
        f"/tools/{tool.name}",
        name=tool.name,
        operation_id=tool.name,
        summary=tool.name.replace("_", " "),
        description=tool.description,
        response_model=tool.result,
        response_model_exclude_none=False,
        tags=["write" if tool.is_write else "read"],
        dependencies=[Depends(guard)],
    )(endpoint)


def _announce(tool_name: str, result, settings: Settings) -> None:
    """Put a link on screen so the agent never has to read a record ID aloud."""
    base = (settings.sf_instance_url or "").rstrip("/")
    record_id = getattr(result, "record_id", None)
    if not base or not record_id:
        return
    publish(_WRITE_LABELS.get(tool_name, "Record updated"), f"{base}/{record_id}")


class _RevalidatedStatic(StaticFiles):
    """A cached stylesheet after a deploy looks exactly like a broken page."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


def _register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RecordNotFound)
    async def _missing(request: Request, exc: RecordNotFound) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ToolError)
    async def _needs_clarification(request: Request, exc: ToolError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(SalesforceError)
    async def _backend_rejected(request: Request, exc: SalesforceError) -> JSONResponse:
        # Logged rather than returned: the agent says it aloud, so it must stay generic.
        logger.error("CRM rejected %s: %s", request.url.path, exc)
        return JSONResponse(status_code=502, content={"detail": "the CRM rejected that request"})
