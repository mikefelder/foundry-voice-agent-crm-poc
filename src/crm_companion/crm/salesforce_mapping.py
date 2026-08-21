"""Translation between Salesforce SObject shapes and the domain model.

Field API names live here rather than scattered through query strings, so
pointing at an org whose custom fields are named differently is a configuration
change. Nothing above this module knows that ``WhatId`` or ``ActivityDate``
exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from crm_companion.config import Settings
from crm_companion.crm.models import Account, Contact, Opportunity, TaskRecord

__all__ = ["FieldMap", "parse_date", "parse_datetime", "parse_decimal"]


@dataclass(frozen=True)
class FieldMap:
    comments: str = "Comments__c"
    customer_need: str = "Customer_Need__c"
    idempotency: str = "Idempotency_Key__c"
    ledger_object: str = "Voice_Write_Log__c"

    @classmethod
    def from_settings(cls, settings: Settings) -> FieldMap:
        return cls(
            comments=settings.sf_field_comments,
            customer_need=settings.sf_field_customer_need,
            idempotency=settings.sf_field_idempotency,
            ledger_object=settings.sf_ledger_object,
        )

    # ---- SELECT clauses ----------------------------------------------------

    ACCOUNT_FIELDS = "Id, Name, Industry, Phone, BillingCity, BillingState"
    CONTACT_FIELDS = "Id, AccountId, Name, Title, Email, Phone"
    TASK_FIELDS = "Id, Subject, ActivityDate, WhatId, Status, Priority, Description"

    @property
    def opportunity_fields(self) -> str:
        return (
            "Id, AccountId, Name, StageName, Amount, CloseDate, CreatedDate, IsClosed, "
            f"{self.comments}, {self.customer_need}"
        )

    # ---- record -> model ---------------------------------------------------

    def to_account(self, row: dict[str, Any]) -> Account:
        return Account(
            id=row["Id"],
            name=row["Name"],
            industry=row.get("Industry"),
            phone=row.get("Phone"),
            city=row.get("BillingCity"),
            state=row.get("BillingState"),
        )

    def to_contact(self, row: dict[str, Any]) -> Contact:
        return Contact(
            id=row["Id"],
            account_id=row.get("AccountId"),
            name=row["Name"],
            title=row.get("Title"),
            email=row.get("Email"),
            phone=row.get("Phone"),
        )

    def to_opportunity(self, row: dict[str, Any]) -> Opportunity:
        return Opportunity(
            id=row["Id"],
            account_id=row.get("AccountId") or "",
            name=row["Name"],
            stage=row.get("StageName") or "",
            close_date=parse_date(row.get("CloseDate")),
            created_date=parse_datetime(row.get("CreatedDate")),
            amount=parse_decimal(row.get("Amount")),
            is_closed=bool(row.get("IsClosed", False)),
            comments=row.get(self.comments),
            customer_need=row.get(self.customer_need),
        )

    def to_task(self, row: dict[str, Any]) -> TaskRecord:
        return TaskRecord(
            id=row["Id"],
            subject=row.get("Subject") or "",
            due_date=parse_date(row.get("ActivityDate")),
            related_to_id=row.get("WhatId"),
            status=row.get("Status"),
            priority=row.get("Priority"),
            description=row.get("Description"),
        )


def parse_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    # Salesforce emits +0000 rather than the +00:00 fromisoformat wants.
    text = str(value).replace("Z", "+00:00")
    if len(text) > 5 and text[-5] in "+-" and ":" not in text[-5:]:
        text = f"{text[:-2]}:{text[-2:]}"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def parse_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
