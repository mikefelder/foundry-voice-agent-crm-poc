from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from crm_companion.crm.fake_provider import (
    DEFAULT_RECORDING_PATH,
    FakeCrmProvider,
    RecordedCrmData,
)
from crm_companion.crm.models import Account, Contact, TaskRecord, WriteOutcome
from crm_companion.tools import RecordNotFound, ToolError
from crm_companion.tools.registry import TOOLS, get_tool, read_tools, tool_names, write_tools
from crm_companion.tools.schemas import GetOpportunityParams, PreviewOpportunityUpdateParams

ACCOUNT_ID = "001000000000001"
CONTACT_ID = "003000000000001"
OPP_ID = "006000000000001"
TASK_ID = "00T000000000001"
USER_ID = "005000000000001"

# Every tool needs a sample call, so a new tool cannot ship without coverage.
SAMPLE_PARAMS = {
    "search_accounts": {"query": "building"},
    "get_account": {"account_id": ACCOUNT_ID},
    "get_pipeline_summary": {"account_id": ACCOUNT_ID},
    "list_open_opportunities": {"account_id": ACCOUNT_ID},
    "list_past_due_opportunities": {"account_id": ACCOUNT_ID},
    "get_opportunity": {"opportunity_id": OPP_ID},
    "list_contacts": {"account_id": ACCOUNT_ID},
    "get_contact": {"contact_id": CONTACT_ID},
    "list_tasks": {},
    "resolve_user": {"name": "Demo"},
    "resolve_stage": {"spoken": "proposal"},
    "preview_opportunity_update": {"opportunity_id": OPP_ID, "amount": "50000"},
    "update_opportunity": {"opportunity_id": OPP_ID, "amount": "50000"},
    "update_opportunity_notes": {"opportunity_id": OPP_ID, "customer_need": "Slate gray."},
    "create_task": {"subject": "Send pricing"},
    "post_chatter_update": {"record_id": OPP_ID, "text": "Pricing sent."},
    "undo_last_write": {"record_id": OPP_ID},
}


@pytest.fixture
def provider() -> FakeCrmProvider:
    """The recorded pipeline, plus one contact and task so every tool has data."""
    recording = RecordedCrmData.load(DEFAULT_RECORDING_PATH)
    return FakeCrmProvider(
        accounts=recording.accounts,
        contacts=(Contact(id=CONTACT_ID, account_id=ACCOUNT_ID, name="Demo Contact 1"),),
        opportunities=recording.opportunities,
        tasks=(TaskRecord(id=TASK_ID, subject="Demo Task 1", related_to_id=OPP_ID),),
        users=recording.users,
        stages=recording.stages,
        today=recording.recorded_at.date(),
    )


@pytest.fixture
def settings_for_tokens(monkeypatch):
    """Tokens are signed with the API key, so tests need a deterministic one."""
    from crm_companion.config import Settings
    from crm_companion.tools import confirmation

    monkeypatch.setattr(
        confirmation,
        "get_settings",
        lambda: Settings(_env_file=None, tool_api_key="unit-test-signing-key"),
    )


async def _preview(provider, **kwargs):
    tool = get_tool("preview_opportunity_update")
    return await tool.handler(provider, tool.params(opportunity_id=OPP_ID, **kwargs))


class TestRegistry:
    def test_names_are_unique(self):
        assert len(tool_names()) == len(TOOLS)

    def test_every_tool_is_partitioned_by_kind(self):
        assert read_tools() + write_tools() == TOOLS

    def test_reads_are_not_marked_as_writes(self):
        assert all(not tool.is_write for tool in read_tools())

    def test_lookup_rejects_unknown_names(self):
        with pytest.raises(KeyError, match="unknown tool"):
            get_tool("delete_everything")

    def test_sample_params_cover_every_tool(self):
        assert set(SAMPLE_PARAMS) == set(tool_names())


@pytest.mark.parametrize("tool", TOOLS, ids=lambda tool: tool.name)
async def test_every_tool_runs_against_the_recorded_fixture(tool, provider, settings_for_tokens):
    sample = dict(SAMPLE_PARAMS[tool.name])
    if "confirmation_token" in tool.params.model_fields:
        skip = {"opportunity_id", "confirmation_token"}
        preview = await _preview(provider, **{k: v for k, v in sample.items() if k not in skip})
        sample["confirmation_token"] = preview.confirmation_tokens[tool.name]
    params = tool.params.model_validate(sample)
    assert await tool.handler(provider, params) is not None


class TestInputValidation:
    def test_invented_record_id_is_rejected_at_the_boundary(self):
        with pytest.raises(ValidationError, match="not a valid Salesforce record ID"):
            GetOpportunityParams(opportunity_id="the-northgate-one")

    def test_unknown_fields_are_rejected(self):
        with pytest.raises(ValidationError):
            GetOpportunityParams(opportunity_id=OPP_ID, delete=True)

    def test_negative_amount_is_rejected(self):
        with pytest.raises(ValidationError):
            PreviewOpportunityUpdateParams(opportunity_id=OPP_ID, amount=Decimal("-1"))


class TestPipelineSummary:
    async def test_counts_come_from_the_recorded_pipeline(self, provider):
        tool = get_tool("get_pipeline_summary")
        summary = await tool.handler(provider, tool.params(account_id=ACCOUNT_ID))

        assert summary.open_count == 14
        assert summary.past_due_count == 6


class TestPreview:
    async def _preview(self, provider, **kwargs):
        tool = get_tool("preview_opportunity_update")
        return await tool.handler(provider, tool.params(opportunity_id=OPP_ID, **kwargs))

    async def test_writes_nothing(self, provider):
        await self._preview(provider, amount=Decimal("50000"))
        opportunity = await provider.get_opportunity(OPP_ID)
        assert opportunity.amount == Decimal("42000.0")

    async def test_reports_before_and_after(self, provider):
        preview = await self._preview(provider, close_date=date(2026, 10, 15))
        change = preview.diff.changes[0]

        assert change.field == "close_date"
        assert change.before == "2026-04-30"
        assert change.after == "2026-10-15"

    async def test_unchanged_value_is_not_a_change(self, provider):
        # The recording stores 42000.0; the spoken value must not read as a diff.
        preview = await self._preview(provider, amount=Decimal("42000"))
        assert not preview.diff.has_changes

    async def test_note_text_is_carried_verbatim(self, provider):
        spoken = 'Wide plank finish in slate gray, about 1,200 sq ft; "quoted".'
        preview = await self._preview(provider, customer_need=spoken)
        assert preview.diff.changes[0].after == spoken

    async def test_unique_stage_resolves_to_the_org_value(self, provider):
        preview = await self._preview(provider, stage="proposal")
        assert preview.stage.is_unique
        assert preview.diff.changes[0].after == "Proposal/Price Quote"

    async def test_ambiguous_stage_is_withheld_from_the_diff(self, provider):
        preview = await self._preview(provider, stage="closed")

        assert preview.stage.is_ambiguous
        assert not preview.diff.has_changes

    async def test_unknown_stage_is_withheld_from_the_diff(self, provider):
        preview = await self._preview(provider, stage="banana")

        assert preview.stage.matches == ()
        assert not preview.diff.has_changes

    async def test_missing_record_is_reported(self, provider):
        tool = get_tool("preview_opportunity_update")
        with pytest.raises(RecordNotFound):
            await tool.handler(provider, tool.params(opportunity_id="006000000000999"))


class TestAccountResolution:
    async def test_a_single_hit_is_unique(self, provider):
        tool = get_tool("search_accounts")
        result = await tool.handler(provider, tool.params(query="building"))

        assert result.is_unique
        assert result.only.id == ACCOUNT_ID

    async def test_several_hits_are_reported_not_picked(self, provider):
        """Two customers can differ only by a suffix nobody says out loud."""
        from crm_companion.crm.fake_provider import FakeCrmProvider

        crowded = FakeCrmProvider(
            accounts=(
                Account(id="001000000000001", name="United Oil & Gas Corp."),
                Account(id="001000000000002", name="United Oil & Gas, UK"),
                Account(id="001000000000003", name="United Oil & Gas, Singapore"),
            )
        )
        tool = get_tool("search_accounts")
        result = await tool.handler(crowded, tool.params(query="United Oil"))

        assert result.is_ambiguous
        assert len(result.matches) == 3
        with pytest.raises(ValueError, match="expected exactly 1"):
            _ = result.only

    async def test_no_hit_is_unresolved(self, provider):
        tool = get_tool("search_accounts")
        result = await tool.handler(provider, tool.params(query="nowhere"))

        assert result.is_unresolved


class TestWrites:
    def test_only_mutating_tools_are_marked_as_writes(self):
        assert {tool.name for tool in write_tools()} == {
            "update_opportunity",
            "update_opportunity_notes",
            "create_task",
            "post_chatter_update",
            "undo_last_write",
        }

    async def test_absolute_values_are_applied(self, provider, settings_for_tokens):
        preview = await _preview(provider, amount=Decimal("50000"), stage="proposal")
        tool = get_tool("update_opportunity")
        await tool.handler(
            provider,
            tool.params(
                opportunity_id=OPP_ID,
                amount=Decimal("50000"),
                stage="proposal",
                confirmation_token=preview.confirmation_tokens["update_opportunity"],
            ),
        )

        updated = await provider.get_opportunity(OPP_ID)
        assert updated.amount == Decimal("50000")
        assert updated.stage == "Proposal/Price Quote"

    async def test_writing_without_a_preview_is_refused(self, provider, settings_for_tokens):
        """The agent skipped preview on notes in a live run; the API has to be the backstop."""
        tool = get_tool("update_opportunity_notes")
        with pytest.raises(ToolError, match="not previewed"):
            await tool.handler(
                provider,
                tool.params(
                    opportunity_id=OPP_ID,
                    customer_need="slipped past the read-back",
                    confirmation_token="0" * 64,
                ),
            )

        assert (await provider.get_opportunity(OPP_ID)).customer_need is None

    async def test_a_token_cannot_be_reused_for_different_values(
        self, provider, settings_for_tokens
    ):
        preview = await _preview(provider, customer_need="slate gray, 1200 sq ft")
        tool = get_tool("update_opportunity_notes")

        with pytest.raises(ToolError, match="not previewed"):
            await tool.handler(
                provider,
                tool.params(
                    opportunity_id=OPP_ID,
                    customer_need="something the rep never heard back",
                    confirmation_token=preview.confirmation_tokens["update_opportunity_notes"],
                ),
            )

    async def test_a_note_token_does_not_authorise_a_field_write(
        self, provider, settings_for_tokens
    ):
        preview = await _preview(provider, customer_need="slate gray")
        tool = get_tool("update_opportunity")

        with pytest.raises(ToolError, match="not previewed"):
            await tool.handler(
                provider,
                tool.params(
                    opportunity_id=OPP_ID,
                    amount=Decimal("1"),
                    confirmation_token=preview.confirmation_tokens["update_opportunity_notes"],
                ),
            )

    async def test_amount_scale_does_not_break_the_token(self, provider, settings_for_tokens):
        # The agent may echo 50000 or 50000.0; both mean the previewed value.
        preview = await _preview(provider, amount=Decimal("50000"))
        tool = get_tool("update_opportunity")

        result = await tool.handler(
            provider,
            tool.params(
                opportunity_id=OPP_ID,
                amount=Decimal("50000.00"),
                confirmation_token=preview.confirmation_tokens["update_opportunity"],
            ),
        )
        assert result.outcome is WriteOutcome.UPDATED

    async def test_ambiguous_stage_is_refused_before_anything_is_written(
        self, provider, settings_for_tokens
    ):
        tool = get_tool("update_opportunity")
        with pytest.raises(ToolError, match="ask which one"):
            await tool.handler(
                provider,
                tool.params(
                    opportunity_id=OPP_ID,
                    stage="closed",
                    amount=Decimal("1"),
                    confirmation_token="0" * 64,
                ),
            )

        unchanged = await provider.get_opportunity(OPP_ID)
        assert unchanged.stage == "Bidding"
        assert unchanged.amount == Decimal("42000.0")

    async def test_unknown_stage_is_refused(self, provider, settings_for_tokens):
        tool = get_tool("update_opportunity")
        with pytest.raises(ToolError, match="not a stage"):
            await tool.handler(
                provider,
                tool.params(opportunity_id=OPP_ID, stage="banana", confirmation_token="0" * 64),
            )

    async def test_notes_are_written_verbatim(self, provider, settings_for_tokens):
        spoken = 'Wide plank finish in slate gray, about 1,200 sq ft; "quoted".'
        preview = await _preview(provider, customer_need=spoken)
        tool = get_tool("update_opportunity_notes")
        await tool.handler(
            provider,
            tool.params(
                opportunity_id=OPP_ID,
                customer_need=spoken,
                confirmation_token=preview.confirmation_tokens["update_opportunity_notes"],
            ),
        )

        assert (await provider.get_opportunity(OPP_ID)).customer_need == spoken

    async def test_repeated_command_creates_one_task(self, provider):
        tool = get_tool("create_task")
        spoken = tool.params(subject="Send pricing", due_date=date(2026, 9, 1))

        first = await tool.handler(provider, spoken)
        second = await tool.handler(provider, spoken)

        assert first.outcome is WriteOutcome.CREATED
        assert second.outcome is WriteOutcome.REPLAYED
        # One seeded task, plus the one this created.
        assert len(await provider.list_tasks()) == 2

    async def test_a_genuinely_different_task_still_gets_created(self, provider):
        tool = get_tool("create_task")
        await tool.handler(provider, tool.params(subject="Send pricing"))
        result = await tool.handler(provider, tool.params(subject="Book the install date"))

        assert result.outcome is WriteOutcome.CREATED
        assert len(await provider.list_tasks()) == 3

    async def test_supplied_key_wins_over_the_derived_one(self, provider):
        tool = get_tool("create_task")
        await tool.handler(provider, tool.params(subject="First", idempotency_key="voice-key-1"))
        second = await tool.handler(
            provider, tool.params(subject="Second", idempotency_key="voice-key-1")
        )

        assert second.outcome is WriteOutcome.REPLAYED

    async def test_repeated_post_reports_the_original(self, provider):
        tool = get_tool("post_chatter_update")
        spoken = tool.params(record_id=OPP_ID, text="Pricing sent.", mention_user_ids=(USER_ID,))

        first = await tool.handler(provider, spoken)
        second = await tool.handler(provider, spoken)

        assert first.outcome is WriteOutcome.CREATED
        assert second.outcome is WriteOutcome.REPLAYED
        assert second.record_id == first.record_id


class TestProvenance:
    async def test_a_write_records_where_it_came_from(self, provider):
        tool = get_tool("create_task")
        await tool.handler(provider, tool.params(subject="Send pricing"))

        entry = provider.write_log[-1]
        assert entry.operation == "create_task"
        assert entry.source == "CRM Sales Companion"

    async def test_a_replayed_command_does_not_log_a_second_change(self, provider):
        tool = get_tool("create_task")
        spoken = tool.params(subject="Send pricing")
        await tool.handler(provider, spoken)
        await tool.handler(provider, spoken)

        assert len(provider.write_log) == 1

    async def test_reads_leave_no_trace(self, provider):
        tool = get_tool("get_pipeline_summary")
        await tool.handler(provider, tool.params(account_id=ACCOUNT_ID))

        assert provider.write_log == ()


class TestUndo:
    async def _undo(self, provider, record_id=OPP_ID):
        tool = get_tool("undo_last_write")
        return await tool.handler(provider, tool.params(record_id=record_id))

    async def test_nothing_to_undo_is_an_answer_not_an_error(self, provider):
        result = await self._undo(provider)

        assert result.undone is False
        assert result.detail == "there is nothing to undo on that record"

    async def test_undo_does_not_reach_into_another_record(self, provider, settings_for_tokens):
        """An unscoped undo reversed a different session's change in live testing."""
        preview = await _preview(provider, amount=Decimal("500000"))
        write = get_tool("update_opportunity")
        await write.handler(
            provider,
            write.params(
                opportunity_id=OPP_ID,
                amount=Decimal("500000"),
                confirmation_token=preview.confirmation_tokens["update_opportunity"],
            ),
        )

        other = [
            oid for oid in (await provider.list_open_opportunities(ACCOUNT_ID)) if oid.id != OPP_ID
        ][0]
        result = await self._undo(provider, record_id=other.id)

        assert result.undone is False
        assert (await provider.get_opportunity(OPP_ID)).amount == Decimal("500000")

    async def test_a_misheard_amount_goes_back(self, provider, settings_for_tokens):
        preview = await _preview(provider, amount=Decimal("500000"))
        write = get_tool("update_opportunity")
        await write.handler(
            provider,
            write.params(
                opportunity_id=OPP_ID,
                amount=Decimal("500000"),
                confirmation_token=preview.confirmation_tokens["update_opportunity"],
            ),
        )

        result = await self._undo(provider)

        assert result.undone is True
        assert (await provider.get_opportunity(OPP_ID)).amount == Decimal("42000.0")
        restored = {change.field: change for change in result.restored}
        # Read back from the tool, never from the agent's memory of the number.
        assert restored["amount"].label == "Amount"
        assert restored["amount"].after == "42000.0"

    async def test_undo_said_twice_does_not_walk_back_further(self, provider, settings_for_tokens):
        first = await _preview(provider, customer_need="slate gray")
        notes = get_tool("update_opportunity_notes")
        await notes.handler(
            provider,
            notes.params(
                opportunity_id=OPP_ID,
                customer_need="slate gray",
                confirmation_token=first.confirmation_tokens["update_opportunity_notes"],
            ),
        )
        second = await _preview(provider, amount=Decimal("500000"))
        fields = get_tool("update_opportunity")
        await fields.handler(
            provider,
            fields.params(
                opportunity_id=OPP_ID,
                amount=Decimal("500000"),
                confirmation_token=second.confirmation_tokens["update_opportunity"],
            ),
        )

        assert (await self._undo(provider)).undone is True
        repeat = await self._undo(provider)

        assert repeat.undone is False
        # The notes write is older and must survive an "undo" heard twice.
        assert (await provider.get_opportunity(OPP_ID)).customer_need == "slate gray"

    async def test_a_task_created_by_mistake_is_removed(self, provider):
        create = get_tool("create_task")
        created = await create.handler(provider, create.params(subject="Call the wrong person"))
        assert len(await provider.list_tasks()) == 2

        result = await self._undo(provider, record_id=created.record_id)

        assert result.undone is True
        assert len(await provider.list_tasks()) == 1

    async def test_the_same_command_after_an_undo_creates_again(self, provider):
        """Undo removed the record, so the replay guard must not report a hit."""
        create = get_tool("create_task")
        spoken = create.params(subject="Send pricing")
        created = await create.handler(provider, spoken)
        await self._undo(provider, record_id=created.record_id)

        again = await create.handler(provider, spoken)

        assert again.outcome is WriteOutcome.CREATED

    async def test_the_reversal_is_logged_too(self, provider):
        create = get_tool("create_task")
        created = await create.handler(provider, create.params(subject="Send pricing"))
        await self._undo(provider, record_id=created.record_id)

        assert [entry.operation for entry in provider.write_log] == ["create_task", "undo"]
