"""Sanitize live provider data into a commit-safe recording."""

from __future__ import annotations

from datetime import UTC, datetime

from crm_companion.crm.fake_provider import RecordedCrmData
from crm_companion.crm.models import Account, Contact, Opportunity, TaskRecord, UserRef
from crm_companion.crm.provider import CrmProvider

__all__ = ["build_sanitized_recording"]

SAFE_OPPORTUNITY_NAMES = {
    "Northgate Commons Phase 2",
    "Ashwood Commons",
    "Cedar Park Townhomes",
    "Harborview Retail Block",
    "Lakeside Medical Annex",
    "Fairmont Civic Center",
    "Riverbend Apartments",
    "Stonegate Business Park",
    "Elmwood School Expansion",
    "Summit Ridge Estates",
    "Beacon Hill Mixed Use",
    "Willow Creek Phase 1",
    "Maple Grove Distribution",
    "Kingsford Logistics Hub",
}


async def build_sanitized_recording(
    provider: CrmProvider,
    *,
    account_name: str,
    mention_name: str | None,
    stages: tuple[str, ...],
) -> RecordedCrmData:
    matches = await provider.search_accounts(account_name)
    if not matches:
        raise RuntimeError("demo account not found; run `python -m scripts.seed_org` first")

    source_account = matches[0]
    source_contacts = await provider.list_contacts(source_account.id)
    source_opportunities = await provider.list_open_opportunities(source_account.id)
    source_tasks = await provider.list_tasks(limit=100)

    account_id = _fake_id("001", 1)
    opportunity_ids = {
        opportunity.id: _fake_id("006", index)
        for index, opportunity in enumerate(source_opportunities, start=1)
    }
    related_ids = {source_account.id: account_id, **opportunity_ids}

    accounts = (
        Account(
            id=account_id,
            name="Demo Building Supply",
            industry=source_account.industry,
        ),
    )
    contacts = tuple(
        Contact(
            id=_fake_id("003", index),
            account_id=account_id,
            name=f"Demo Contact {index}",
            title=contact.title,
        )
        for index, contact in enumerate(source_contacts, start=1)
    )
    opportunities = tuple(
        Opportunity(
            id=opportunity_ids[opportunity.id],
            account_id=account_id,
            name=(
                opportunity.name
                if opportunity.name in SAFE_OPPORTUNITY_NAMES
                else f"Demo Opportunity {index}"
            ),
            stage=opportunity.stage,
            close_date=opportunity.close_date,
            created_date=opportunity.created_date,
            amount=opportunity.amount,
            is_closed=opportunity.is_closed,
        )
        for index, opportunity in enumerate(source_opportunities, start=1)
    )
    tasks = tuple(
        TaskRecord(
            id=_fake_id("00T", index),
            subject=f"Demo Task {index}",
            due_date=task.due_date,
            related_to_id=related_ids[task.related_to_id],
            status=task.status,
            priority=task.priority,
        )
        for index, task in enumerate(
            (task for task in source_tasks if task.related_to_id in related_ids), start=1
        )
    )

    users: tuple[UserRef, ...] = ()
    if mention_name:
        resolution = await provider.resolve_user(mention_name)
        if resolution.is_unique:
            users = (UserRef(id=_fake_id("005", 1), name="Demo User"),)

    return RecordedCrmData(
        recorded_at=datetime.now(UTC),
        accounts=accounts,
        contacts=contacts,
        opportunities=opportunities,
        tasks=tasks,
        users=users,
        stages=stages,
    )


def _fake_id(prefix: str, index: int) -> str:
    """Return a valid-looking 15-character ID that cannot identify a live record."""
    return f"{prefix}{index:012d}"
