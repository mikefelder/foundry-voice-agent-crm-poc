"""The provider seam.

Tool handlers depend on this Protocol and nothing below it. Swapping the live
Salesforce implementation for the recorded fake is a configuration change, which
is what lets the test suite and prompt iteration run without network or quota.

Write operations that create records take an ``idempotency_key``; implementations
must make replays inert. Operations that update existing records do not, because
they only accept absolute values and are therefore already idempotent.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Protocol, runtime_checkable

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

__all__ = ["CrmProvider", "narrowest_stage_matches"]

_NON_ALNUM = re.compile(r"[^a-z0-9]")


def narrowest_stage_matches(spoken: str, candidates: tuple[str, ...]) -> tuple[str, ...]:
    """Return only the most-specific matching tier, preserving ambiguity."""
    target = _NON_ALNUM.sub("", spoken.casefold())
    if not target:
        return ()

    if exact := [candidate for candidate in candidates if candidate == spoken]:
        return tuple(exact)
    if insensitive := [
        candidate for candidate in candidates if _NON_ALNUM.sub("", candidate.casefold()) == target
    ]:
        return tuple(insensitive)
    if prefixed := [
        candidate
        for candidate in candidates
        if _NON_ALNUM.sub("", candidate.casefold()).startswith(target)
    ]:
        return tuple(prefixed)
    return tuple(
        candidate for candidate in candidates if target in _NON_ALNUM.sub("", candidate.casefold())
    )


@runtime_checkable
class CrmProvider(Protocol):
    # ---- reads -------------------------------------------------------------

    async def search_accounts(self, query: str, *, limit: int = 5) -> list[Account]: ...

    async def get_account(self, account_id: str) -> Account | None: ...

    async def get_contact(self, contact_id: str) -> Contact | None: ...

    async def list_contacts(self, account_id: str, *, limit: int = 25) -> list[Contact]: ...

    async def get_pipeline_summary(self, account_id: str) -> PipelineSummary:
        """Aggregate counts and dates, computed by the datastore."""

    async def list_open_opportunities(
        self, account_id: str, *, limit: int = 50
    ) -> list[Opportunity]: ...

    async def list_past_due_opportunities(
        self, account_id: str, *, limit: int = 25
    ) -> list[Opportunity]:
        """Open opportunities whose close date has passed, oldest first."""

    async def get_opportunity(self, opportunity_id: str) -> Opportunity | None: ...

    async def list_tasks(self, *, limit: int = 25) -> list[TaskRecord]: ...

    # ---- resolution --------------------------------------------------------

    async def resolve_user(self, name: str, *, limit: int = 5) -> UserResolution:
        """Map a spoken name onto candidate mention targets. Never guesses."""

    async def resolve_stage(self, spoken: str) -> StageResolution:
        """Map spoken stage shorthand onto the org's picklist values."""

    # ---- writes ------------------------------------------------------------

    async def update_opportunity(
        self,
        opportunity_id: str,
        *,
        stage: str | None = None,
        close_date: date | None = None,
        amount: Decimal | None = None,
    ) -> WriteResult:
        """Absolute values only. A delta-shaped API could not be replay-safe."""

    async def update_opportunity_notes(
        self,
        opportunity_id: str,
        *,
        comments: str | None = None,
        customer_need: str | None = None,
    ) -> WriteResult:
        """Written verbatim; ``customer_need`` is consumed downstream by supply chain."""

    async def create_task(
        self,
        *,
        subject: str,
        idempotency_key: str,
        due_date: date | None = None,
        related_to_id: str | None = None,
        description: str | None = None,
    ) -> WriteResult: ...

    async def post_chatter_update(
        self,
        *,
        record_id: str,
        text: str,
        idempotency_key: str,
        mention_user_ids: tuple[str, ...] = (),
    ) -> WriteResult:
        """Posts with structured mention segments so mentioned users are notified."""
