from datetime import UTC, date, datetime
from decimal import Decimal

from crm_companion.crm.fake_provider import FakeCrmProvider
from crm_companion.crm.models import Account, Contact, Opportunity, TaskRecord, UserRef
from crm_companion.crm.recording import build_sanitized_recording

LIVE_ACCOUNT_ID = "001A000001abcDE"
LIVE_OPP_ID = "006A000001abcDE"


async def test_recording_removes_live_ids_and_sensitive_values():
    provider = FakeCrmProvider(
        accounts=(
            Account(
                id=LIVE_ACCOUNT_ID,
                name="Real Customer Name",
                industry="Construction",
                phone="555-0100",
                city="Real City",
            ),
        ),
        contacts=(
            Contact(
                id="003A000001abcDE",
                account_id=LIVE_ACCOUNT_ID,
                name="Real Contact",
                title="Buyer",
                email="person@example.com",
                phone="555-0101",
            ),
        ),
        opportunities=(
            Opportunity(
                id=LIVE_OPP_ID,
                account_id=LIVE_ACCOUNT_ID,
                name="Confidential Project",
                stage="Bidding",
                close_date=date(2026, 8, 1),
                created_date=datetime(2025, 3, 12, tzinfo=UTC),
                amount=Decimal("42000"),
                comments="private comment",
                customer_need="private manufacturing detail",
            ),
        ),
        tasks=(
            TaskRecord(
                id="00TA000001abcDE",
                subject="Call the real customer",
                related_to_id=LIVE_OPP_ID,
                description="private task detail",
            ),
        ),
        users=(UserRef(id="005A000001abcDE", name="Real Employee"),),
        stages=("Bidding",),
    )

    recording = await build_sanitized_recording(
        provider,
        account_name="Real Customer Name",
        mention_name="Real Employee",
        stages=("Bidding",),
    )
    serialized = recording.model_dump_json()

    for sensitive in (
        "Real Customer Name",
        "Real Contact",
        "person@example.com",
        "Confidential Project",
        "private comment",
        "private manufacturing detail",
        "Call the real customer",
        "Real Employee",
        LIVE_ACCOUNT_ID,
        LIVE_OPP_ID,
    ):
        assert sensitive not in serialized

    assert recording.accounts[0].name == "Demo Building Supply"
    assert recording.contacts[0].name == "Demo Contact 1"
    assert recording.opportunities[0].name == "Demo Opportunity 1"
    assert recording.tasks[0].subject == "Demo Task 1"
    assert recording.users[0].name == "Demo User"