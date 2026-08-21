import httpx
import pytest

from crm_companion.crm.salesforce_auth import Credentials
from crm_companion.crm.salesforce_client import (
    SalesforceClient,
    SalesforceError,
    UpsertResult,
)

CREDS = Credentials(instance_url="https://example.my.salesforce.com", access_token="tok-1")


class StubTokens:
    """Counts refreshes so re-auth behaviour is observable."""

    def __init__(self, tokens=("tok-1", "tok-2")):
        self._tokens = list(tokens)
        self.get_calls = 0
        self.refresh_calls = 0

    async def get(self):
        self.get_calls += 1
        return Credentials(instance_url=CREDS.instance_url, access_token=self._tokens[0])

    async def refresh(self):
        self.refresh_calls += 1
        if len(self._tokens) > 1:
            self._tokens.pop(0)
        return Credentials(instance_url=CREDS.instance_url, access_token=self._tokens[0])


def client_with(handler, tokens=None) -> SalesforceClient:
    provider = tokens or StubTokens()
    client = SalesforceClient(provider, api_version="v62.0")
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


class TestQuery:
    async def test_returns_records(self):
        def handler(request):
            assert request.url.params["q"] == "SELECT Id FROM Account"
            return httpx.Response(200, json={"records": [{"Id": "001"}], "done": True})

        async with client_with(handler) as sf:
            assert await sf.query("SELECT Id FROM Account") == [{"Id": "001"}]

    async def test_follows_pagination(self):
        pages = [
            {"records": [{"Id": "1"}], "done": False, "nextRecordsUrl": "/next/1"},
            {"records": [{"Id": "2"}], "done": True},
        ]

        def handler(request):
            return httpx.Response(200, json=pages.pop(0))

        async with client_with(handler) as sf:
            assert [r["Id"] for r in await sf.query("SELECT Id FROM Account")] == ["1", "2"]

    async def test_max_records_truncates(self):
        def handler(request):
            return httpx.Response(
                200, json={"records": [{"Id": str(i)} for i in range(10)], "done": True}
            )

        async with client_with(handler) as sf:
            assert len(await sf.query("SELECT Id FROM Account", max_records=3)) == 3

    async def test_query_one_returns_none_when_empty(self):
        def handler(request):
            return httpx.Response(200, json={"records": [], "done": True})

        async with client_with(handler) as sf:
            assert await sf.query_one("SELECT Id FROM Account") is None


class TestCount:
    async def test_reads_aggregate_alias(self):
        def handler(request):
            return httpx.Response(200, json={"records": [{"expr0": 14}], "done": True})

        async with client_with(handler) as sf:
            assert await sf.count("SELECT COUNT(Id) FROM Opportunity") == 14

    async def test_zero_when_no_rows(self):
        def handler(request):
            return httpx.Response(200, json={"records": [], "done": True})

        async with client_with(handler) as sf:
            assert await sf.count("SELECT COUNT(Id) FROM Opportunity") == 0


class TestUpsert:
    async def test_created_true_on_first_write(self):
        def handler(request):
            assert request.method == "PATCH"
            assert request.url.path.endswith("/Task/Idempotency_Key__c/abc")
            return httpx.Response(201, json={"id": "00T1", "success": True, "created": True})

        async with client_with(handler) as sf:
            result = await sf.upsert_by_external_id("Task", "Idempotency_Key__c", "abc", {})
            assert result == UpsertResult(id="00T1", created=True)

    async def test_created_false_on_replay(self):
        def handler(request):
            return httpx.Response(200, json={"id": "00T1", "success": True, "created": False})

        async with client_with(handler) as sf:
            result = await sf.upsert_by_external_id("Task", "Idempotency_Key__c", "abc", {})
            assert result.created is False
            assert result.id == "00T1"


class TestReauth:
    async def test_401_triggers_refresh_and_retry(self):
        seen: list[str] = []

        def handler(request):
            seen.append(request.headers["Authorization"])
            if len(seen) == 1:
                return httpx.Response(
                    401, json=[{"errorCode": "INVALID_SESSION_ID", "message": "expired"}]
                )
            return httpx.Response(200, json={"records": [], "done": True})

        tokens = StubTokens()
        async with client_with(handler, tokens) as sf:
            await sf.query("SELECT Id FROM Account")

        assert tokens.refresh_calls == 1
        assert seen == ["Bearer tok-1", "Bearer tok-2"]

    async def test_second_401_is_raised(self):
        def handler(request):
            return httpx.Response(
                401, json=[{"errorCode": "INVALID_SESSION_ID", "message": "expired"}]
            )

        with pytest.raises(SalesforceError) as err:
            async with client_with(handler) as sf:
                await sf.query("SELECT Id FROM Account")
        assert err.value.error_code == "INVALID_SESSION_ID"


class TestErrors:
    async def test_parses_salesforce_error_array(self):
        def handler(request):
            return httpx.Response(
                400,
                json=[
                    {
                        "errorCode": "REQUIRED_FIELD_MISSING",
                        "message": "Required fields are missing",
                        "fields": ["Subject"],
                    }
                ],
            )

        with pytest.raises(SalesforceError) as err:
            async with client_with(handler) as sf:
                await sf.create("Task", {})
        assert err.value.error_code == "REQUIRED_FIELD_MISSING"
        assert err.value.fields == ("Subject",)
        assert "Subject" in str(err.value)

    async def test_handles_non_json_error_body(self):
        def handler(request):
            return httpx.Response(500, text="upstream exploded")

        with pytest.raises(SalesforceError, match="upstream exploded"):
            async with client_with(handler) as sf:
                await sf.query("SELECT Id FROM Account")

    async def test_204_returns_empty_dict(self):
        def handler(request):
            return httpx.Response(204)

        async with client_with(handler) as sf:
            await sf.update("Opportunity", "006", {"StageName": "Bidding"})


class TestDescribeCache:
    async def test_describe_is_cached(self):
        calls = []

        def handler(request):
            calls.append(request.url.path)
            return httpx.Response(
                200,
                json={
                    "fields": [
                        {
                            "name": "StageName",
                            "picklistValues": [
                                {"value": "Bidding", "active": True},
                                {"value": "Retired", "active": False},
                            ],
                        }
                    ]
                },
            )

        async with client_with(handler) as sf:
            await sf.describe("Opportunity")
            await sf.describe("Opportunity")
            assert len(calls) == 1

    async def test_picklist_values_skip_inactive(self):
        def handler(request):
            return httpx.Response(
                200,
                json={
                    "fields": [
                        {
                            "name": "StageName",
                            "picklistValues": [
                                {"value": "Bidding", "active": True},
                                {"value": "Retired", "active": False},
                            ],
                        }
                    ]
                },
            )

        async with client_with(handler) as sf:
            assert await sf.picklist_values("Opportunity", "StageName") == ("Bidding",)


class TestChatter:
    async def test_posts_structured_segments(self):
        captured = {}

        def handler(request):
            import json as _json

            captured.update(_json.loads(request.content))
            return httpx.Response(201, json={"id": "0D5xx"})

        segments = [
            {"type": "Text", "text": "Pricing sent. "},
            {"type": "Mention", "id": "005xx"},
        ]
        async with client_with(handler) as sf:
            assert await sf.post_feed_item(subject_id="006xx", segments=segments) == "0D5xx"

        assert captured["feedElementType"] == "FeedItem"
        assert captured["subjectId"] == "006xx"
        assert captured["body"]["messageSegments"][1] == {"type": "Mention", "id": "005xx"}
