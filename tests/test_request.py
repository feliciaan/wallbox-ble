"""request(): the response timeout must fire, and cancellation must propagate.

get_parsed_response once caught everything with a bare `except:`, which also
swallows asyncio.CancelledError. asyncio delivers a timeout as a cancellation,
so an unanswered request never timed out -- polling hung for good -- and Home
Assistant could not cancel the refresh task at shutdown ("Task could not be
canceled"). Reproduced on Python 3.11 and 3.13.
"""
import asyncio
import json

import pytest

from _loader import api


class SilentLink:
    """Accepts every write; never answers unless told to."""

    is_connected = True

    def __init__(self):
        self.services = self
        self.writes = []

    def get_service(self, _uuid):
        return self

    def get_characteristic(self, _uuid):
        return "rx"

    async def write_gatt_char(self, _char, payload, _response):
        self.writes.append(bytes(payload))


def make_client(monkeypatch, link):
    monkeypatch.setattr(api, "RESPONSE_TIMEOUT_S", 0.2, raising=False)
    client = api.WallboxBLEApiClient()
    client.client = link
    client.service_uuid = client.rx_uuid = "uuid"
    client.rx_queue = asyncio.Queue()
    client._disconnected_event = asyncio.Event()
    client._pair_lock = asyncio.Lock()
    client._last_write_exc = None
    client.last_error = client.last_response = None
    return client


def test_unanswered_request_times_out(monkeypatch):
    async def scenario():
        client = make_client(monkeypatch, SilentLink())
        return await asyncio.wait_for(client.request("r_sta"), 3)

    # Before the fix this hung until the outer 3 s guard tripped.
    assert asyncio.run(scenario()) == (False, None)


def test_cancellation_propagates(monkeypatch):
    async def scenario():
        client = make_client(monkeypatch, SilentLink())
        task = asyncio.create_task(client.request("r_sta"))
        await asyncio.sleep(0.05)
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=1)
        assert task in done, "request swallowed its cancellation"
        assert task.cancelled()

    asyncio.run(scenario())


def _answer(client, request_id, chunks=1, **body):
    frame = json.dumps({"id": request_id, **body}).encode()
    step = max(1, len(frame) // chunks)
    for i in range(0, len(frame), step):
        client.rx_queue.put_nowait(frame[i:i + step])


def _sent_id(link):
    frame = b"".join(link.writes)
    body = frame[4:-1]  # strip b"EaE" + length byte, and the checksum
    return json.loads(body)["id"]


def test_reply_split_across_notifications(monkeypatch):
    async def scenario():
        link = SilentLink()
        client = make_client(monkeypatch, link)
        task = asyncio.create_task(client.request("r_sta"))
        await asyncio.sleep(0.02)
        _answer(client, _sent_id(link), chunks=4, r={"lock_status": 1})
        return await task

    assert asyncio.run(scenario()) == (True, {"lock_status": 1})


def test_foreign_frame_does_not_poison_the_next(monkeypatch):
    async def scenario():
        link = SilentLink()
        client = make_client(monkeypatch, link)
        task = asyncio.create_task(client.request("r_sta"))
        await asyncio.sleep(0.02)
        client.rx_queue.put_nowait(b'{"no_id": true}')  # complete, but not ours
        _answer(client, _sent_id(link), r=7)
        return await task

    assert asyncio.run(scenario()) == (True, 7)


def test_timeout_is_reported_as_such(monkeypatch, caplog):
    async def scenario():
        client = make_client(monkeypatch, SilentLink())
        await client.request("r_hsh")
        return client.last_failure

    caplog.set_level("DEBUG")
    assert asyncio.run(scenario()) == "timeout"
    assert any("No response to r_hsh" in r.getMessage() for r in caplog.records)


def test_success_clears_the_failure(monkeypatch):
    async def scenario():
        link = SilentLink()
        client = make_client(monkeypatch, link)
        client.last_failure = "timeout"
        task = asyncio.create_task(client.request("r_sta"))
        await asyncio.sleep(0.02)
        _answer(client, _sent_id(link), r=1)
        await task
        return client.last_failure

    assert asyncio.run(scenario()) is None
