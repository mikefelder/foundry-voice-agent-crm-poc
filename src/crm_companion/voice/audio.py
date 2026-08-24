"""Microphone capture and speaker playback.

The interesting part is barge-in. When the rep starts talking the agent must stop
mid-sentence, but audio for the cancelled response is often already queued and
more may still arrive over the socket. Each chunk therefore carries the
generation it was queued under, and anything from an older generation is dropped
rather than played over the rep.

``pyaudio`` is imported lazily so the queue logic can be tested on a machine with
no audio device.
"""

from __future__ import annotations

import asyncio
import queue
import threading
from typing import Any

__all__ = ["CHUNK_MS", "SAMPLE_RATE_HZ", "Microphone", "PlaybackQueue", "Speaker"]

SAMPLE_RATE_HZ = 24_000
CHUNK_MS = 50
SAMPLE_WIDTH_BYTES = 2
CHUNK_FRAMES = SAMPLE_RATE_HZ * CHUNK_MS // 1000

_STOP = object()


class PlaybackQueue:
    """Ordering and barge-in policy, with no audio device attached."""

    def __init__(self) -> None:
        self._items: queue.Queue[Any] = queue.Queue()
        self._generation = 0
        self._lock = threading.Lock()

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def put(self, pcm: bytes) -> None:
        with self._lock:
            generation = self._generation
        self._items.put((generation, pcm))

    def barge_in(self) -> int:
        """Drop what is queued and invalidate anything still in flight."""
        with self._lock:
            self._generation += 1
            current = self._generation
        while True:
            try:
                self._items.get_nowait()
            except queue.Empty:
                break
        return current

    def close(self) -> None:
        self._items.put(_STOP)

    def next_playable(self, timeout: float | None = None) -> bytes | None:
        """Return the next chunk still worth playing, or None when closed."""
        while True:
            try:
                item = self._items.get(timeout=timeout)
            except queue.Empty:
                return b""
            if item is _STOP:
                return None
            generation, pcm = item
            with self._lock:
                current = self._generation
            if generation == current:
                return pcm


class Speaker:
    def __init__(self, *, sample_rate: int = SAMPLE_RATE_HZ) -> None:
        import pyaudio

        self._pyaudio = pyaudio.PyAudio()
        self._stream = self._pyaudio.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=sample_rate,
            output=True,
            frames_per_buffer=CHUNK_FRAMES,
        )
        self._queue = PlaybackQueue()
        self._worker = threading.Thread(target=self._drain, daemon=True)
        self._worker.start()

    def play(self, pcm: bytes) -> None:
        self._queue.put(pcm)

    def barge_in(self) -> None:
        self._queue.barge_in()

    def close(self) -> None:
        self._queue.close()
        self._worker.join(timeout=2)
        self._stream.stop_stream()
        self._stream.close()
        self._pyaudio.terminate()

    def _drain(self) -> None:
        while True:
            pcm = self._queue.next_playable(timeout=0.1)
            if pcm is None:
                return
            if pcm:
                self._stream.write(pcm)


class Microphone:
    """Yields 50 ms PCM16 chunks onto the event loop."""

    def __init__(self, *, sample_rate: int = SAMPLE_RATE_HZ) -> None:
        import pyaudio

        self._loop = asyncio.get_running_loop()
        self._chunks: asyncio.Queue[bytes] = asyncio.Queue()
        self._pyaudio = pyaudio.PyAudio()
        self._stream = self._pyaudio.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=sample_rate,
            input=True,
            frames_per_buffer=CHUNK_FRAMES,
            stream_callback=self._on_chunk,
        )

    def _on_chunk(self, in_data, frame_count, time_info, status):  # noqa: ARG002
        import pyaudio

        self._loop.call_soon_threadsafe(self._chunks.put_nowait, in_data)
        return (None, pyaudio.paContinue)

    async def __aiter__(self):
        while True:
            yield await self._chunks.get()

    def close(self) -> None:
        self._stream.stop_stream()
        self._stream.close()
        self._pyaudio.terminate()
