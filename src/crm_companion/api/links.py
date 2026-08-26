"""Record links pushed to connected browsers.

Voice Live tells the client nothing about tool calls in agent mode, so a link
cannot come back through the conversation. It does not need to: the tool API and
the browser relay are the same process, so a successful write can put a link on
screen directly while the agent only says it has done so.

Fan-out is to every open session. That is correct for one rep with one tab and
wrong for concurrent users - correlating a tool call to a session needs an
identifier Foundry does not currently pass through.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator

__all__ = ["publish", "subscribe"]

_subscribers: set[asyncio.Queue[dict[str, str]]] = set()


@contextlib.contextmanager
def subscribe() -> Iterator[asyncio.Queue[dict[str, str]]]:
    queue: asyncio.Queue[dict[str, str]] = asyncio.Queue()
    _subscribers.add(queue)
    try:
        yield queue
    finally:
        _subscribers.discard(queue)


def publish(label: str, url: str) -> None:
    for queue in _subscribers:
        queue.put_nowait({"label": label, "url": url})
