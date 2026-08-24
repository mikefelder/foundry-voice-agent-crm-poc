from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from crm_companion.crm.fake_provider import FakeCrmProvider, RecordedCrmData
from crm_companion.crm.models import Account, Opportunity, UserRef, WriteOutcome
from crm_companion.crm.provider import CrmProvider

ACCOUNT_ID = "001A000001abcDE"
OPP_ID = "006A000001abcDE"


def make_provider() -> FakeCrmProvider:
    today = date.today()
    return FakeCrmProvider(
        accounts=(Account(id=ACCOUNT_ID, name="Demo Building Supply"),),
        opportunities=(
            Opportunity(
                id=OPP_ID,
                account_id=ACCOUNT_ID,
                name="Northgate Commons",
                stage="Bidding",
                close_date=today - timedelta(days=10),
                created_date=datetime(2025, 3, 12, tzinfo=UTC),
                amount=Decimal("42000"),
            ),
        ),
        users=(UserRef(id="005A000001abcDE", name="Demo User"),),
        stages=("Bidding", "Proposal/Price Quote", "Closed Won", "Closed Lost"),
    )


def test_implements_provider_protocol():
    assert isinstance(make_provider(), CrmProvider)


async def test_reads_and_aggregates_recorded_models():
    provider = make_provider()

    assert (await provider.search_accounts("building"))[0].id == ACCOUNT_ID
    assert (await provider.list_past_due_opportunities(ACCOUNT_ID))[0].id == OPP_ID
    summary = await provider.get_pipeline_summary(ACCOUNT_ID)
    assert summary.open_count == 1
    assert summary.past_due_count == 1
    assert summary.total_open_amount == Decimal("42000")
    assert (await provider.resolve_stage("proposal")).only == "Proposal/Price Quote"
    assert (await provider.resolve_user("demo")).only.name == "Demo User"


async def test_updates_are_visible_on_reread_and_verbatim():
    provider = make_provider()
    spoken = "Wide plank finish in slate gray, about 1,200 sq ft."

    await provider.update_opportunity(OPP_ID, amount=Decimal("50000"))
    await provider.update_opportunity_notes(OPP_ID, customer_need=spoken)

    updated = await provider.get_opportunity(OPP_ID)
    assert updated is not None
    assert updated.amount == Decimal("50000")
    assert updated.customer_need == spoken


async def test_create_replays_are_inert():
    provider = make_provider()

    first = await provider.create_task(subject="Send pricing", idempotency_key="task-1")
    second = await provider.create_task(subject="Ignored replay", idempotency_key="task-1")

    assert first.outcome is WriteOutcome.CREATED
    assert second.outcome is WriteOutcome.REPLAYED
    assert second.record_id == first.record_id
    assert len(await provider.list_tasks()) == 1


async def test_chatter_replays_report_original_post():
    provider = make_provider()

    first = await provider.post_chatter_update(
        record_id=OPP_ID, text="Pricing sent.", idempotency_key="post-1"
    )
    second = await provider.post_chatter_update(
        record_id=OPP_ID, text="Ignored replay", idempotency_key="post-1"
    )

    assert first.outcome is WriteOutcome.CREATED
    assert second.outcome is WriteOutcome.REPLAYED
    assert second.record_id == first.record_id


async def test_recording_round_trip(tmp_path: Path):
    source = make_provider()
    recording = RecordedCrmData(
        recorded_at=datetime.now(UTC),
        accounts=tuple(source._accounts.values()),
        opportunities=tuple(source._opportunities.values()),
        users=source._users,
        stages=source._stages,
    )
    path = tmp_path / "crm.json"

    recording.write(path)
    loaded = FakeCrmProvider.from_file(path)

    assert (await loaded.get_account(ACCOUNT_ID)).name == "Demo Building Supply"
    assert (await loaded.get_opportunity(OPP_ID)).amount == Decimal("42000")


async def test_default_recording_matches_seeded_acceptance_scenario():
    provider = FakeCrmProvider.from_default_recording()
    account = (await provider.search_accounts("building supply"))[0]

    summary = await provider.get_pipeline_summary(account.id)
    overdue = await provider.list_past_due_opportunities(account.id)

    assert summary.open_count == 14
    assert summary.past_due_count == 6
    assert len(overdue) == 6
    assert overdue[0].name == "Northgate Commons Phase 2"
