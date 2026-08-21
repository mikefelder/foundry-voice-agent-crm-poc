"""Shared fixtures.

Live-org fixtures skip rather than fail when no org is configured, so the
offline suite stays runnable on a machine that has never seen Salesforce.
"""

from __future__ import annotations

import uuid
import warnings

import pytest

from crm_companion.config import Settings
from crm_companion.crm.salesforce_auth import build_token_provider
from crm_companion.crm.salesforce_client import SalesforceClient
from crm_companion.crm.salesforce_provider import SalesforceProvider


@pytest.fixture(scope="session")
def live_settings() -> Settings:
    settings = Settings()  # reads .env
    if not (settings.sf_org_alias or settings.sf_client_id):
        pytest.skip("no Salesforce org configured (set SF_ORG_ALIAS or JWT settings)")
    return settings


@pytest.fixture
async def live_client(live_settings: Settings):
    async with SalesforceClient(
        build_token_provider(live_settings),
        api_version=live_settings.sf_api_version,
        timeout=live_settings.request_timeout_seconds,
    ) as client:
        yield client


@pytest.fixture
async def live_provider(live_client, live_settings: Settings) -> SalesforceProvider:
    return SalesforceProvider(live_client, settings=live_settings)


@pytest.fixture
async def demo_account_id(live_provider, live_settings: Settings) -> str:
    accounts = await live_provider.search_accounts(live_settings.demo_account_name)
    if not accounts:
        pytest.skip("demo account not seeded: run `python -m scripts.seed_org`")
    return accounts[0].id


@pytest.fixture
def unique_key() -> str:
    """Fresh idempotency key so reruns never collide with earlier records."""
    return f"test-{uuid.uuid4()}"


@pytest.fixture
async def cleanup(live_client):
    """Records registered here are deleted even if the test fails."""
    created: list[tuple[str, str]] = []

    def register(sobject: str, record_id: str) -> None:
        created.append((sobject, record_id))

    yield register

    for sobject, record_id in reversed(created):
        try:
            await live_client.delete(sobject, record_id)
        except Exception as exc:  # noqa: BLE001 - must not mask the real failure
            # Warn rather than swallow: leftover records in the org will make a
            # later run's count assertions fail for no visible reason.
            warnings.warn(
                f"cleanup failed for {sobject} {record_id}: {exc}",
                stacklevel=1,
            )
