from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

Event = dict[str, Any]
EventQueue = asyncio.Queue[Event]


class EventHub:
    """In-process fan-out for browser WebSockets and tests.

    History replay and subscriber registration share one lock with publishing,
    so a client cannot miss an event in the gap between those two operations.
    Slow clients retain the newest events; the dashboard also re-fetches the
    authoritative SQLite snapshot after every notification.
    """

    def __init__(self, history_size: int = 500, queue_size: int = 1000) -> None:
        self._subscribers: set[EventQueue] = set()
        self._history: deque[Event] = deque(maxlen=history_size)
        self._queue_size = queue_size
        self._lock = asyncio.Lock()

    async def publish(self, event: Event) -> None:
        async with self._lock:
            self._history.append(event)
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

    async def subscribe(self, replay: bool = True) -> EventQueue:
        queue: EventQueue = asyncio.Queue(maxsize=self._queue_size)
        async with self._lock:
            if replay:
                history = list(self._history)[-self._queue_size:]
                for item in history:
                    queue.put_nowait(item)
            self._subscribers.add(queue)
        return queue

    async def unsubscribe(self, queue: EventQueue) -> None:
        async with self._lock:
            self._subscribers.discard(queue)
