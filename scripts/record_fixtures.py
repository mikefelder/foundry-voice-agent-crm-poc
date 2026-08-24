"""Record a sanitized, CRM-neutral fixture from the configured Salesforce org."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from crm_companion.config import Settings, get_settings
from crm_companion.crm.fake_provider import RecordedCrmData
from crm_companion.crm.recording import build_sanitized_recording
from crm_companion.crm.salesforce_auth import build_token_provider
from crm_companion.crm.salesforce_client import SalesforceClient
from crm_companion.crm.salesforce_provider import SalesforceProvider

DEFAULT_OUTPUT = Path("src/crm_companion/data/crm_fixture.json")


async def record(settings: Settings) -> RecordedCrmData:
    token_provider = build_token_provider(settings)
    async with SalesforceClient(
        token_provider,
        api_version=settings.sf_api_version,
        timeout=settings.request_timeout_seconds,
    ) as client:
        provider = SalesforceProvider(client, settings=settings)
        stages = await client.picklist_values("Opportunity", "StageName")
        return await build_sanitized_recording(
            provider,
            account_name=settings.demo_account_name,
            mention_name=settings.demo_mention_name,
            stages=stages,
        )


async def _run(output: Path) -> int:
    recording = await record(get_settings())
    recording.write(output)
    print(
        f"wrote {output}: {len(recording.accounts)} account, "
        f"{len(recording.contacts)} contacts, "
        f"{len(recording.opportunities)} opportunities, "
        f"{len(recording.tasks)} tasks, {len(recording.users)} users"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    return asyncio.run(_run(args.output))


if __name__ == "__main__":
    sys.exit(main())