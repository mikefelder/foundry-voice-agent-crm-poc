"""Tool handlers.

Thin over ``CrmProvider`` by design: anything a backend can answer belongs in the
provider, so the fake and the live org cannot disagree. The preview handler is
the exception — it is the write-safety step and has no backend equivalent.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal
from typing import Any

from crm_companion.crm.models import (
    Account,
    Contact,
    FieldChange,
    Opportunity,
    OpportunityDiff,
    PipelineSummary,
    StageResolution,
    TaskRecord,
    UserResolution,
    WriteResult,
)
from crm_companion.crm.provider import CrmProvider
from crm_companion.tools import RecordNotFound, ToolError
from crm_companion.tools.confirmation import issue_token, require_token
from crm_companion.tools.schemas import (
    CreateTaskParams,
    GetAccountParams,
    GetContactParams,
    GetOpportunityParams,
    ListContactsParams,
    ListOpportunitiesParams,
    ListTasksParams,
    OpportunityPreview,
    PostChatterUpdateParams,
    PreviewOpportunityUpdateParams,
    ResolveStageParams,
    ResolveUserParams,
    SearchAccountsParams,
    UpdateOpportunityNotesParams,
    UpdateOpportunityParams,
)

__all__ = [
    "create_task",
    "get_account",
    "get_contact",
    "get_opportunity",
    "get_pipeline_summary",
    "list_contacts",
    "list_open_opportunities",
    "list_past_due_opportunities",
    "list_tasks",
    "post_chatter_update",
    "preview_opportunity_update",
    "resolve_stage",
    "resolve_user",
    "search_accounts",
    "update_opportunity",
    "update_opportunity_notes",
]


async def search_accounts(provider: CrmProvider, params: SearchAccountsParams) -> list[Account]:
    return await provider.search_accounts(params.query, limit=params.limit)


async def get_account(provider: CrmProvider, params: GetAccountParams) -> Account:
    account = await provider.get_account(params.account_id)
    if account is None:
        raise RecordNotFound(f"no account with id {params.account_id}")
    return account


async def list_contacts(provider: CrmProvider, params: ListContactsParams) -> list[Contact]:
    return await provider.list_contacts(params.account_id, limit=params.limit)


async def get_contact(provider: CrmProvider, params: GetContactParams) -> Contact:
    contact = await provider.get_contact(params.contact_id)
    if contact is None:
        raise RecordNotFound(f"no contact with id {params.contact_id}")
    return contact


async def get_pipeline_summary(provider: CrmProvider, params: GetAccountParams) -> PipelineSummary:
    return await provider.get_pipeline_summary(params.account_id)


async def list_open_opportunities(
    provider: CrmProvider, params: ListOpportunitiesParams
) -> list[Opportunity]:
    return await provider.list_open_opportunities(params.account_id, limit=params.limit)


async def list_past_due_opportunities(
    provider: CrmProvider, params: ListOpportunitiesParams
) -> list[Opportunity]:
    return await provider.list_past_due_opportunities(params.account_id, limit=params.limit)


async def get_opportunity(provider: CrmProvider, params: GetOpportunityParams) -> Opportunity:
    opportunity = await provider.get_opportunity(params.opportunity_id)
    if opportunity is None:
        raise RecordNotFound(f"no opportunity with id {params.opportunity_id}")
    return opportunity


async def list_tasks(provider: CrmProvider, params: ListTasksParams) -> list[TaskRecord]:
    return await provider.list_tasks(limit=params.limit)


async def resolve_user(provider: CrmProvider, params: ResolveUserParams) -> UserResolution:
    return await provider.resolve_user(params.name, limit=params.limit)


async def resolve_stage(provider: CrmProvider, params: ResolveStageParams) -> StageResolution:
    return await provider.resolve_stage(params.spoken)


async def preview_opportunity_update(
    provider: CrmProvider, params: PreviewOpportunityUpdateParams
) -> OpportunityPreview:
    """Read-only. Returns the exact diff the agent must read back before writing."""
    current = await provider.get_opportunity(params.opportunity_id)
    if current is None:
        raise RecordNotFound(f"no opportunity with id {params.opportunity_id}")

    changes: list[FieldChange] = []
    resolution: StageResolution | None = None

    if params.stage is not None:
        resolution = await provider.resolve_stage(params.stage)
        # An ambiguous stage is left out of the diff so it has to be asked aloud.
        if resolution.is_unique and resolution.only != current.stage:
            changes.append(
                FieldChange(
                    field="stage", label="Stage", before=current.stage, after=resolution.only
                )
            )

    if params.close_date is not None and params.close_date != current.close_date:
        changes.append(
            FieldChange(
                field="close_date",
                label="Close Date",
                before=current.close_date.isoformat() if current.close_date else None,
                after=params.close_date.isoformat(),
            )
        )

    if params.amount is not None and params.amount != current.amount:
        changes.append(
            FieldChange(
                field="amount",
                label="Amount",
                before=f"{current.amount:f}" if current.amount is not None else None,
                after=f"{params.amount:f}",
            )
        )

    if params.comments is not None and params.comments != current.comments:
        changes.append(
            FieldChange(
                field="comments",
                label="Comments",
                before=current.comments,
                after=params.comments,
            )
        )

    if params.customer_need is not None and params.customer_need != current.customer_need:
        changes.append(
            FieldChange(
                field="customer_need",
                label="Customer Need",
                before=current.customer_need,
                after=params.customer_need,
            )
        )

    resolved_stage = resolution.only if resolution and resolution.is_unique else None
    tokens: dict[str, str] = {}
    if any(change.field in {"stage", "close_date", "amount"} for change in changes):
        tokens["update_opportunity"] = issue_token(
            "update_opportunity",
            _field_values(current.id, resolved_stage, params.close_date, params.amount),
        )
    if any(change.field in {"comments", "customer_need"} for change in changes):
        tokens["update_opportunity_notes"] = issue_token(
            "update_opportunity_notes",
            _note_values(current.id, params.comments, params.customer_need),
        )

    return OpportunityPreview(
        diff=OpportunityDiff(
            opportunity_id=current.id,
            opportunity_name=current.name,
            changes=tuple(changes),
        ),
        stage=resolution,
        confirmation_tokens=tokens,
    )


def _field_values(
    opportunity_id: str, stage: str | None, close_date: date | None, amount: Decimal | None
) -> dict[str, Any]:
    return {
        "opportunity_id": opportunity_id,
        "stage": stage,
        "close_date": close_date,
        "amount": amount,
    }


def _note_values(
    opportunity_id: str, comments: str | None, customer_need: str | None
) -> dict[str, Any]:
    return {
        "opportunity_id": opportunity_id,
        "comments": comments,
        "customer_need": customer_need,
    }


async def update_opportunity(provider: CrmProvider, params: UpdateOpportunityParams) -> WriteResult:
    stage = await _resolved_stage(provider, params.stage) if params.stage is not None else None
    require_token(
        params.confirmation_token,
        "update_opportunity",
        _field_values(params.opportunity_id, stage, params.close_date, params.amount),
    )
    return await provider.update_opportunity(
        params.opportunity_id,
        stage=stage,
        close_date=params.close_date,
        amount=params.amount,
    )


async def update_opportunity_notes(
    provider: CrmProvider, params: UpdateOpportunityNotesParams
) -> WriteResult:
    require_token(
        params.confirmation_token,
        "update_opportunity_notes",
        _note_values(params.opportunity_id, params.comments, params.customer_need),
    )
    return await provider.update_opportunity_notes(
        params.opportunity_id,
        comments=params.comments,
        customer_need=params.customer_need,
    )


async def create_task(provider: CrmProvider, params: CreateTaskParams) -> WriteResult:
    return await provider.create_task(
        subject=params.subject,
        idempotency_key=_idempotency_key("create_task", params),
        due_date=params.due_date,
        related_to_id=params.related_to_id,
        description=params.description,
    )


async def post_chatter_update(
    provider: CrmProvider, params: PostChatterUpdateParams
) -> WriteResult:
    return await provider.post_chatter_update(
        record_id=params.record_id,
        text=params.text,
        idempotency_key=_idempotency_key("post_chatter_update", params),
        mention_user_ids=params.mention_user_ids,
    )


async def _resolved_stage(provider: CrmProvider, spoken: str) -> str:
    """A stage that is not exactly one match must never reach a write."""
    resolution = await provider.resolve_stage(spoken)
    if resolution.is_unique:
        return resolution.only
    if resolution.is_ambiguous:
        raise ToolError(f"{spoken!r} matches {', '.join(resolution.matches)}; ask which one")
    raise ToolError(f"{spoken!r} is not a stage in this org")


def _idempotency_key(tool: str, params: CreateTaskParams | PostChatterUpdateParams) -> str:
    """Derived from the request so a phrase repeated over road noise writes once."""
    if params.idempotency_key:
        return params.idempotency_key
    payload: dict[str, Any] = params.model_dump(exclude={"idempotency_key"}, mode="json")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"{tool}-{hashlib.sha256(canonical.encode()).hexdigest()[:40]}"
