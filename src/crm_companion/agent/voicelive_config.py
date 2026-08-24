"""Voice Live session configuration.

Field names and enum values here were read off azure-ai-voicelive 1.3.0 rather
than copied from documentation, because the session shape differs between the
Realtime API family and Voice Live's Azure extensions.

Two settings carry the in-car experience: deep noise suppression plus semantic
VAD, so engine noise and a passenger talking do not end the rep's turn, and
interim responses, so a CRM round-trip does not sound like a dropped call.
"""

from __future__ import annotations

from azure.ai.voicelive.models import (
    AudioEchoCancellation,
    AudioNoiseReduction,
    AzureSemanticVad,
    AzureStandardVoice,
    InputAudioFormat,
    InterimResponseTrigger,
    LlmInterimResponseConfig,
    Modality,
    OutputAudioFormat,
    RequestSession,
)

from crm_companion.config import Settings

__all__ = [
    "METADATA_VALUE_LIMIT",
    "SAMPLE_RATE_HZ",
    "build_session",
    "chunk_metadata",
    "reassemble_metadata",
]

SAMPLE_RATE_HZ = 24_000

# Foundry caps a single agent metadata value; longer config is split across keys.
METADATA_VALUE_LIMIT = 512


def build_session(settings: Settings, *, instructions: str | None = None) -> RequestSession:
    """Omit ``instructions`` in agent mode - Voice Live rejects them as read-only."""
    session = RequestSession(
        modalities=[Modality.TEXT, Modality.AUDIO],
        voice=AzureStandardVoice(name=settings.voice_name),
        input_audio_format=InputAudioFormat.PCM16,
        output_audio_format=OutputAudioFormat.PCM16,
        input_audio_sampling_rate=SAMPLE_RATE_HZ,
        input_audio_noise_reduction=AudioNoiseReduction(type="azure_deep_noise_suppression"),
        input_audio_echo_cancellation=AudioEchoCancellation(),
        turn_detection=AzureSemanticVad(
            threshold=0.5,
            prefix_padding_ms=300,
            # Long enough that a pause mid-sentence is not treated as a finished turn.
            silence_duration_ms=700,
            remove_filler_words=True,
            interrupt_response=True,
            create_response=True,
        ),
        interim_response=LlmInterimResponseConfig(
            triggers=[InterimResponseTrigger.TOOL, InterimResponseTrigger.LATENCY],
            latency_threshold_ms=100,
        ),
    )
    if instructions is not None:
        session.instructions = instructions
    return session


def chunk_metadata(key: str, value: str, *, limit: int = METADATA_VALUE_LIMIT) -> dict[str, str]:
    if limit < 1:
        raise ValueError("limit must be positive")
    parts = [value[index : index + limit] for index in range(0, len(value), limit)] or [""]
    return {f"{key}_{position}": part for position, part in enumerate(parts)}


def reassemble_metadata(metadata: dict[str, str], key: str) -> str | None:
    """Ordered numerically, because ``_10`` sorts before ``_9`` as text."""
    prefix = f"{key}_"
    positions: list[tuple[int, str]] = []
    for name, part in metadata.items():
        if not name.startswith(prefix):
            continue
        suffix = name[len(prefix) :]
        if suffix.isdigit():
            positions.append((int(suffix), part))

    if not positions:
        return None
    return "".join(part for _, part in sorted(positions))
