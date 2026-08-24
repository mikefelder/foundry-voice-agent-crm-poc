"""Provider selection.

``CRM_PROVIDER`` decides which implementation the tool layer talks to. The
Salesforce client owns an HTTP connection and must be closed; the fake owns
nothing, so both are handed out through the same scope.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from crm_companion.config import Settings
from crm_companion.crm.fake_provider import FakeCrmProvider
from crm_companion.crm.provider import CrmProvider
from crm_companion.crm.salesforce_auth import build_token_provider
from crm_companion.crm.salesforce_client import SalesforceClient
from crm_companion.crm.salesforce_provider import SalesforceProvider

__all__ = ["provider_scope"]


@asynccontextmanager
async def provider_scope(settings: Settings) -> AsyncIterator[CrmProvider]:
    if not settings.use_salesforce:
        yield FakeCrmProvider.from_default_recording()
        return

    async with SalesforceClient(
        build_token_provider(settings),
        api_version=settings.sf_api_version,
        timeout=settings.request_timeout_seconds,
    ) as client:
        yield SalesforceProvider(client, settings=settings)
