"""Shutdown must stop the reconnect loop; warnings are reported once per cause."""
import asyncio

from _loader import api


class FakeLink:
    is_connected = True

    def __init__(self):
        self.disconnects = 0

    async def disconnect(self):
        self.disconnects += 1


def test_shutdown_stops_the_loop_and_leaves_no_orphan():
    async def scenario():
        ticks = {"n": 0}

        async def forever():
            while True:
                ticks["n"] += 1
                await asyncio.sleep(0.01)

        client = api.WallboxBLEApiClient()
        link = FakeLink()
        client.client = link
        client.client_task = asyncio.create_task(forever())
        await asyncio.sleep(0.05)
        assert ticks["n"] > 0

        await client.async_shutdown()
        stopped_at = ticks["n"]
        await asyncio.sleep(0.05)
        assert ticks["n"] == stopped_at
        assert client.client_task is None and client.client is None
        assert link.disconnects == 1
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]

        await client.async_shutdown()  # idempotent

    asyncio.run(scenario())


def test_shutdown_without_a_task_is_safe():
    async def scenario():
        client = api.WallboxBLEApiClient()
        client.client = None
        client.client_task = None
        await client.async_shutdown()

    asyncio.run(scenario())


def test_warn_once_is_keyed_by_cause(monkeypatch):
    seen = []
    monkeypatch.setattr(api.LOGGER, "warning", lambda msg, *a: seen.append(msg % a))
    client = api.WallboxBLEApiClient()
    client._warned = set()
    for _ in range(5):
        client._warn_once("a", "problem %s", "one")
        client._warn_once("b", "problem %s", "two")
    assert seen == ["problem one", "problem two"]
