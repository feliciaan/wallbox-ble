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
    monkeypatch.setattr(api, "RESPONSE_START_TIMEOUT_S", 0.2, raising=False)
    monkeypatch.setattr(api, "RESPONSE_GAP_TIMEOUT_S", 0.1, raising=False)
    monkeypatch.setattr(api, "RESPONSE_TOTAL_TIMEOUT_S", 2, raising=False)
    client = api.WallboxBLEApiClient()
    client.client = link
    client.service_uuid = client.rx_uuid = "uuid"
    client.rx_queue = asyncio.Queue()
    client._disconnected_event = asyncio.Event()
    client._pair_lock = asyncio.Lock()
    client._request_lock = asyncio.Lock()
    client._last_request_id = 0
    client._session_ready = True
    client._last_write_exc = None
    client.last_error = client.last_response = None
    return client


def test_unanswered_request_times_out(monkeypatch):
    async def scenario():
        client = make_client(monkeypatch, SilentLink())
        return await asyncio.wait_for(client.request("r_sta"), 3)

    # Must time out on its own, well inside the outer 3 s guard.
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
    assert any("response to r_hsh" in r.getMessage() for r in caplog.records)


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


# ---------------------------------------------------------------- one at a time

class AnsweringLink(SilentLink):
    """Answers each complete EaE frame; records every byte written, in order."""

    def __init__(self):
        super().__init__()
        self.client = None
        self.stream = bytearray()

    async def write_gatt_char(self, _char, payload, _response):
        self.stream += payload
        self.writes.append(bytes(payload))
        # A whole frame is b"EaE" + length byte + JSON + checksum byte.
        if len(self.stream) >= 4 and self.stream[:3] == b"EaE":
            size = 4 + self.stream[3] + 1
            if len(self.stream) >= size:
                frame, self.stream = bytes(self.stream[:size]), self.stream[size:]
                request_id = json.loads(frame[4:-1])["id"]
                reply = json.dumps({"id": request_id, "r": "ok"}).encode()
                self.client.rx_queue.put_nowait(reply)


def _frames(chunks):
    """Split the written byte stream into EaE frames; fail if they are mixed."""
    stream, frames = b"".join(chunks), []
    while stream:
        assert stream[:3] == b"EaE", f"stream lost frame sync at {stream[:12]!r}"
        size = 4 + stream[3] + 1
        frames.append(json.loads(stream[4:size - 1]))
        stream = stream[size:]
    return frames


def test_concurrent_requests_do_not_interleave(monkeypatch):
    async def scenario():
        link = AnsweringLink()
        client = make_client(monkeypatch, link)
        link.client = client
        # Parameters long enough that every frame takes several 20-byte writes.
        results = await asyncio.gather(*(
            client.request(f"m_{i}", {"padding": "x" * 30}) for i in range(4)
        ))
        return link, results

    link, results = asyncio.run(scenario())
    frames = _frames(link.writes)
    assert [f["met"] for f in frames] == ["m_0", "m_1", "m_2", "m_3"]
    assert results == [(True, "ok")] * 4


def test_not_ready_until_setup_finishes(monkeypatch):
    client = make_client(monkeypatch, SilentLink())
    client._session_ready = False
    assert not client.ready, "polling would race the connect sequence"
    client._session_ready = True
    assert client.ready
    client.client.is_connected = False
    assert not client.ready


# ---------------------------------------------------------------- slow replies

async def _trickle(client, request_id, pieces, delay):
    """Deliver a reply in `pieces` notifications, `delay` seconds apart."""
    frame = json.dumps({"id": request_id, "r": {"st": 1, "pad": "x" * 200}}).encode()
    step = -(-len(frame) // pieces)
    for i in range(0, len(frame), step):
        await asyncio.sleep(delay)
        client.rx_queue.put_nowait(frame[i:i + step])


def test_long_slow_reply_is_not_cut_off(monkeypatch):
    """A status reply trickling in through a proxy, slower overall than the
    first-piece timeout, but never pausing longer than the gap timeout."""
    async def scenario():
        link = SilentLink()
        client = make_client(monkeypatch, link)
        task = asyncio.create_task(client.request("r_dat"))
        await asyncio.sleep(0.01)
        # 12 pieces x 0.05 s = 0.6 s in all: 3x the 0.2 s first-piece timeout.
        await _trickle(client, _sent_id(link), pieces=12, delay=0.05)
        return await task

    ok, result = asyncio.run(scenario())
    assert ok and result["st"] == 1


def test_reply_that_stalls_midway_times_out(monkeypatch):
    async def scenario():
        link = SilentLink()
        client = make_client(monkeypatch, link)
        task = asyncio.create_task(client.request("r_dat"))
        await asyncio.sleep(0.01)
        frame = json.dumps({"id": _sent_id(link), "r": 1}).encode()
        client.rx_queue.put_nowait(frame[:5])  # starts, then nothing more
        return await asyncio.wait_for(task, 3)

    assert asyncio.run(scenario()) == (False, None)


# ---------------------------------------------------------------- session login

def test_login_happens_once_per_connection(monkeypatch):
    async def scenario():
        link = AnsweringLink()
        client = make_client(monkeypatch, link)
        link.client = client
        client.logged_in = False
        await client.async_login({"usid": 42})
        await client.async_login({"usid": 42})  # already logged in: no traffic
        await client.async_login({"st": 0})  # no usid: nothing to do
        return client, link

    client, link = asyncio.run(scenario())
    sent = _frames(link.writes)
    assert [(f["met"], f["par"]) for f in sent] == [("suser", 42)]
    assert client.logged_in


def test_login_retried_after_a_failed_attempt(monkeypatch):
    async def scenario():
        client = make_client(monkeypatch, SilentLink())  # nobody answers
        client.logged_in = False
        first = await client.async_login({"usid": 42})
        return first, client.logged_in

    assert asyncio.run(scenario()) == (False, False)  # next poll will try again
