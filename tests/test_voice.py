import pytest

from crm_companion.voice.audio import CHUNK_FRAMES, CHUNK_MS, SAMPLE_RATE_HZ, PlaybackQueue
from crm_companion.voice.session import SessionHandler


class TestPlaybackQueue:
    def test_plays_what_was_queued(self):
        playback = PlaybackQueue()
        playback.put(b"one")
        playback.put(b"two")

        assert playback.next_playable(timeout=0.1) == b"one"
        assert playback.next_playable(timeout=0.1) == b"two"

    def test_barge_in_drops_queued_audio(self):
        playback = PlaybackQueue()
        playback.put(b"mid-sentence")
        playback.barge_in()

        assert playback.next_playable(timeout=0.05) == b""

    def test_late_audio_from_a_cancelled_response_is_discarded(self):
        """Chunks already in flight when the rep interrupts must not be played."""
        playback = PlaybackQueue()
        stale = playback.generation
        playback.barge_in()

        # Arrives after the cancel, tagged with the generation it was created under.
        playback._items.put((stale, b"too late"))
        playback.put(b"fresh")

        assert playback.next_playable(timeout=0.1) == b"fresh"

    def test_close_ends_playback(self):
        playback = PlaybackQueue()
        playback.close()

        assert playback.next_playable(timeout=0.1) is None

    def test_each_barge_in_advances_the_generation(self):
        playback = PlaybackQueue()
        first = playback.barge_in()

        assert playback.barge_in() == first + 1


class FakeSpeaker:
    def __init__(self):
        self.played = []
        self.barge_ins = 0

    def play(self, pcm):
        self.played.append(pcm)

    def barge_in(self):
        self.barge_ins += 1


class FakeConnection:
    def __init__(self):
        self.sent = []

    async def send(self, event):
        self.sent.append(str(getattr(event, "type", "")))


class Event:
    def __init__(self, type_, **kwargs):
        self.type = type_
        for key, value in kwargs.items():
            setattr(self, key, value)


class TestSessionHandling:
    async def test_speech_cancels_playback_and_the_response(self):
        connection, speaker = FakeConnection(), FakeSpeaker()
        handler = SessionHandler(speaker, report=lambda role, text: None)

        await handler.handle(connection, Event("response.created"))
        await handler.handle(connection, Event("input_audio_buffer.speech_started"))

        assert speaker.barge_ins == 1
        assert any("response.cancel" in sent for sent in connection.sent)

    async def test_does_not_cancel_when_the_agent_is_not_speaking(self):
        """Road noise trips speech detection constantly; the service errors on a stray cancel."""
        connection, speaker = FakeConnection(), FakeSpeaker()
        handler = SessionHandler(speaker, report=lambda role, text: None)

        await handler.handle(connection, Event("input_audio_buffer.speech_started"))

        assert speaker.barge_ins == 1
        assert connection.sent == []

    async def test_a_finished_response_is_no_longer_cancellable(self):
        connection, speaker = FakeConnection(), FakeSpeaker()
        handler = SessionHandler(speaker, report=lambda role, text: None)

        await handler.handle(connection, Event("response.created"))
        await handler.handle(connection, Event("response.done"))
        await handler.handle(connection, Event("input_audio_buffer.speech_started"))

        assert connection.sent == []

    async def test_audio_deltas_reach_the_speaker(self):
        connection, speaker = FakeConnection(), FakeSpeaker()
        handler = SessionHandler(speaker, report=lambda role, text: None)

        await handler.handle(connection, Event("response.audio.delta", delta=b"pcm"))

        assert speaker.played == [b"pcm"]

    @pytest.mark.parametrize(
        ("kind", "role"),
        [
            ("conversation.item.input_audio_transcription.completed", "you"),
            ("response.audio_transcript.done", "agent"),
        ],
    )
    async def test_transcripts_are_reported(self, kind, role):
        reported = []
        handler = SessionHandler(FakeSpeaker(), report=lambda r, text: reported.append((r, text)))

        await handler.handle(FakeConnection(), Event(kind, transcript="hello"))

        assert reported == [(role, "hello")]


def test_chunk_size_matches_the_documented_format():
    assert SAMPLE_RATE_HZ == 24_000
    assert CHUNK_FRAMES == SAMPLE_RATE_HZ * CHUNK_MS // 1000
