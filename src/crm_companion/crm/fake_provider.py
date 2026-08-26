"""In-memory ``CrmProvider`` backed by CRM-neutral recorded data."""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from itertools import count
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from crm_companion.config import get_settings
from crm_companion.crm.models import (
    FIELD_LABELS,
    UNDO_OPERATION,
    Account,
    Contact,
    FieldChange,
    Opportunity,
    PipelineSummary,
    StageResolution,
    TaskRecord,
    UndoResult,
    UserRef,
    UserResolution,
    WriteLogEntry,
    WriteOutcome,
    WriteResult,
    as_field_text,
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
        source: str | None = None,
    ) -> None:
        self._accounts = {record.id: record for record in accounts}
        self._contacts = {record.id: record for record in contacts}
        self._opportunities = {record.id: record for record in opportunities}
        self._tasks = {record.id: record for record in tasks}
        self._users = users
        self._stages = stages
        self._today = today or date.today()
        self._source = source or get_settings().write_source
        self._task_replays: dict[str, str] = {}
        self._chatter_replays: dict[str, str] = {}
        self._log: list[WriteLogEntry] = []
        self._ids = count(1)
        self._issued_ids = (
            set(self._accounts) | set(self._contacts) | set(self._opportunities) | set(self._tasks)
        )

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
        self._record_write(
            "update_opportunity",
            target_record_id=opportunity_id,
            result_record_id=opportunity_id,
            previous={name: as_field_text(getattr(opportunity, name)) for name in changes},
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
        self._record_write(
            "update_opportunity_notes",
            target_record_id=opportunity_id,
            result_record_id=opportunity_id,
            previous={name: as_field_text(getattr(opportunity, name)) for name in changes},
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
        self._record_write(
            "create_task",
            target_record_id=related_to_id,
            result_record_id=task_id,
            replay_key=idempotency_key,
        )
        return WriteResult(outcome=WriteOutcome.CREATED, record_id=task_id)

    async def post_chatter_update(
        self,
        *,
        record_id: str,
        text: str,
        idempotency_key: str,
        mention_user_ids: tuple[str, ...] = (),
    ) -> WriteResult:
        del text, mention_user_ids
        if prior_id := self._chatter_replays.get(idempotency_key):
            return WriteResult(
                outcome=WriteOutcome.REPLAYED,
                record_id=prior_id,
                detail="this update was already posted",
            )
        feed_id = self._next_id("0D5")
        self._chatter_replays[idempotency_key] = feed_id
        self._record_write(
            "post_chatter_update",
            target_record_id=record_id,
            result_record_id=feed_id,
            replay_key=idempotency_key,
        )
        return WriteResult(outcome=WriteOutcome.CREATED, record_id=feed_id)

    async def undo_last_write(self, record_id: str) -> UndoResult:
        candidates = [
            entry
            for entry in self._log
            if entry.operation != UNDO_OPERATION
            and record_id in {entry.target_record_id, entry.result_record_id}
        ]
        if not candidates:
            return UndoResult(undone=False, detail="there is nothing to undo on that record")
        entry = candidates[-1]
        # Only ever the single most recent write. Reaching further back would let
        # "undo" repeated over road noise unwind the whole day.
        if entry.undone:
            return UndoResult(undone=False, detail="the last change has already been put back")

        restored: tuple[FieldChange, ...] = ()
        detail: str | None = None
        if entry.operation in {"update_opportunity", "update_opportunity_notes"}:
            restored = self._restore_opportunity(entry)
        elif entry.operation == "create_task":
            self._tasks.pop(entry.result_record_id or "", None)
            self._task_replays.pop(entry.replay_key or "", None)
            detail = "removed it"
        elif entry.operation == "post_chatter_update":
            self._chatter_replays.pop(entry.replay_key or "", None)
            detail = "removed it"
        else:
            return UndoResult(undone=False, detail="that change cannot be undone")

        self._log[self._log.index(entry)] = entry.model_copy(update={"undone": True})
        self._record_write(
            UNDO_OPERATION,
            target_record_id=entry.target_record_id,
            result_record_id=entry.result_record_id,
        )
        return UndoResult(
            undone=True,
            operation=entry.operation,
            record_id=entry.target_record_id or entry.result_record_id,
            restored=restored,
            detail=detail,
        )

    def _restore_opportunity(self, entry: WriteLogEntry) -> tuple[FieldChange, ...]:
        opportunity = self._opportunities.get(entry.target_record_id or "")
        if opportunity is None:
            return ()
        changes: dict[str, object] = {}
        restored: list[FieldChange] = []
        for name, value in entry.previous_values.items():
            if name not in FIELD_LABELS:
                continue
            changes[name] = _parse_field(name, value)
            restored.append(
                FieldChange(
                    field=name,
                    label=FIELD_LABELS[name],
                    before=as_field_text(getattr(opportunity, name, None)),
                    after=value,
                )
            )
        if changes:
            self._opportunities[opportunity.id] = opportunity.model_copy(update=changes)
        return tuple(restored)

    def _record_write(
        self,
        operation: str,
        *,
        target_record_id: str | None = None,
        result_record_id: str | None = None,
        previous: dict[str, str | None] | None = None,
        replay_key: str | None = None,
    ) -> None:
        self._log.append(
            WriteLogEntry(
                id=self._next_id("a00"),
                operation=operation,
                source=self._source,
                target_record_id=target_record_id,
                result_record_id=result_record_id,
                previous_values=previous or {},
                replay_key=replay_key,
            )
        )

    @property
    def write_log(self) -> tuple[WriteLogEntry, ...]:
        return tuple(self._log)

    def _require_opportunity(self, opportunity_id: str) -> Opportunity:
        try:
            return self._opportunities[opportunity_id]
        except KeyError:
            raise KeyError(f"opportunity not found: {opportunity_id}") from None

    def _next_id(self, prefix: str) -> str:
        """Skips IDs already in the recording, which would otherwise be overwritten."""
        while True:
            candidate = f"{prefix}{next(self._ids):012d}"
            if candidate not in self._issued_ids:
                self._issued_ids.add(candidate)
                return candidate


def _parse_field(name: str, value: str | None) -> object:
    if value is None:
        return None
    if name == "close_date":
        return date.fromisoformat(value)
    if name == "amount":
        return Decimal(value)
    return value


def _opportunity_order(opportunity: Opportunity) -> tuple[date, str]:
    return opportunity.close_date or date.max, opportunity.name


def _task_order(task: TaskRecord) -> tuple[date, str]:
    return task.due_date or date.max, task.subject
