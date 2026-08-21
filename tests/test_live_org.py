"""Assertions that only a real org can make.

Every test here guards an assumption that mocks cannot verify: whether a field
actually exists and is readable, whether a picklist value behaves as Open,
whether Salesforce really dedupes an upsert, whether a Mention segment survives
a round trip. Each was proven once by hand during development; this is what
stops it silently regressing.

    pytest -m liveorg
"""

from __future__ import annotations

import pytest

from crm_companion.crm.models import WriteOutcome

pytestmark = pytest.mark.liveorg


class TestOrgShape:
    async def test_authenticates(self, live_client):
        limits = await live_client._request("GET", f"{live_client.data_path}/limits")
        assert "DailyApiRequests" in limits

    async def test_custom_fields_exist_and_are_readable(self, live_client, live_settings):
        """A field without FLS is silently absent from describe, not an error."""
        opportunity = await live_client.describe("Opportunity")
        names = {f["name"] for f in opportunity["fields"]}
        assert live_settings.sf_field_customer_need in names
        assert live_settings.sf_field_comments in names

        for sobject in ("Task", "Event"):
            described = await live_client.describe(sobject)
            fields = {f["name"] for f in described["fields"]}
            assert live_settings.sf_field_idempotency in fields, (
                f"{sobject} is missing the idempotency field. It is defined on the "
                "shared Activity object; check the deploy and the permission set."
            )

    async def test_idempotency_field_is_a_unique_external_id(self, live_client, live_settings):
        described = await live_client.describe("Task")
        field = next(
            f for f in described["fields"] if f["name"] == live_settings.sf_field_idempotency
        )
        assert field["externalId"] is True
        assert field["unique"] is True

    async def test_ledger_object_exists(self, live_client, live_settings):
        described = await live_client.describe(live_settings.sf_ledger_object)
        names = {f["name"] for f in described["fields"]}
        assert {"Operation__c", "Target_Record_Id__c", "Result_Record_Id__c"} <= names

    async def test_bidding_stage_exists_and_is_open(self, live_client):
        """A Closed stage would silently remove those records from past-due queries."""
        stages = await live_client.picklist_values("Opportunity", "StageName")
        assert "Bidding" in stages

        open_bidding = await live_client.count(
            "SELECT COUNT(Id) FROM Opportunity WHERE StageName = 'Bidding' AND IsClosed = true"
        )
        assert open_bidding == 0, "Bidding is configured as a Closed stage"


class TestIdempotency:
    async def test_repeated_upsert_creates_one_record(
        self, live_client, live_settings, unique_key, cleanup
    ):
        payload = {"Subject": "live test - idempotency", "Status": "Completed"}

        first = await live_client.upsert_by_external_id(
            "Task", live_settings.sf_field_idempotency, unique_key, payload
        )
        cleanup("Task", first.id)
        second = await live_client.upsert_by_external_id(
            "Task", live_settings.sf_field_idempotency, unique_key, payload
        )

        assert first.created is True
        assert second.created is False
        assert first.id == second.id

        total = await live_client.count(
            "SELECT COUNT(Id) FROM Task WHERE "  # noqa: S608 - key is a generated UUID
            f"{live_settings.sf_field_idempotency} = '{unique_key}'"
        )
        assert total == 1

    async def test_provider_reports_replay(self, live_provider, unique_key, cleanup):
        first = await live_provider.create_task(
            subject="live test - provider replay", idempotency_key=unique_key
        )
        cleanup("Task", first.record_id)
        second = await live_provider.create_task(
            subject="live test - provider replay", idempotency_key=unique_key
        )

        assert first.outcome is WriteOutcome.CREATED
        assert second.outcome is WriteOutcome.REPLAYED


class TestChatterMentions:
    async def test_mention_segment_survives_round_trip(
        self, live_client, live_provider, live_settings, demo_account_id, unique_key, cleanup
    ):
        """Text that merely looks like a mention notifies nobody, and does not error."""
        resolution = await live_provider.resolve_user(live_settings.demo_mention_name or "Chatter")
        if not resolution.is_unique:
            pytest.skip("no unambiguous Chatter-capable user to mention")

        opportunities = await live_provider.list_open_opportunities(demo_account_id, limit=1)
        target = opportunities[0]

        result = await live_provider.post_chatter_update(
            record_id=target.id,
            text="Live test - please ignore.",
            idempotency_key=unique_key,
            mention_user_ids=(resolution.only.id,),
        )
        assert result.outcome is WriteOutcome.CREATED
        feed_id = result.record_id

        try:
            posted = await live_client._request(
                "GET", f"{live_client.data_path}/chatter/feed-elements/{feed_id}"
            )
            segments = posted["body"]["messageSegments"]
            kinds = [segment["type"] for segment in segments]

            assert "Mention" in kinds, "mention was downgraded to text; nobody was notified"
            mention = next(s for s in segments if s["type"] == "Mention")
            assert mention["record"]["id"].startswith(resolution.only.id[:15])
        finally:
            await live_client._request(
                "DELETE", f"{live_client.data_path}/chatter/feed-elements/{feed_id}"
            )

    async def test_replayed_post_does_not_duplicate(
        self, live_client, live_provider, demo_account_id, unique_key, cleanup
    ):
        opportunities = await live_provider.list_open_opportunities(demo_account_id, limit=1)
        target = opportunities[0]

        first = await live_provider.post_chatter_update(
            record_id=target.id, text="Live test - replay.", idempotency_key=unique_key
        )
        second = await live_provider.post_chatter_update(
            record_id=target.id, text="Live test - replay.", idempotency_key=unique_key
        )

        try:
            assert first.outcome is WriteOutcome.CREATED
            assert second.outcome is WriteOutcome.REPLAYED
            assert second.record_id == first.record_id
        finally:
            await live_client._request(
                "DELETE", f"{live_client.data_path}/chatter/feed-elements/{first.record_id}"
            )


class TestResolution:
    async def test_spoken_shorthand_resolves_to_real_picklist_value(self, live_provider):
        resolved = await live_provider.resolve_stage("proposal")
        assert resolved.is_unique
        assert resolved.only == "Proposal/Price Quote"

    async def test_ambiguous_stage_is_not_guessed(self, live_provider):
        resolved = await live_provider.resolve_stage("closed")
        assert resolved.is_ambiguous

    async def test_identity_licence_users_are_excluded(self, live_client, live_provider):
        """They resolve by name but can never receive a notification."""
        rows = await live_client.query(
            "SELECT Name FROM User WHERE IsActive = true "
            "AND Profile.UserLicense.Name = 'Identity' LIMIT 1"
        )
        if not rows:
            pytest.skip("org has no Identity-licence users to exclude")

        name = rows[0]["Name"]
        assert (await live_provider.resolve_user(name)).is_unresolved


class TestSeededPipeline:
    async def test_aggregate_counts_match_the_seed(self, live_provider, demo_account_id):
        summary = await live_provider.get_pipeline_summary(demo_account_id)
        assert summary.open_count == 14
        assert summary.past_due_count == 6

    async def test_past_due_list_is_ordered_oldest_first(self, live_provider, demo_account_id):
        overdue = await live_provider.list_past_due_opportunities(demo_account_id)
        assert len(overdue) == 6
        close_dates = [o.close_date for o in overdue]
        assert close_dates == sorted(close_dates)

    async def test_pipeline_age_is_meaningful(self, live_provider, demo_account_id):
        """Without writable audit fields every record is created today."""
        summary = await live_provider.get_pipeline_summary(demo_account_id)
        assert summary.oldest_open_created is not None
        from datetime import UTC, datetime

        age_days = (datetime.now(UTC) - summary.oldest_open_created).days
        assert age_days > 180, "seeded creation dates look like today; audit fields not writable"


class TestNotesRoundTrip:
    async def test_customer_need_is_written_verbatim(
        self, live_provider, live_settings, demo_account_id
    ):
        """Supply chain manufactures from this field, so a paraphrase is a defect."""
        opportunities = await live_provider.list_open_opportunities(demo_account_id, limit=1)
        target = opportunities[0]
        original = target.customer_need

        spoken = 'Wide plank finish in slate gray, about 1,200 sq ft; verbatim check "quoted".'
        try:
            await live_provider.update_opportunity_notes(target.id, customer_need=spoken)
            reread = await live_provider.get_opportunity(target.id)
            assert reread.customer_need == spoken
        finally:
            await live_provider.update_opportunity_notes(target.id, customer_need=original or "")
