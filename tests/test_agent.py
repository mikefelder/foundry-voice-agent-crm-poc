import pytest
from azure.ai.voicelive.models import InterimResponseTrigger, Modality

from crm_companion.agent.instructions import INSTRUCTIONS, build_instructions
from crm_companion.agent.voicelive_config import (
    METADATA_VALUE_LIMIT,
    SAMPLE_RATE_HZ,
    build_session,
    chunk_metadata,
    reassemble_metadata,
)
from crm_companion.config import Settings
from crm_companion.tools.registry import write_tools


class TestInstructions:
    def test_names_every_write_tool(self):
        """A write the agent is never told about is a write with no confirmation policy."""
        for tool in write_tools():
            assert tool.name in INSTRUCTIONS

    def test_names_the_preview_and_resolution_tools(self):
        for name in ("preview_opportunity_update", "resolve_stage", "resolve_user"):
            assert name in INSTRUCTIONS

    def test_forbids_counting_and_arithmetic(self):
        assert "get_pipeline_summary" in INSTRUCTIONS
        assert "arithmetic" in INSTRUCTIONS

    def test_forbids_inventing_ids(self):
        assert "invent" in INSTRUCTIONS

    def test_forbids_speaking_ids_or_links(self):
        """It read an 18-character ID out letter by letter when asked for a link."""
        assert "never spell one out" in INSTRUCTIONS
        assert "link" in INSTRUCTIONS

    def test_requires_asking_which_account(self):
        assert "search_accounts returns every match" in INSTRUCTIONS

    def test_requires_verbatim_notes(self):
        assert "word for word" in INSTRUCTIONS
        assert "paraphrase" in INSTRUCTIONS

    def test_forbids_relative_adjustments(self):
        assert "never an adjustment" in INSTRUCTIONS

    def test_avoids_markdown_for_a_listener(self):
        assert "markdown" in INSTRUCTIONS
        assert "#" not in INSTRUCTIONS
        assert "*" not in INSTRUCTIONS

    def test_stays_within_a_sane_prompt_budget(self):
        assert len(INSTRUCTIONS) < 4000

    def test_greeting_is_appended_when_supplied(self):
        assert "Ready when you are." in build_instructions(greeting="Ready when you are.")


class TestSession:
    @pytest.fixture
    def session(self):
        return build_session(Settings(_env_file=None))

    def test_omits_instructions_for_agent_mode(self):
        """Voice Live rejects client-supplied instructions when talking to an agent."""
        assert "instructions" not in build_session(Settings(_env_file=None)).as_dict()

    def test_includes_instructions_when_asked(self):
        session = build_session(Settings(_env_file=None), instructions=INSTRUCTIONS)
        assert session.as_dict()["instructions"] == INSTRUCTIONS

    def test_streams_text_and_audio(self, session):
        assert set(session.modalities) == {Modality.TEXT, Modality.AUDIO}

    def test_uses_deep_noise_suppression_for_a_car(self, session):
        assert session.input_audio_noise_reduction.type == "azure_deep_noise_suppression"

    def test_uses_semantic_vad_that_allows_barge_in(self, session):
        assert session.turn_detection.type == "azure_semantic_vad"
        assert session.turn_detection.interrupt_response is True

    def test_speaks_during_tool_calls(self, session):
        triggers = set(session.interim_response.triggers)
        assert InterimResponseTrigger.TOOL in triggers
        assert InterimResponseTrigger.LATENCY in triggers

    def test_audio_matches_the_client_capture_format(self, session):
        assert session.input_audio_sampling_rate == SAMPLE_RATE_HZ
        assert session.input_audio_format == "pcm16"

    def test_serializes_for_the_wire(self, session):
        assert "turn_detection" in session.as_dict()


class TestMetadataChunking:
    def test_round_trips(self):
        value = "x" * (METADATA_VALUE_LIMIT * 3 + 17)
        assert reassemble_metadata(chunk_metadata("cfg", value), "cfg") == value

    def test_no_chunk_exceeds_the_limit(self):
        chunks = chunk_metadata("cfg", "y" * 2000)
        assert all(len(part) <= METADATA_VALUE_LIMIT for part in chunks.values())

    def test_orders_numerically_not_lexicographically(self):
        # 11 chunks means _10 exists, which sorts before _9 as text.
        value = "".join(str(index % 10) * 5 for index in range(11))
        chunks = chunk_metadata("cfg", value, limit=5)

        assert len(chunks) == 11
        assert reassemble_metadata(chunks, "cfg") == value

    def test_ignores_unrelated_keys(self):
        chunks = chunk_metadata("cfg", "hello", limit=2)
        chunks["other_0"] = "ignored"
        chunks["cfg_notanumber"] = "ignored"

        assert reassemble_metadata(chunks, "cfg") == "hello"

    def test_missing_key_is_none(self):
        assert reassemble_metadata({"other_0": "x"}, "cfg") is None
