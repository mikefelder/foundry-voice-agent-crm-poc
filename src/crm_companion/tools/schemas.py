"""Tool inputs and tool-specific outputs.

Record IDs are validated here rather than inside a provider, so an ID the model
invented is rejected at the boundary regardless of which backend is configured.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from crm_companion.crm.models import OpportunityDiff, StageResolution
from crm_companion.crm.soql import record_id

__all__ = [
    "CreateTaskParams",
    "GetAccountParams",
    "GetContactParams",
    "GetOpportunityParams",
    "ListContactsParams",
    "ListOpportunitiesParams",
    "ListTasksParams",
    "OpportunityPreview",
    "PostChatterUpdateParams",
    "PreviewOpportunityUpdateParams",
    "RecordId",
    "ResolveStageParams",
    "ResolveUserParams",
    "SearchAccountsParams",
    "UpdateOpportunityNotesParams",
    "UpdateOpportunityParams",
]

RecordId = Annotated[str, AfterValidator(lambda value: record_id(value, field="record id"))]


class _Schema(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SearchAccountsParams(_Schema):
    query: str = Field(
        min_length=2,
        max_length=200,
        description="Account name as the rep said it.",
    )
    limit: int = Field(default=5, ge=1, le=25)


class GetAccountParams(_Schema):
    account_id: RecordId


class ListContactsParams(_Schema):
    account_id: RecordId
    limit: int = Field(default=25, ge=1, le=50)


class GetContactParams(_Schema):
    contact_id: RecordId


class ListOpportunitiesParams(_Schema):
    account_id: RecordId
    limit: int = Field(default=25, ge=1, le=50)


class GetOpportunityParams(_Schema):
    opportunity_id: RecordId


class ListTasksParams(_Schema):
    limit: int = Field(default=25, ge=1, le=50)


class ResolveUserParams(_Schema):
    name: str = Field(
        min_length=2,
        max_length=200,
        description="Person's name as the rep said it.",
    )
    limit: int = Field(default=5, ge=1, le=25)


class ResolveStageParams(_Schema):
    spoken: str = Field(
        min_length=1,
        max_length=100,
        description="Stage as the rep said it, such as 'proposal' or 'negotiation'.",
    )


class PreviewOpportunityUpdateParams(_Schema):
    """Absolute values only. Every field is what the value should become."""

    opportunity_id: RecordId
    stage: str | None = Field(default=None, max_length=100)
    close_date: date | None = None
    amount: Decimal | None = Field(default=None, ge=0)
    comments: str | None = Field(default=None, max_length=32000)
    customer_need: str | None = Field(
        default=None,
        max_length=32000,
        description="Carried verbatim; supply chain manufactures from this text.",
    )


class OpportunityPreview(_Schema):
    """What a write would change, computed without writing anything."""

    diff: OpportunityDiff
    stage: StageResolution | None = Field(
        default=None,
        description=(
            "Present only when a stage was requested. When it is not unique the "
            "stage change is left out of the diff and must be asked aloud."
        ),
    )


class UpdateOpportunityParams(_Schema):
    """Absolute values only. There is deliberately no way to adjust a value by an amount."""

    opportunity_id: RecordId
    stage: str | None = Field(
        default=None,
        max_length=100,
        description="Spoken stage is resolved here; anything not unique is refused.",
    )
    close_date: date | None = None
    amount: Decimal | None = Field(default=None, ge=0)


class UpdateOpportunityNotesParams(_Schema):
    opportunity_id: RecordId
    comments: str | None = Field(default=None, max_length=32000)
    customer_need: str | None = Field(
        default=None,
        max_length=32000,
        description="Written verbatim; supply chain manufactures from this text.",
    )


class _CreateParams(_Schema):
    idempotency_key: str | None = Field(
        default=None,
        min_length=8,
        max_length=255,
        description=(
            "Optional. Left empty, a key is derived from the request itself, so a "
            "command repeated over road noise creates nothing the second time."
        ),
    )


class CreateTaskParams(_CreateParams):
    subject: str = Field(min_length=1, max_length=255)
    due_date: date | None = None
    related_to_id: RecordId | None = None
    description: str | None = Field(default=None, max_length=32000)


class PostChatterUpdateParams(_CreateParams):
    record_id: RecordId
    text: str = Field(min_length=1, max_length=10000)
    mention_user_ids: tuple[RecordId, ...] = Field(
        default=(),
        max_length=10,
        description="User IDs from resolve_user. A name that was never resolved notifies nobody.",
    )
