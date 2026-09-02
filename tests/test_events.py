from __future__ import annotations

import asyncio

import pytest

from nightshift.events import EventHub


@pytest.mark.asyncio
async def test_replay_is_ordered_before_live_delivery() -> None:
    hub = EventHub(history_size=10, queue_size=10)
    await hub.publish({"seq": 1})
    queue = await hub.subscribe(replay=True)
    await hub.publish({"seq": 2})
    try:
        first = await asyncio.wait_for(queue.get(), timeout=1)
        second = await asyncio.wait_for(queue.get(), timeout=1)
        assert [first["seq"], second["seq"]] == [1, 2]
    finally:
        await hub.unsubscribe(queue)


@pytest.mark.asyncio
async def test_slow_subscriber_keeps_newest_event() -> None:
    hub = EventHub(history_size=10, queue_size=2)
    queue = await hub.subscribe(replay=False)
    try:
        await hub.publish({"seq": 1})
        await hub.publish({"seq": 2})
        await hub.publish({"seq": 3})
        assert (await queue.get())["seq"] == 2
        assert (await queue.get())["seq"] == 3
    finally:
        await hub.unsubscribe(queue)
