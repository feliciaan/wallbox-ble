"""Pairing through a Bluetooth proxy: one request per link, no self-inflicted drops.

Replays a field log where the poll's write-retry overlapped the connect path's
pairing request: the proxy answers once per AUTH_CMPL, so the second request
hung for 30 s, and the failed write then forced a reconnect that tore the link
down under it.
"""
import asyncio

from _loader import api

AUTH_ERROR = Exception(
    "Bluetooth GATT Error address=54:64:DE:92:BC:7C handle=23 error=5 "
    "description=Insufficient authentication"
)


class FakeLink:
    """Stands in for the bleak client: pair() blocks until released."""

    is_connected = True

    def __init__(self, pair_error=None):
        self.pair_calls = 0
        self.release = asyncio.Event()
        self.pair_error = pair_error
        self.services = self

    async def pair(self):
        self.pair_calls += 1
        await self.release.wait()
        if self.pair_error:
            raise self.pair_error

    # request() looks the command characteristic up on the service.
    def get_service(self, _uuid):
        return self

    def get_characteristic(self, _uuid):
        return "rx"

    async def write_gatt_char(self, *_args):
        raise AUTH_ERROR


def make_client(link):
    client = api.WallboxBLEApiClient()
    client.client = link
    client.pin = 123456
    client.hass = None
    client.address = "54:64:DE:92:BC:7C"
    client.service_uuid = client.rx_uuid = "uuid"
    client.rx_queue = asyncio.Queue()
    client._disconnected_event = asyncio.Event()
    client._warned = set()
    client._auth_help_logged = False
    client._pairing_unsupported_logged = True  # proxy-served: skip the D-Bus lookup
    client.pairing_auth_failed = False
    client._last_write_exc = None
    client._pair_lock = asyncio.Lock()
    client._request_lock = asyncio.Lock()
    client._session_ready = True
    client._link_pair_result = None
    return client


def test_poll_during_setup_pairing_neither_repairs_nor_reconnects():
    async def scenario():
        link = FakeLink()
        client = make_client(link)

        # The connect path starts pairing and is waiting on the proxy...
        setup = asyncio.create_task(client._async_pair_over_link())
        await asyncio.sleep(0)
        assert link.pair_calls == 1

        # ...when the coordinator's poll writes and is rejected.
        ok, _ = await client.request("r_dat")
        assert ok is False
        assert link.pair_calls == 1, "poll issued a second, overlapping pair request"
        assert not client._disconnected_event.is_set(), "poll tore the link down mid-pairing"

        link.release.set()
        assert await setup is True

    asyncio.run(scenario())


def test_failed_pairing_is_not_repeated_on_the_same_link():
    async def scenario():
        link = FakeLink(pair_error=Exception("Pairing failed due to error: 80"))
        link.release.set()
        client = make_client(link)

        assert await client._async_pair_over_link() is False
        for _ in range(3):
            ok, _ = await client.request("r_dat")
            assert ok is False
        assert link.pair_calls == 1, "a failed pairing was retried on the same link"
        assert not client._disconnected_event.is_set(), "auth rejection forced a reconnect"

    asyncio.run(scenario())


def test_new_link_gets_a_fresh_attempt():
    async def scenario():
        link = FakeLink(pair_error=Exception("Pairing failed due to error: 80"))
        link.release.set()
        client = make_client(link)
        await client._async_pair_over_link()

        # run_ble_client resets this on every (re)connect.
        client._link_pair_result = None
        link.pair_error = None
        assert await client._async_pair_over_link() is True
        assert link.pair_calls == 2

    asyncio.run(scenario())


def test_concurrent_callers_share_one_request():
    async def scenario():
        link = FakeLink()
        client = make_client(link)
        calls = [asyncio.create_task(client._async_pair_over_link()) for _ in range(4)]
        await asyncio.sleep(0)
        link.release.set()
        assert await asyncio.gather(*calls) == [True] * 4
        assert link.pair_calls == 1

    asyncio.run(scenario())


def test_non_auth_write_failure_still_reconnects():
    async def scenario():
        link = FakeLink()
        client = make_client(link)

        async def dead_link(*_args):
            raise Exception("Bluetooth GATT Error address=X handle=23 error=-1 description=Disconnected")

        link.write_gatt_char = dead_link
        ok, _ = await client.request("r_dat")
        assert ok is False
        assert client._disconnected_event.is_set(), "a dead link must still trigger a reconnect"
        assert link.pair_calls == 0

    asyncio.run(scenario())
