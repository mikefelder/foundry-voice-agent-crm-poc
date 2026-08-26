"""``CrmProvider`` backed by a live Salesforce org.

Three behaviours here are load-bearing and were each verified against a real org
rather than inferred from documentation:

* counts and dates come from SOQL aggregates, never from counting records in
  Python or asking a model to do arithmetic;
* creates go through upsert-on-External-ID so a replayed voice command is inert,
  with a ledger object standing in for records that cannot carry their own key;
* mention targets are filtered by user licence, because a user who resolves but
  cannot receive a notification is the failure mode with no symptom.

Every write also leaves a ledger row. Provenance and undo want the same record -
one says the change came from the companion, the other needs the values it
replaced - so there is one row carrying both rather than two stores to keep in
step.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import uuid4

from crm_companion.config import Settings, get_settings
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
    WriteOutcome,
    WriteResult,
    as_field_text,
)
from crm_companion.crm.provider import narrowest_stage_matches
from crm_companion.crm.salesforce_client import SalesforceClient, SalesforceError
from crm_companion.crm.salesforce_mapping import FieldMap, parse_datetime, parse_decimal

# Aliased because `record_id` is also a keyword argument on post_chatter_update.
from crm_companion.crm.soql import record_id as validate_id
from crm_companion.crm.soql import soql_literal, sosl_term

__all__ = ["SalesforceProvider"]

logger = logging.getLogger(__name__)

_OPPORTUNITY_FIELD_API_NAMES = {
    "stage": "StageName",
    "close_date": "CloseDate",
    "amount": "Amount",
}

# What a reversal has to remove rather than restore.
_CREATED_BY = {"create_task": "Task", "post_chatter_update": "FeedItem"}


class SalesforceProvider:
    def __init__(
        self,
        client: SalesforceClient,
        *,
        settings: Settings | None = None,
        fields: FieldMap | None = None,
    ) -> None:
        self._sf = client
        self._settings = settings or get_settings()
        self._fields = fields or FieldMap.from_settings(self._settings)

    # ---- reads -------------------------------------------------------------

    async def search_accounts(self, query: str, *, limit: int = 5) -> list[Account]:
        term = sosl_term(query, field="account name")
        rows = await self._sf.search(
            f"FIND {{{term}}} IN NAME FIELDS RETURNING "
            f"Account({FieldMap.ACCOUNT_FIELDS} LIMIT {int(limit)})"
        )
        return [self._fields.to_account(row) for row in rows]

    async def get_account(self, account_id: str) -> Account | None:
        rid = validate_id(account_id, field="account_id")
        row = await self._sf.query_one(
            f"SELECT {FieldMap.ACCOUNT_FIELDS} FROM Account WHERE Id = '{rid}'"  # noqa: S608
        )
        return self._fields.to_account(row) if row else None

    async def get_contact(self, contact_id: str) -> Contact | None:
        rid = validate_id(contact_id, field="contact_id")
        row = await self._sf.query_one(
            f"SELECT {FieldMap.CONTACT_FIELDS} FROM Contact WHERE Id = '{rid}'"  # noqa: S608
        )
        return self._fields.to_contact(row) if row else None

    async def list_contacts(self, account_id: str, *, limit: int = 25) -> list[Contact]:
        rid = validate_id(account_id, field="account_id")
        rows = await self._sf.query(
            f"SELECT {FieldMap.CONTACT_FIELDS} FROM Contact "  # noqa: S608
            f"WHERE AccountId = '{rid}' ORDER BY Name LIMIT {int(limit)}"
        )
        return [self._fields.to_contact(row) for row in rows]

    async def get_pipeline_summary(self, account_id: str) -> PipelineSummary:
        """Every number here is computed by the datastore, not by this process."""
        rid = validate_id(account_id, field="account_id")
        open_filter = f"AccountId = '{rid}' AND IsClosed = false"

        summary_row = await self._sf.query_one(
            "SELECT COUNT(Id) total, MIN(CreatedDate) oldest, SUM(Amount) amount "  # noqa: S608
            f"FROM Opportunity WHERE {open_filter}"
        )
        past_due = await self._sf.count(
            f"SELECT COUNT(Id) FROM Opportunity WHERE {open_filter} AND CloseDate < TODAY"  # noqa: S608
        )
        account = await self.get_account(rid)

        row: dict[str, Any] = summary_row or {}
        return PipelineSummary(
            account_id=rid,
            account_name=account.name if account else None,
            open_count=int(row.get("total") or 0),
            past_due_count=past_due,
            oldest_open_created=parse_datetime(row.get("oldest")),
            total_open_amount=parse_decimal(row.get("amount")),
        )

    async def list_open_opportunities(
        self, account_id: str, *, limit: int = 50
    ) -> list[Opportunity]:
        rid = validate_id(account_id, field="account_id")
        rows = await self._sf.query(
            f"SELECT {self._fields.opportunity_fields} FROM Opportunity "  # noqa: S608
            f"WHERE AccountId = '{rid}' AND IsClosed = false "
            f"ORDER BY CloseDate ASC LIMIT {int(limit)}"
        )
        return [self._fields.to_opportunity(row) for row in rows]

    async def list_past_due_opportunities(
        self, account_id: str, *, limit: int = 25
    ) -> list[Opportunity]:
        """``TODAY`` is evaluated server-side, so no client clock is involved."""
        rid = validate_id(account_id, field="account_id")
        rows = await self._sf.query(
            f"SELECT {self._fields.opportunity_fields} FROM Opportunity "  # noqa: S608
            f"WHERE AccountId = '{rid}' AND IsClosed = false AND CloseDate < TODAY "
            f"ORDER BY CloseDate ASC LIMIT {int(limit)}"
        )
        return [self._fields.to_opportunity(row) for row in rows]

    async def get_opportunity(self, opportunity_id: str) -> Opportunity | None:
        rid = validate_id(opportunity_id, field="opportunity_id")
        row = await self._sf.query_one(
            f"SELECT {self._fields.opportunity_fields} "  # noqa: S608
            f"FROM Opportunity WHERE Id = '{rid}'"
        )
        return self._fields.to_opportunity(row) if row else None

    async def list_tasks(self, *, limit: int = 25) -> list[TaskRecord]:
        rows = await self._sf.query(
            f"SELECT {FieldMap.TASK_FIELDS} FROM Task "  # noqa: S608
            "WHERE IsClosed = false "
            f"ORDER BY ActivityDate ASC NULLS LAST LIMIT {int(limit)}"
        )
        return [self._fields.to_task(row) for row in rows]

    # ---- resolution --------------------------------------------------------

    async def resolve_stage(self, spoken: str) -> StageResolution:
        available = await self._sf.picklist_values("Opportunity", "StageName")
        return StageResolution(
            spoken=spoken,
            matches=narrowest_stage_matches(spoken, available),
            available=available,
        )

    async def resolve_user(self, name: str, *, limit: int = 5) -> UserResolution:
        """Only users whose licence can actually receive a Chatter notification.

        ``/chatter/users`` looks like the right endpoint but returns
        Identity-licence users too, who resolve by name and are then never
        notified. Licence is the signal that holds.
        """
        pattern = soql_literal(f"%{name.strip()}%", field="user name")
        rows = await self._sf.query(
            "SELECT Id, Name, IsActive, Profile.UserLicense.Name FROM User "  # noqa: S608
            f"WHERE IsActive = true AND Name LIKE {pattern} "
            f"ORDER BY Name LIMIT {int(limit) * 4}"
        )

        allowed = {licence.casefold() for licence in self._settings.mention_licenses}
        matches = tuple(
            UserRef(id=row["Id"], name=row["Name"], is_active=True)
            for row in rows
            if _licence_of(row).casefold() in allowed
        )[:limit]
        return UserResolution(query=name, matches=matches)

    # ---- writes ------------------------------------------------------------

    async def update_opportunity(
        self,
        opportunity_id: str,
        *,
        stage: str | None = None,
        close_date: date | None = None,
        amount: Decimal | None = None,
    ) -> WriteResult:
        rid = validate_id(opportunity_id, field="opportunity_id")
        payload: dict[str, Any] = {}
        if stage is not None:
            payload["StageName"] = stage
        if close_date is not None:
            payload["CloseDate"] = close_date.isoformat()
        if amount is not None:
            payload["Amount"] = float(amount)

        if not payload:
            return WriteResult(
                outcome=WriteOutcome.UPDATED, record_id=rid, detail="no changes requested"
            )

        supplied = {"stage": stage, "close_date": close_date, "amount": amount}
        before = await self.get_opportunity(rid)
        await self._sf.update("Opportunity", rid, payload)
        await self._log_write(
            operation="update_opportunity",
            idempotency_key=_event_key("update_opportunity"),
            target_record_id=rid,
            result_record_id=rid,
            previous=_previous_values(before, supplied),
        )
        return WriteResult(outcome=WriteOutcome.UPDATED, record_id=rid)

    async def update_opportunity_notes(
        self,
        opportunity_id: str,
        *,
        comments: str | None = None,
        customer_need: str | None = None,
    ) -> WriteResult:
        """Written verbatim - the manufacturing note must not be paraphrased."""
        rid = validate_id(opportunity_id, field="opportunity_id")
        payload: dict[str, Any] = {}
        if comments is not None:
            payload[self._fields.comments] = comments
        if customer_need is not None:
            payload[self._fields.customer_need] = customer_need

        if not payload:
            return WriteResult(
                outcome=WriteOutcome.UPDATED, record_id=rid, detail="no notes supplied"
            )

        supplied = {"comments": comments, "customer_need": customer_need}
        before = await self.get_opportunity(rid)
        await self._sf.update("Opportunity", rid, payload)
        await self._log_write(
            operation="update_opportunity_notes",
            idempotency_key=_event_key("update_opportunity_notes"),
            target_record_id=rid,
            result_record_id=rid,
            previous=_previous_values(before, supplied),
        )
        return WriteResult(outcome=WriteOutcome.UPDATED, record_id=rid)

    async def create_task(
        self,
        *,
        subject: str,
        idempotency_key: str,
        due_date: date | None = None,
        related_to_id: str | None = None,
        description: str | None = None,
    ) -> WriteResult:
        payload: dict[str, Any] = {"Subject": subject, "Status": "Not Started"}
        if due_date is not None:
            payload["ActivityDate"] = due_date.isoformat()
        if related_to_id is not None:
            payload["WhatId"] = validate_id(related_to_id, field="related_to_id")
        if description is not None:
            payload["Description"] = description

        result = await self._sf.upsert_by_external_id(
            "Task", self._fields.idempotency, idempotency_key, payload
        )
        if result.created:
            await self._log_write(
                operation="create_task",
                idempotency_key=idempotency_key,
                target_record_id=payload.get("WhatId"),
                result_record_id=result.id,
            )
        return WriteResult(
            outcome=WriteOutcome.CREATED if result.created else WriteOutcome.REPLAYED,
            record_id=result.id,
        )

    async def post_chatter_update(
        self,
        *,
        record_id: str,
        text: str,
        idempotency_key: str,
        mention_user_ids: tuple[str, ...] = (),
    ) -> WriteResult:
        """Ledger-gated, because FeedItem cannot carry a custom External ID field."""
        subject = validate_id(record_id, field="record_id")
        mentions = tuple(validate_id(uid, field="mention_user_id") for uid in mention_user_ids)

        ledger = await self._sf.upsert_by_external_id(
            self._fields.ledger_object,
            self._fields.idempotency,
            idempotency_key,
            {
                "Operation__c": "post_chatter_update",
                "Target_Record_Id__c": subject,
                "Source__c": self._settings.write_source,
            },
        )
        if not ledger.created:
            prior = (
                await self._sf.query_one(
                    f"SELECT Result_Record_Id__c, Undone__c FROM {self._fields.ledger_object} "  # noqa: S608
                    f"WHERE Id = '{ledger.id}'"
                )
                or {}
            )
            # An undone post is gone from the feed, so saying it again has to
            # post it again rather than report a replay of something not there.
            if not prior.get("Undone__c"):
                return WriteResult(
                    outcome=WriteOutcome.REPLAYED,
                    record_id=prior.get("Result_Record_Id__c"),
                    detail="this update was already posted",
                )

        feed_id = await self._sf.post_feed_item(
            subject_id=subject, segments=_build_segments(text, mentions)
        )
        await self._sf.update(
            self._fields.ledger_object,
            ledger.id,
            {"Result_Record_Id__c": feed_id, "Undone__c": False},
        )
        return WriteResult(outcome=WriteOutcome.CREATED, record_id=feed_id)

    # ---- provenance and undo -----------------------------------------------

    async def _log_write(
        self,
        *,
        operation: str,
        idempotency_key: str,
        target_record_id: str | None = None,
        result_record_id: str | None = None,
        previous: dict[str, str | None] | None = None,
    ) -> None:
        """Record what the companion just did.

        Failures are logged, not raised: the CRM write has already landed, so
        turning a lost audit row into a reported failure would tell the rep their
        change did not save and invite them to make it twice.
        """
        payload: dict[str, Any] = {
            "Operation__c": operation,
            "Source__c": self._settings.write_source,
            # Reset, so a command re-issued after an undo is undoable again.
            "Undone__c": False,
        }
        if target_record_id:
            payload["Target_Record_Id__c"] = target_record_id
        if result_record_id:
            payload["Result_Record_Id__c"] = result_record_id
        if previous is not None:
            payload["Previous_Values__c"] = json.dumps(previous, separators=(",", ":"))
        try:
            await self._sf.upsert_by_external_id(
                self._fields.ledger_object, self._fields.idempotency, idempotency_key, payload
            )
        except SalesforceError as exc:
            logger.error("write ledger rejected %s: %s", operation, exc)

    async def undo_last_write(self, record_id: str) -> UndoResult:
        rid = validate_id(record_id, field="record_id")
        ledger = self._fields.ledger_object
        row = await self._sf.query_one(
            "SELECT Id, Operation__c, Target_Record_Id__c, Result_Record_Id__c, "  # noqa: S608
            f"Previous_Values__c, Undone__c FROM {ledger} "
            f"WHERE Operation__c != '{UNDO_OPERATION}' "
            # Matched on either end so a task created against a record is reachable
            # from the record the rep is talking about, or from the task itself.
            f"AND (Target_Record_Id__c = '{rid}' OR Result_Record_Id__c = '{rid}') "
            "ORDER BY CreatedDate DESC LIMIT 1"
        )
        if row is None:
            return UndoResult(undone=False, detail="there is nothing to undo on that record")
        # Only ever the single most recent write. Reaching further back would let
        # "undo" repeated over road noise unwind the whole day.
        if row.get("Undone__c"):
            return UndoResult(undone=False, detail="the last change has already been put back")

        operation = row.get("Operation__c") or ""
        target = row.get("Target_Record_Id__c")
        result_id = row.get("Result_Record_Id__c")
        previous: dict[str, str | None] = json.loads(row.get("Previous_Values__c") or "{}")

        if operation in _CREATED_BY:
            if not result_id:
                return UndoResult(undone=False, detail="that change cannot be undone")
            await self._sf.delete(_CREATED_BY[operation], result_id)
            restored: tuple[FieldChange, ...] = ()
            detail = "removed it"
        elif operation in {"update_opportunity", "update_opportunity_notes"} and target:
            restored = await self._restore_opportunity(target, previous)
            detail = None
        else:
            return UndoResult(undone=False, detail="that change cannot be undone")

        await self._sf.update(ledger, row["Id"], {"Undone__c": True})
        await self._log_write(
            operation=UNDO_OPERATION,
            idempotency_key=_event_key(UNDO_OPERATION),
            target_record_id=target,
            result_record_id=result_id,
        )
        return UndoResult(
            undone=True,
            operation=operation,
            record_id=target or result_id,
            restored=restored,
            detail=detail,
        )

    async def _restore_opportunity(
        self, opportunity_id: str, previous: dict[str, str | None]
    ) -> tuple[FieldChange, ...]:
        current = await self.get_opportunity(opportunity_id)
        payload: dict[str, Any] = {}
        restored: list[FieldChange] = []
        for name, value in previous.items():
            if name not in FIELD_LABELS:
                continue
            payload[self._api_name(name)] = (
                float(Decimal(value)) if name == "amount" and value else value
            )
            restored.append(
                FieldChange(
                    field=name,
                    label=FIELD_LABELS[name],
                    before=as_field_text(getattr(current, name, None)),
                    after=value,
                )
            )
        if payload:
            await self._sf.update("Opportunity", opportunity_id, payload)
        return tuple(restored)

    def _api_name(self, field: str) -> str:
        if field == "comments":
            return self._fields.comments
        if field == "customer_need":
            return self._fields.customer_need
        return _OPPORTUNITY_FIELD_API_NAMES[field]


def _event_key(operation: str) -> str:
    """Updates carry no idempotency key of their own, but each one is its own event."""
    return f"{operation}-{uuid4().hex}"


def _previous_values(before: Opportunity | None, supplied: dict[str, Any]) -> dict[str, str | None]:
    """Only the fields this write actually changed, as they were beforehand."""
    if before is None:
        return {}
    return {
        name: as_field_text(getattr(before, name, None))
        for name, value in supplied.items()
        if value is not None
    }


def _build_segments(text: str, mention_ids: tuple[str, ...]) -> list[dict[str, Any]]:
    """Mentions are structured segments; the same text as a string notifies nobody."""
    segments: list[dict[str, Any]] = [{"type": "Text", "text": text}]
    for index, user_id in enumerate(mention_ids):
        segments.append({"type": "Text", "text": " " if index == 0 else ", "})
        segments.append({"type": "Mention", "id": user_id})
    return segments


def _licence_of(row: dict[str, Any]) -> str:
    profile = row.get("Profile") or {}
    licence = profile.get("UserLicense") or {}
    return licence.get("Name") or ""
