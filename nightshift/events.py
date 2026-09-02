from __future__ import annotations

import asyncio
from collections import deque
from typing import Any


class EventHub:
    """In-process fan-out for browser WebSockets and tests."""

    def __init__(self, history_size: int = 500) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._history: deque[dict[str, Any]] = deque(maxlen=history_size)
        self._lock = asyncio.Lock()

    async def publish(self, event: dict[str, Any]) -> None:
        self._history.append(event)
        async with self._lock:
            subscribers = list(self._subscribers)
        for queue in subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                    queue.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    async def subscribe(self, replay: bool = True) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        if replay:
            for item in self._history:
                try:
                    queue.put_nowait(item)
                except asyncio.QueueFull:
                    break
        async with self._lock:
            self._subscribers.add(queue)
        return queue

    async def unsubscribe(self, queue: asyncio.Queue) -> None:
        async with self._lock:
            self._subscribers.discard(queue)
