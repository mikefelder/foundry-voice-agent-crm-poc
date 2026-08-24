"""In-memory ``CrmProvider`` backed by CRM-neutral recorded data."""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from itertools import count
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from crm_companion.crm.models import (
    Account,
    Contact,
    Opportunity,
    PipelineSummary,
    StageResolution,
    TaskRecord,
    UserRef,
    UserResolution,
    WriteOutcome,
    WriteResult,
)
from crm_companion.crm.provider import narrowest_stage_matches

DEFAULT_RECORDING_PATH = Path(__file__).parents[1] / "data" / "crm_fixture.json"

__all__ = ["DEFAULT_RECORDING_PATH", "FakeCrmProvider", "RecordedCrmData"]


class RecordedCrmData(BaseModel):
    """Versioned, strict snapshot consumed by ``FakeCrmProvider``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    recorded_at: datetime
    accounts: tuple[Account, ...] = ()
    contacts: tuple[Contact, ...] = ()
    opportunities: tuple[Opportunity, ...] = ()
    tasks: tuple[TaskRecord, ...] = ()
    users: tuple[UserRef, ...] = ()
    stages: tuple[str, ...] = ()

    @classmethod
    def load(cls, path: Path) -> RecordedCrmData:
        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.loads(self.model_dump_json())
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


class FakeCrmProvider:
    """A stateful fake with the same observable behavior as the live provider."""

    def __init__(
        self,
        *,
        accounts: tuple[Account, ...] = (),
        contacts: tuple[Contact, ...] = (),
        opportunities: tuple[Opportunity, ...] = (),
        tasks: tuple[TaskRecord, ...] = (),
        users: tuple[UserRef, ...] = (),
        stages: tuple[str, ...] = (),
        today: date | None = None,
    ) -> None:
        self._accounts = {record.id: record for record in accounts}
        self._contacts = {record.id: record for record in contacts}
        self._opportunities = {record.id: record for record in opportunities}
        self._tasks = {record.id: record for record in tasks}
        self._users = users
        self._stages = stages
        self._today = today or date.today()
        self._task_replays: dict[str, str] = {}
        self._chatter_replays: dict[str, str] = {}
        self._ids = count(1)

    @classmethod
    def from_recording(cls, recording: RecordedCrmData) -> FakeCrmProvider:
        return cls(
            accounts=recording.accounts,
            contacts=recording.contacts,
            opportunities=recording.opportunities,
            tasks=recording.tasks,
            users=recording.users,
            stages=recording.stages,
            today=recording.recorded_at.date(),
        )

    @classmethod
    def from_file(cls, path: Path) -> FakeCrmProvider:
        return cls.from_recording(RecordedCrmData.load(path))

    @classmethod
    def from_default_recording(cls) -> FakeCrmProvider:
        return cls.from_file(DEFAULT_RECORDING_PATH)

    async def search_accounts(self, query: str, *, limit: int = 5) -> list[Account]:
        term = query.strip().casefold()
        return [
            account
            for account in sorted(self._accounts.values(), key=lambda item: item.name)
            if term in account.name.casefold()
        ][:limit]

    async def get_account(self, account_id: str) -> Account | None:
        return self._accounts.get(account_id)

    async def get_contact(self, contact_id: str) -> Contact | None:
        return self._contacts.get(contact_id)

    async def list_contacts(self, account_id: str, *, limit: int = 25) -> list[Contact]:
        return [
            contact
            for contact in sorted(self._contacts.values(), key=lambda item: item.name)
            if contact.account_id == account_id
        ][:limit]

    async def get_pipeline_summary(self, account_id: str) -> PipelineSummary:
        open_opportunities = [
            opportunity
            for opportunity in self._opportunities.values()
            if opportunity.account_id == account_id and not opportunity.is_closed
        ]
        created_dates = [
            opportunity.created_date
            for opportunity in open_opportunities
            if opportunity.created_date is not None
        ]
        amounts = [
            opportunity.amount
            for opportunity in open_opportunities
            if opportunity.amount is not None
        ]
        account = self._accounts.get(account_id)
        return PipelineSummary(
            account_id=account_id,
            account_name=account.name if account else None,
            open_count=len(open_opportunities),
            past_due_count=sum(item.is_past_due(self._today) for item in open_opportunities),
            oldest_open_created=min(created_dates) if created_dates else None,
            total_open_amount=sum(amounts, start=Decimal(0)) if amounts else None,
        )

    async def list_open_opportunities(
        self, account_id: str, *, limit: int = 50
    ) -> list[Opportunity]:
        return sorted(
            (
                opportunity
                for opportunity in self._opportunities.values()
                if opportunity.account_id == account_id and not opportunity.is_closed
            ),
            key=_opportunity_order,
        )[:limit]

    async def list_past_due_opportunities(
        self, account_id: str, *, limit: int = 25
    ) -> list[Opportunity]:
        return [
            opportunity
            for opportunity in await self.list_open_opportunities(account_id)
            if opportunity.is_past_due(self._today)
        ][:limit]

    async def get_opportunity(self, opportunity_id: str) -> Opportunity | None:
        return self._opportunities.get(opportunity_id)

    async def list_tasks(self, *, limit: int = 25) -> list[TaskRecord]:
        return sorted(self._tasks.values(), key=_task_order)[:limit]

    async def resolve_user(self, name: str, *, limit: int = 5) -> UserResolution:
        term = name.strip().casefold()
        matches = tuple(
            user
            for user in sorted(self._users, key=lambda item: item.name)
            if user.is_active and term in user.name.casefold()
        )[:limit]
        return UserResolution(query=name, matches=matches)

    async def resolve_stage(self, spoken: str) -> StageResolution:
        return StageResolution(
            spoken=spoken,
            matches=narrowest_stage_matches(spoken, self._stages),
            available=self._stages,
        )

    async def update_opportunity(
        self,
        opportunity_id: str,
        *,
        stage: str | None = None,
        close_date: date | None = None,
        amount: Decimal | None = None,
    ) -> WriteResult:
        opportunity = self._require_opportunity(opportunity_id)
        changes = {
            key: value
            for key, value in {"stage": stage, "close_date": close_date, "amount": amount}.items()
            if value is not None
        }
        if not changes:
            return WriteResult(
                outcome=WriteOutcome.UPDATED,
                record_id=opportunity_id,
                detail="no changes requested",
            )
        self._opportunities[opportunity_id] = opportunity.model_copy(update=changes)
        return WriteResult(outcome=WriteOutcome.UPDATED, record_id=opportunity_id)

    async def update_opportunity_notes(
        self,
        opportunity_id: str,
        *,
        comments: str | None = None,
        customer_need: str | None = None,
    ) -> WriteResult:
        opportunity = self._require_opportunity(opportunity_id)
        changes = {
            key: value
            for key, value in {"comments": comments, "customer_need": customer_need}.items()
            if value is not None
        }
        if not changes:
            return WriteResult(
                outcome=WriteOutcome.UPDATED,
                record_id=opportunity_id,
                detail="no notes supplied",
            )
        self._opportunities[opportunity_id] = opportunity.model_copy(update=changes)
        return WriteResult(outcome=WriteOutcome.UPDATED, record_id=opportunity_id)

    async def create_task(
        self,
        *,
        subject: str,
        idempotency_key: str,
        due_date: date | None = None,
        related_to_id: str | None = None,
        description: str | None = None,
    ) -> WriteResult:
        if prior_id := self._task_replays.get(idempotency_key):
            return WriteResult(outcome=WriteOutcome.REPLAYED, record_id=prior_id)
        task_id = self._next_id("00T")
        self._tasks[task_id] = TaskRecord(
            id=task_id,
            subject=subject,
            due_date=due_date,
            related_to_id=related_to_id,
            status="Not Started",
            description=description,
        )
        self._task_replays[idempotency_key] = task_id
        return WriteResult(outcome=WriteOutcome.CREATED, record_id=task_id)

    async def post_chatter_update(
        self,
        *,
        record_id: str,
        text: str,
        idempotency_key: str,
        mention_user_ids: tuple[str, ...] = (),
    ) -> WriteResult:
        del record_id, text, mention_user_ids
        if prior_id := self._chatter_replays.get(idempotency_key):
            return WriteResult(
                outcome=WriteOutcome.REPLAYED,
                record_id=prior_id,
                detail="this update was already posted",
            )
        feed_id = self._next_id("0D5")
        self._chatter_replays[idempotency_key] = feed_id
        return WriteResult(outcome=WriteOutcome.CREATED, record_id=feed_id)

    def _require_opportunity(self, opportunity_id: str) -> Opportunity:
        try:
            return self._opportunities[opportunity_id]
        except KeyError:
            raise KeyError(f"opportunity not found: {opportunity_id}") from None

    def _next_id(self, prefix: str) -> str:
        return f"{prefix}{next(self._ids):012d}"


def _opportunity_order(opportunity: Opportunity) -> tuple[date, str]:
    return opportunity.close_date or date.max, opportunity.name


def _task_order(task: TaskRecord) -> tuple[date, str]:
    return task.due_date or date.max, task.subject
