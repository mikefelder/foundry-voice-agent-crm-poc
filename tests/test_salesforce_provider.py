import json
from datetime import date
from decimal import Decimal

import httpx
import pytest

from crm_companion.config import Settings
from crm_companion.crm.models import WriteOutcome
from crm_companion.crm.provider import narrowest_stage_matches
from crm_companion.crm.salesforce_auth import Credentials
from crm_companion.crm.salesforce_client import SalesforceClient
from crm_companion.crm.salesforce_provider import (
    SalesforceProvider,
    _build_segments,
)

STAGES = (
    "Prospecting",
    "Qualification",
    "Needs Analysis",
    "Value Proposition",
    "Proposal/Price Quote",
    "Negotiation/Review",
    "Bidding",
    "Closed Won",
    "Closed Lost",
)

ACCOUNT_ID = "001A000001abcDE"
OPP_ID = "006A000001abcDE"
USER_ID = "005A000001abcDE"


class StubTokens:
    async def get(self):
        return Credentials(instance_url="https://example.my.salesforce.com", access_token="t")

    async def refresh(self):
        return await self.get()


def make_provider(handler) -> SalesforceProvider:
    client = SalesforceClient(StubTokens(), api_version="v62.0")
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return SalesforceProvider(client, settings=Settings(_env_file=None))


class TestStageMatching:
    @pytest.mark.parametrize(
        ("spoken", "expected"),
        [
            ("Bidding", ("Bidding",)),
            ("bidding", ("Bidding",)),
            ("proposal", ("Proposal/Price Quote",)),
            ("Proposal Price Quote", ("Proposal/Price Quote",)),
            ("negotiation", ("Negotiation/Review",)),
            ("neg", ("Negotiation/Review",)),
            ("value prop", ("Value Proposition",)),
        ],
    )
    def test_resolves_spoken_shorthand(self, spoken, expected):
        assert narrowest_stage_matches(spoken, STAGES) == expected

    def test_ambiguity_is_preserved_not_guessed(self):
        assert narrowest_stage_matches("closed", STAGES) == ("Closed Won", "Closed Lost")

    def test_unknown_stage_yields_nothing(self):
        assert narrowest_stage_matches("banana", STAGES) == ()

    def test_empty_input_yields_nothing(self):
        assert narrowest_stage_matches("   ", STAGES) == ()

    def test_exact_match_wins_over_prefix(self):
        # "Closed Won" is an exact hit and must not drag in "Closed Lost".
        assert narrowest_stage_matches("Closed Won", STAGES) == ("Closed Won",)


class TestSegmentBuilding:
    def test_plain_text_when_no_mentions(self):
        assert _build_segments("hello", ()) == [{"type": "Text", "text": "hello"}]

    def test_single_mention_is_structured(self):
        segments = _build_segments("Pricing sent.", (USER_ID,))
        assert segments[-1] == {"type": "Mention", "id": USER_ID}
        assert any(s["type"] == "Mention" for s in segments)

    def test_multiple_mentions_are_separated(self):
        segments = _build_segments("done", (USER_ID, "005A000001abcDF"))
        mentions = [s for s in segments if s["type"] == "Mention"]
        assert len(mentions) == 2


class TestResolveUser:
    def _handler(self, rows):
        def handler(request):
            return httpx.Response(200, json={"records": rows, "done": True})

        return handler

    async def test_excludes_licences_that_cannot_receive_mentions(self):
        rows = [
            {
                "Id": USER_ID,
                "Name": "Real Person",
                "Profile": {"UserLicense": {"Name": "Salesforce"}},
            },
            {
                "Id": "005A000001abcDF",
                "Name": "Identity Person",
                "Profile": {"UserLicense": {"Name": "Identity"}},
            },
        ]
        provider = make_provider(self._handler(rows))
        result = await provider.resolve_user("Person")

        assert result.is_unique
        assert result.only.name == "Real Person"

    async def test_chatter_free_is_allowed(self):
        rows = [
            {
                "Id": USER_ID,
                "Name": "Chatter Expert",
                "Profile": {"UserLicense": {"Name": "Chatter Free"}},
            }
        ]
        provider = make_provider(self._handler(rows))
        assert (await provider.resolve_user("Chatter")).is_unique

    async def test_ambiguity_is_reported(self):
        rows = [
            {"Id": USER_ID, "Name": "A B", "Profile": {"UserLicense": {"Name": "Salesforce"}}},
            {
                "Id": "005A000001abcDF",
                "Name": "A C",
                "Profile": {"UserLicense": {"Name": "Salesforce"}},
            },
        ]
        provider = make_provider(self._handler(rows))
        assert (await provider.resolve_user("A")).is_ambiguous

    async def test_no_eligible_users_is_unresolved(self):
        rows = [
            {
                "Id": USER_ID,
                "Name": "Identity Person",
                "Profile": {"UserLicense": {"Name": "Identity"}},
            }
        ]
        provider = make_provider(self._handler(rows))
        assert (await provider.resolve_user("Identity")).is_unresolved


class TestPipelineSummary:
    async def test_reads_counts_from_aggregates(self):
        def handler(request):
            q = request.url.params.get("q", "")
            if "MIN(CreatedDate)" in q:
                return httpx.Response(
                    200,
                    json={
                        "records": [
                            {
                                "total": 14,
                                "oldest": "2025-03-12T19:30:34.000+0000",
                                "amount": 1642300,
                            }
                        ],
                        "done": True,
                    },
                )
            if "CloseDate < TODAY" in q:
                return httpx.Response(200, json={"records": [{"expr0": 6}], "done": True})
            return httpx.Response(
                200,
                json={"records": [{"Id": ACCOUNT_ID, "Name": "Demo Co"}], "done": True},
            )

        summary = await make_provider(handler).get_pipeline_summary(ACCOUNT_ID)
        assert summary.open_count == 14
        assert summary.past_due_count == 6
        assert summary.account_name == "Demo Co"
        assert summary.oldest_open_created.year == 2025
        assert summary.total_open_amount == Decimal("1642300")

    async def test_rejects_malformed_account_id(self):
        provider = make_provider(lambda r: httpx.Response(200, json={"records": []}))
        with pytest.raises(Exception, match="not a valid Salesforce record ID"):
            await provider.get_pipeline_summary("not-an-id")


class TestWrites:
    async def test_update_opportunity_sends_only_supplied_fields(self):
        captured = {}

        def handler(request):
            captured.update(json.loads(request.content))
            return httpx.Response(204)

        result = await make_provider(handler).update_opportunity(
            OPP_ID, close_date=date(2026, 10, 15)
        )
        assert captured == {"CloseDate": "2026-10-15"}
        assert result.outcome is WriteOutcome.UPDATED

    async def test_update_with_nothing_supplied_is_a_noop(self):
        def handler(request):
            raise AssertionError("should not call Salesforce")

        result = await make_provider(handler).update_opportunity(OPP_ID)
        assert result.detail == "no changes requested"

    async def test_notes_are_written_verbatim(self):
        captured = {}

        def handler(request):
            captured.update(json.loads(request.content))
            return httpx.Response(204)

        spoken = "They want the wide plank finish in slate gray, about 1200 sq ft."
        await make_provider(handler).update_opportunity_notes(OPP_ID, customer_need=spoken)
        assert captured["Customer_Need__c"] == spoken

    async def test_create_task_reports_created(self):
        def handler(request):
            return httpx.Response(201, json={"id": "00T1", "created": True})

        result = await make_provider(handler).create_task(
            subject="Send pricing", idempotency_key="k1"
        )
        assert result.outcome is WriteOutcome.CREATED

    async def test_create_task_reports_replay(self):
        def handler(request):
            return httpx.Response(200, json={"id": "00T1", "created": False})

        result = await make_provider(handler).create_task(
            subject="Send pricing", idempotency_key="k1"
        )
        assert result.outcome is WriteOutcome.REPLAYED


class TestChatterLedger:
    async def test_first_post_writes_feed_item(self):
        calls = []

        def handler(request):
            calls.append(str(request.url))
            if "Voice_Write_Log__c/Idempotency_Key__c" in str(request.url):
                return httpx.Response(201, json={"id": "a0L1", "created": True})
            if "feed-elements" in str(request.url):
                return httpx.Response(201, json={"id": "0D51"})
            return httpx.Response(204)

        result = await make_provider(handler).post_chatter_update(
            record_id=OPP_ID,
            text="Pricing sent.",
            idempotency_key="k1",
            mention_user_ids=(USER_ID,),
        )
        assert result.outcome is WriteOutcome.CREATED
        assert result.record_id == "0D51"
        assert any("feed-elements" in c for c in calls)

    async def test_replay_skips_the_post_entirely(self):
        posted = []

        def handler(request):
            url = str(request.url)
            if "Voice_Write_Log__c/Idempotency_Key__c" in url:
                return httpx.Response(200, json={"id": "a0L1", "created": False})
            if "feed-elements" in url:
                posted.append(url)
                return httpx.Response(201, json={"id": "0D52"})
            return httpx.Response(
                200, json={"records": [{"Result_Record_Id__c": "0D51"}], "done": True}
            )

        result = await make_provider(handler).post_chatter_update(
            record_id=OPP_ID, text="Pricing sent.", idempotency_key="k1"
        )
        assert result.outcome is WriteOutcome.REPLAYED
        assert result.record_id == "0D51"  # reports the original post
        assert posted == []  # and never posted a second time
