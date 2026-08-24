import json

import pytest

from crm_companion.agent.instructions import INSTRUCTIONS
from crm_companion.agent.provision import (
    SESSION_METADATA_KEY,
    build_definition,
    build_metadata,
    provision,
)
from crm_companion.agent.voicelive_config import METADATA_VALUE_LIMIT, reassemble_metadata
from crm_companion.config import ConfigError, Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        project_endpoint="https://acct.services.ai.azure.com/api/projects/proj",
        project_name="proj",
        model_deployment_name="gpt-realtime",
        agent_name="crm-sales-companion",
    )


class StubAgents:
    def __init__(self):
        self.calls = []

    def create_version(self, agent_name, **kwargs):
        self.calls.append({"agent_name": agent_name, **kwargs})
        return {"name": agent_name, "version": "3"}


class StubClient:
    def __init__(self):
        self.agents = StubAgents()
        self.closed = False

    def close(self):
        self.closed = True


class TestDefinition:
    def test_binds_the_configured_model_and_instructions(self, settings):
        definition = build_definition(settings)

        assert definition.model == "gpt-realtime"
        assert definition.instructions == INSTRUCTIONS

    def test_starts_with_no_tools(self, settings):
        # The OpenAPI tool needs a public HTTPS URL, so it is attached after deploy.
        assert build_definition(settings).tools == []


class TestMetadata:
    def test_session_config_round_trips_out_of_metadata(self, settings):
        metadata = build_metadata(settings)
        restored = json.loads(reassemble_metadata(metadata, SESSION_METADATA_KEY))

        assert restored["turn_detection"]["type"] == "azure_semantic_vad"
        assert restored["input_audio_noise_reduction"]["type"] == "azure_deep_noise_suppression"

    def test_every_value_fits_the_cap(self, settings):
        metadata = build_metadata(settings)

        assert metadata
        assert all(len(value) <= METADATA_VALUE_LIMIT for value in metadata.values())

    def test_carries_no_instructions(self, settings):
        """The agent definition owns them; a client replaying them would be rejected."""
        restored = json.loads(reassemble_metadata(build_metadata(settings), SESSION_METADATA_KEY))
        assert "instructions" not in restored


class TestProvision:
    def test_sends_definition_and_metadata(self, settings):
        client = StubClient()
        provision(settings, client=client)
        call = client.agents.calls[0]

        assert call["agent_name"] == "crm-sales-companion"
        assert call["definition"].model == "gpt-realtime"
        assert f"{SESSION_METADATA_KEY}_0" in call["metadata"]

    def test_does_not_close_a_client_it_was_given(self, settings):
        client = StubClient()
        provision(settings, client=client)

        assert client.closed is False

    def test_missing_foundry_config_fails_before_any_call(self):
        client = StubClient()
        with pytest.raises(ConfigError, match="PROJECT_ENDPOINT"):
            provision(Settings(_env_file=None, project_endpoint=None), client=client)

        assert client.agents.calls == []
