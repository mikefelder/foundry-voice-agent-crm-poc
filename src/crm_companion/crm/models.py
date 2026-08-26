"""Domain models.

These are deliberately CRM-neutral: no Salesforce field names leak in here. The
mapping layer translates to and from SObject shapes so the tool contract, the
OpenAPI document, and the agent never depend on a particular backend.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "Account",
    "AccountResolution",
    "Contact",
    "FieldChange",
    "Opportunity",
    "OpportunityDiff",
    "PipelineSummary",
    "StageResolution",
    "TaskRecord",
    "UserRef",
    "UserResolution",
    "WriteOutcome",
    "WriteResult",
]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Account(_Model):
    id: str
    name: str
    industry: str | None = None
    phone: str | None = None
    city: str | None = None
    state: str | None = None


class AccountResolution(_Model):
    """Search hits for a spoken account name.

    Two customers can share a name, or differ only by a suffix nobody says out
    loud - "United Oil & Gas Corp." against "United Oil & Gas, UK". Picking one
    silently answers about the wrong company and, worse, carries that account ID
    into every write that follows.
    """

    query: str
    matches: tuple[Account, ...] = ()

    @property
    def is_unique(self) -> bool:
        return len(self.matches) == 1

    @property
    def is_ambiguous(self) -> bool:
        return len(self.matches) > 1

    @property
    def is_unresolved(self) -> bool:
        return not self.matches

    @property
    def only(self) -> Account:
        if not self.is_unique:
            raise ValueError(f"{len(self.matches)} matches for {self.query!r}; expected exactly 1")
        return self.matches[0]


class Contact(_Model):
    id: str
    account_id: str | None = None
    name: str
    title: str | None = None
    email: str | None = None
    phone: str | None = None


class Opportunity(_Model):
    id: str
    account_id: str
    name: str
    stage: str
    close_date: date | None = None
    created_date: datetime | None = None
    amount: Decimal | None = None
    is_closed: bool = False
    comments: str | None = None
    customer_need: str | None = None

    def is_past_due(self, today: date) -> bool:
        """Past due means still open with a close date already behind us."""
        return not self.is_closed and self.close_date is not None and self.close_date < today


class TaskRecord(_Model):
    id: str
    subject: str
    due_date: date | None = None
    related_to_id: str | None = None
    status: str | None = None
    priority: str | None = None
    description: str | None = None


class PipelineSummary(_Model):
    """Answers 'how many open, how old, how many past due' in one shot.

    Every field here comes from a database aggregate. Nothing in this model is
    derived by counting records in application code or by the model itself.
    """

    account_id: str
    account_name: str | None = None
    open_count: int = Field(ge=0)
    past_due_count: int = Field(ge=0)
    oldest_open_created: datetime | None = None
    total_open_amount: Decimal | None = None


class FieldChange(_Model):
    field: str
    label: str
    before: str | None = None
    after: str | None = None


class OpportunityDiff(_Model):
    opportunity_id: str
    opportunity_name: str
    changes: tuple[FieldChange, ...] = ()

    @property
    def has_changes(self) -> bool:
        return bool(self.changes)


class UserRef(_Model):
    id: str
    name: str
    is_active: bool = True


class UserResolution(_Model):
    """Result of turning a spoken name into a mention target.

    A Chatter mention needs a real user ID; text that merely looks like a mention
    notifies nobody. Ambiguity is returned rather than resolved so the agent can ask.
    """

    query: str
    matches: tuple[UserRef, ...] = ()

    @property
    def is_unique(self) -> bool:
        return len(self.matches) == 1

    @property
    def is_ambiguous(self) -> bool:
        return len(self.matches) > 1

    @property
    def is_unresolved(self) -> bool:
        return not self.matches

    @property
    def only(self) -> UserRef:
        if not self.is_unique:
            raise ValueError(f"{len(self.matches)} matches for {self.query!r}; expected exactly 1")
        return self.matches[0]


class StageResolution(_Model):
    """Spoken stage shorthand mapped onto the org's actual picklist values."""

    spoken: str
    matches: tuple[str, ...] = ()
    available: tuple[str, ...] = ()

    @property
    def is_unique(self) -> bool:
        return len(self.matches) == 1

    @property
    def is_ambiguous(self) -> bool:
        return len(self.matches) > 1

    @property
    def only(self) -> str:
        if not self.is_unique:
            raise ValueError(f"{len(self.matches)} matches for {self.spoken!r}; expected exactly 1")
        return self.matches[0]


class WriteOutcome(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    REPLAYED = "replayed"


class WriteResult(_Model):
    """Outcome of a mutation.

    ``REPLAYED`` means the idempotency key had already been used, so nothing new
    was written. The agent says something different in that case rather than
    silently reporting success twice.
    """

    outcome: WriteOutcome
    record_id: str | None = None
    detail: str | None = None

    @property
    def is_new(self) -> bool:
        return self.outcome is WriteOutcome.CREATED
