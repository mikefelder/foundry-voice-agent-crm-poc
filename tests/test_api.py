import json

import pytest
from fastapi.testclient import TestClient
from openapi_spec_validator import validate
from starlette.websockets import WebSocketDisconnect

from crm_companion.api.app import create_app
from crm_companion.api.openapi import build_spec
from crm_companion.config import Settings
from crm_companion.tools.registry import TOOLS, tool_names

API_KEY = "test-key-1234"
ACCOUNT_ID = "001000000000001"
OPP_ID = "006000000000001"
HEADERS = {"x-api-key": API_KEY}


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        crm_provider="fake",
        tool_api_key=API_KEY,
        sf_instance_url="https://example.my.salesforce.com",
    )


@pytest.fixture
def client(settings: Settings):
    with TestClient(create_app(settings)) as test_client:
        yield test_client


class TestAuth:
    def test_missing_key_is_rejected(self, client):
        response = client.post("/tools/list_tasks", json={})
        assert response.status_code == 401

    def test_wrong_key_is_rejected(self, client):
        response = client.post("/tools/list_tasks", headers={"x-api-key": "nope"}, json={})
        assert response.status_code == 401

    def test_unconfigured_key_rejects_everything(self):
        app = create_app(Settings(_env_file=None, crm_provider="fake", tool_api_key=None))
        with TestClient(app) as unconfigured:
            response = unconfigured.post("/tools/list_tasks", headers={"x-api-key": ""}, json={})
            assert response.status_code == 401


class TestRoutes:
    def test_every_tool_has_a_route(self, client):
        paths = set(client.app.openapi()["paths"])
        assert paths == {f"/tools/{name}" for name in tool_names()}

    def test_reads_answer_from_the_recorded_pipeline(self, client):
        response = client.post(
            "/tools/get_pipeline_summary", headers=HEADERS, json={"account_id": ACCOUNT_ID}
        )
        body = response.json()

        assert response.status_code == 200
        assert body["open_count"] == 14
        assert body["past_due_count"] == 6

    def test_missing_record_is_reported_as_not_found(self, client):
        response = client.post(
            "/tools/get_opportunity", headers=HEADERS, json={"opportunity_id": "006000000000999"}
        )
        assert response.status_code == 404

    def test_ambiguous_stage_is_a_conflict(self, client):
        response = client.post(
            "/tools/update_opportunity",
            headers=HEADERS,
            json={
                "opportunity_id": OPP_ID,
                "stage": "closed",
                # Rejected for ambiguity before the token is ever checked.
                "confirmation_token": "0" * 64,
            },
        )

        assert response.status_code == 409
        assert "ask which one" in response.json()["detail"]

    def test_writing_without_a_preview_is_a_conflict(self, client):
        response = client.post(
            "/tools/update_opportunity_notes",
            headers=HEADERS,
            json={
                "opportunity_id": OPP_ID,
                "customer_need": "never read back",
                "confirmation_token": "0" * 64,
            },
        )

        assert response.status_code == 409
        assert "not previewed" in response.json()["detail"]

    def test_invented_id_never_reaches_a_handler(self, client):
        response = client.post(
            "/tools/get_opportunity", headers=HEADERS, json={"opportunity_id": "the-northgate-one"}
        )
        assert response.status_code == 422

    def test_unknown_fields_are_rejected(self, client):
        response = client.post(
            "/tools/get_opportunity",
            headers=HEADERS,
            json={"opportunity_id": OPP_ID, "drop_table": True},
        )
        assert response.status_code == 422

    def test_repeated_create_is_replayed_across_requests(self, client):
        body = {"subject": "Send pricing"}
        first = client.post("/tools/create_task", headers=HEADERS, json=body)
        second = client.post("/tools/create_task", headers=HEADERS, json=body)

        assert first.json()["outcome"] == "created"
        assert second.json()["outcome"] == "replayed"
        assert second.json()["record_id"] == first.json()["record_id"]


class TestRecordLinks:
    def test_a_write_puts_a_link_on_screen(self, client):
        """Voice Live never tells the browser about tool calls, so the API pushes it."""
        from crm_companion.api.links import subscribe

        with subscribe() as queue:
            client.post("/tools/create_task", headers=HEADERS, json={"subject": "Send pricing"})
            link = queue.get_nowait()

        assert link["label"] == "Task created"
        assert link["url"].startswith("https://example.my.salesforce.com/")

    def test_a_read_publishes_nothing(self, client):
        from crm_companion.api.links import subscribe

        with subscribe() as queue:
            client.post("/tools/list_tasks", headers=HEADERS, json={})
            assert queue.empty()


class TestWebClient:
    def test_serves_the_browser_client(self, client):
        response = client.get("/")

        assert response.status_code == 200
        assert "CRM Sales Companion" in response.text

    @pytest.mark.parametrize("payload", [{"key": "nope"}, {}], ids=["wrong", "missing"])
    def test_socket_grants_no_session_without_the_key(self, client, payload):
        with pytest.raises(WebSocketDisconnect):  # noqa: PT012
            with client.websocket_connect("/ws/voice") as socket:
                socket.send_text(json.dumps(payload))
                # A valid key answers with {"type": "ready"}; this must never arrive.
                socket.receive_text()


class TestSpec:
    def test_document_is_valid(self, settings):
        validate(build_spec(settings))

    def test_auth_is_a_security_scheme_not_a_parameter(self, settings):
        spec = build_spec(settings)
        operation = spec["paths"]["/tools/search_accounts"]["post"]

        assert "APIKeyHeader" in spec["components"]["securitySchemes"]
        assert operation["security"] == [{"APIKeyHeader": []}]
        # A fillable header parameter is a credential the model could invent.
        assert not any(p["name"] == "x-api-key" for p in operation.get("parameters", []))

    def test_operation_ids_are_the_tool_names(self, settings):
        spec = build_spec(settings)
        operation_ids = {
            operation["operationId"]
            for path in spec["paths"].values()
            for operation in path.values()
        }
        assert operation_ids == set(tool_names())

    def test_every_operation_carries_its_description(self, settings):
        spec = build_spec(settings)
        for tool in TOOLS:
            assert spec["paths"][f"/tools/{tool.name}"]["post"]["description"] == tool.description

    def test_writes_are_tagged_separately(self, settings):
        spec = build_spec(settings)
        for tool in TOOLS:
            expected = ["write"] if tool.is_write else ["read"]
            assert spec["paths"][f"/tools/{tool.name}"]["post"]["tags"] == expected
