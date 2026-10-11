"""End-to-end runs against a simulated charger.

The unit tests check pieces in isolation; the bugs found in the field lived
between them (the connect sequence racing polling, a fixed reply timeout
against a slow proxy, a login skipped after one slow read). These tests drive
the real connection loop (run_ble_client), the real request path and the real
coordinator against a simulated Wallbox, across every combination of:

    Bluetooth path   local BlueZ adapter / ESPHome proxy
    radio profile    pulsar_max (one characteristic) / bgexpress (two) /
                     zentri (stream mode)
    passcode         none / required
    meter            none / current only / wired with a voltage reference
    link speed       fast / slow (a proxy relaying each 20-byte piece)

plus failure scenarios. The simulated charger checks every frame byte for
byte -- framing, length, checksum -- and, like the real one, stops answering
once it receives a corrupt frame, until the link is rebuilt.

Timers are scaled down so a scenario takes about a second.
"""
import asyncio
import json
import types

import pytest

from _loader import api, load, pairing

coordinator_mod = load("coordinator")

ADDRESS = "54:64:DE:92:BC:7C"
PIN = 675250
PROFILES = {p["name"]: p for p in api.WallboxBLEApiConst.BLE_PROFILES}

AUTH_ERROR = ("Bluetooth GATT Error address=54:64:DE:92:BC:7C handle=23 error=5 "
              "description=Insufficient authentication")
NOTIFY_AUTHZ_ERROR = ("Peripheral 54:64:DE:92:BC:7C changed connection status while "
                      "waiting for BluetoothGATTNotifyResponse, BluetoothGATTErrorResponse: "
                      "Insufficient authorization (8)")

METERS = {
    "none": None,
    "current_only": {"v1": 0, "p1": 0, "c1": 220, "c2": 0, "c3": 0, "e": 0},
    "voltage": {"v1": 238, "p1": 4975, "c1": 220, "c2": 0, "c3": 0, "e": 8123456},
}


class BleakError(Exception):
    pass


# Link timings through an ESPHome proxy, in real seconds, from a field log.
WRITE_S = 0.4  # each 20-byte write until its ATT response is relayed back
SLOW_WRITE_S = 4.0  # an occasional slow one: over the old 2 s write timeout
REPLY_START_S = 2.0  # until the first piece of a reply
REPLY_GAP_S = 0.15  # between pieces
LATE_START_S = 10.0  # a reply that starts after the first-piece timeout
STALL_S = 4.5  # a mid-reply pause longer than the between-pieces timeout

FIELD_ANSWERS = {
    "r_dat": {"L1": 0, "L2": 0, "L3": 0, "cp": 0.0, "cur": 32, "den": 0, "en": 0,
              "gen": 0, "grid": 0, "mid": 1, "ocpp": 1, "ps": 0, "s": 0, "st": 6,
              "usid": 1},
    "r_sta": {"charger_status": 6, "error_code": 0, "external_meter_status": 2,
              "lock_status": 1, "max_available_current": 32, "max_charging_current": 32,
              "mid_status": 1, "ocpp_status": 1, "phases_connection": 0,
              "power_sharing_status": 0},  # no "grid" on this firmware
    "r_dca": {"c1": 18, "c2": 0, "c3": 0, "e": 800, "p1": 351, "p2": 0, "p3": 0,
              "v1": 230, "v2": 0, "v3": 0},
    "fw_v_": {"c": "ATLAS", "db": 125, "fw": 20202, "p": "prj20-pulsar-max-pro",
              "r": 20202, "s": "6.13.6"},
    "r_sn_": "01414644", "r_wel": 0, "g_alo": 300, "r_not": [],
    "r_ses": {"last": 24, "size": 99999}, "r_hsh": 40, "suser": 0, "r_fsI": 32,
}


# ---------------------------------------------------------------- the charger

class Charger:
    """State that outlives a single BLE connection."""

    def __init__(self, profile="pulsar_max", pin=None, meter="voltage",
                 first_delay=0.0, chunk_delay=0.0, silent=(), notify_failures=0,
                 proxy_has_passkey=True, pairing_wrong_pin=False,
                 write_delay=0.0, faults=None, slow_writes=(), reappear_delay=0.0):
        self.profile = PROFILES[profile]
        self.pin = pin
        self.meter = METERS[meter]
        self.first_delay = first_delay
        self.chunk_delay = chunk_delay
        self.silent = set(silent)
        self.notify_failures = notify_failures
        self.proxy_has_passkey = proxy_has_passkey
        self.pairing_wrong_pin = pairing_wrong_pin
        self.write_delay = write_delay
        # Request ordinal (1-based, across links) -> "late" | "stall" | "drop".
        self.faults = dict(faults or {})
        self.faulted_ids = {}  # request id -> fault, as the client numbered it
        # Write-call ordinals (1-based) that take SLOW_WRITE_S to confirm.
        self.slow_writes = set(slow_writes)
        self.writes = 0
        # After the client drops the link, how long until it advertises again.
        self.reappear_delay = reappear_delay
        self.hidden_until = 0.0
        self.bonded = False
        self.visible = True
        self.links = []
        self.requests = []  # (met, par) in arrival order, across links
        self.request_ids = []  # the matching request ids
        self.garbled = 0
        # Charger state the commands act on.
        self.status = {"st": 1, "cur": 16, "L1": 160, "L2": 0, "L3": 0,
                       "cp": 3.76, "en": 512, "usid": 4242}
        self.locked = 0
        self.autolock = 120
        self.halo = {"bright": 80, "mode": 0, "time_s": 30}

    @property
    def link(self):
        return self.links[-1] if self.links else None

    def answer(self, met, par):
        """BAPI: (kind, value). kind is "r", "error", "top" or None (silent)."""
        if met in self.silent:
            return None, None
        if getattr(self, "field", False) and met in FIELD_ANSWERS:
            value = FIELD_ANSWERS[met]
            return ("top", value) if met == "fw_v_" else ("r", value)
        table = {
            "r_dat": lambda: ("r", dict(self.status)),
            "r_fsI": lambda: ("r", 32),
            "suser": lambda: ("r", 1),
            "r_sta": lambda: ("r", {"lock_status": self.locked, "max_available_current": 25,
                                    "grid": 1234567, "charger_status": 1, "ocpp_status": 1}),
            "r_dca": lambda: ("r", dict(self.meter)) if self.meter else ("error", {"code": 4}),
            "g_alo": lambda: ("r", self.autolock),
            "s_alo": lambda: self._set("autolock", par),
            "g_halocfg": lambda: ("r", dict(self.halo)),
            "s_halocfg": lambda: self._set("halo", par),
            "w_lck": lambda: self._set("locked", par),
            "w_mxI": lambda: self._set_current(par),
            "r_not": lambda: ("r", [{"message": "Grounding fault"}]),
            "r_ses": lambda: ("r", {"last": 412}),
            "r_hsh": lambda: ("r", 25),
            "fw_v_": lambda: ("top", {"s": "6.11.26", "p": "MAX"}),
            "r_sn_": lambda: ("r", "1414644"),
            "r_wel": lambda: ("r", 0),
        }
        return table[met]() if met in table else ("error", {"code": 4})

    def _set(self, attr, value):
        setattr(self, attr, value)
        return "r", 1

    def _set_current(self, value):
        self.status["cur"] = value
        return "r", 1


class Service:
    def __init__(self, uuids):
        self.uuids = uuids

    def get_characteristic(self, uuid):
        return types.SimpleNamespace(uuid=uuid) if uuid in self.uuids else None


class Services:
    def __init__(self, profile):
        self.profile = profile

    def get_service(self, uuid):
        if uuid != self.profile["service"]:
            return None
        return Service({self.profile["rx"], self.profile["tx"], self.profile["mode"]} - {None})


class Link:
    """One BLE connection to the charger, as bleak / bleak-esphome present it."""

    def __init__(self, charger, disconnected_callback):
        self.charger = charger
        self.disconnected_callback = disconnected_callback
        self.services = Services(charger.profile)
        self.is_connected = True
        # A stored bond encrypts the link on connect.
        self.encrypted = charger.bonded
        self.notify = None
        self.stream_mode = charger.profile["stream_mode"] is None
        self.buffer = bytearray()
        self.desynced = False
        self.logins = 0
        self.pending = set()

    # -- bleak API ---------------------------------------------------------
    async def start_notify(self, uuid, callback):
        assert uuid == self.charger.profile["tx"]
        if self.charger.notify_failures > 0:
            self.charger.notify_failures -= 1
            await self._drop()
            raise BleakError(NOTIFY_AUTHZ_ERROR)
        self.notify = callback

    async def write_gatt_char(self, char, payload, response=True):
        if not self.is_connected:
            raise BleakError("Not connected")
        uuid = char if isinstance(char, str) else char.uuid
        self.charger.writes += 1
        delay = self.charger.write_delay
        if self.charger.writes in self.charger.slow_writes:
            delay = scaled(SLOW_WRITE_S)
        if delay:
            await asyncio.sleep(delay)
            if not self.is_connected:
                raise BleakError("Not connected")
        if uuid == self.charger.profile["mode"]:
            self.stream_mode = bytes(payload) == bytes([self.charger.profile["stream_mode"]])
            return
        assert uuid == self.charger.profile["rx"], uuid
        if self.charger.pin and not self.encrypted:
            raise BleakError(AUTH_ERROR)
        if not self.stream_mode:
            return  # module in command mode: swallowed
        self.buffer += payload
        self._parse()

    async def pair(self, *args, **kwargs):
        if self.charger.pin and not self.charger.proxy_has_passkey:
            raise BleakError("Pairing failed due to error: 80")
        self.encrypted = True
        self.charger.bonded = True

    async def disconnect(self):
        if self.is_connected and self.charger.reappear_delay:
            # Field: after the client drops the link, the charger stopped
            # advertising for minutes ("last advertisement 266s ago").
            self.charger.hidden_until = (
                asyncio.get_running_loop().time() + self.charger.reappear_delay)
        await self._drop(notify=False)

    # -- charger side ------------------------------------------------------
    def _parse(self):
        while self.buffer:
            if self.buffer[:3] != b"EaE"[: len(self.buffer[:3])]:
                return self._garbled()
            if len(self.buffer) < 4:
                return
            size = 4 + self.buffer[3] + 1
            if len(self.buffer) < size:
                return
            frame, self.buffer = bytes(self.buffer[:size]), self.buffer[size:]
            if sum(frame[:-1]) % 256 != frame[-1]:
                return self._garbled()
            try:
                body = json.loads(frame[4:-1])
            except ValueError:
                return self._garbled()
            if self.desynced:
                continue  # out of step until the link is rebuilt
            self.charger.requests.append((body["met"], body["par"]))
            self.charger.request_ids.append(body["id"])
            fault = self.charger.faults.get(len(self.charger.requests))
            if fault:
                self.charger.faulted_ids[body["id"]] = fault
            if body["met"] == "suser":
                self.logins += 1
            kind, value = self.charger.answer(body["met"], body["par"])
            if kind is not None and fault != "drop":
                task = asyncio.ensure_future(self._reply(body["id"], kind, value, fault))
                self.pending.add(task)
                task.add_done_callback(self.pending.discard)

    def _garbled(self):
        self.charger.garbled += 1
        self.desynced = True
        self.buffer.clear()

    async def _reply(self, request_id, kind, value, fault=None):
        if kind == "r":
            message = {"id": request_id, "r": value}
        elif kind == "error":
            message = {"id": request_id, "error": value}
        else:
            message = {"id": request_id, **value}
        data = json.dumps(message, separators=(",", ":"), sort_keys=True).encode()
        if fault == "stall" and len(data) <= 40:
            # Fewer than three pieces: there is no "halfway" to stall at, so
            # this reply arrives normally and must not count as faulted.
            self.charger.faulted_ids.pop(request_id, None)
            fault = None
        first = scaled(LATE_START_S) if fault == "late" else self.charger.first_delay
        await asyncio.sleep(first)
        for n, i in enumerate(range(0, len(data), 20)):  # 23-byte ATT MTU
            if not self.is_connected or self.notify is None:
                return
            if fault == "stall" and n == 2:
                # Part of the reply is out, then a pause longer than the
                # client's between-pieces timeout: the rest arrives while the
                # client is already waiting for its next request's reply.
                await asyncio.sleep(scaled(STALL_S))
            await self.notify(None, bytearray(data[i:i + 20]))
            await asyncio.sleep(self.charger.chunk_delay)

    async def _drop(self, notify=True):
        if not self.is_connected:
            return
        self.is_connected = False
        for task in list(self.pending):
            task.cancel()
        if notify and self.disconnected_callback:
            self.disconnected_callback(self)


# ---------------------------------------------------------------- harness

# Every timer is the integration's own constant times SCALE, so the ratios
# between them -- and against the link timings below, given in real seconds
# and scaled the same way -- are the real ones.
SCALE = 1 / 25
API_TIMERS = ("RESPONSE_START_TIMEOUT_S", "RESPONSE_GAP_TIMEOUT_S",
              "RESPONSE_TOTAL_TIMEOUT_S", "WRITE_TIMEOUT_S", "HEALTH_CHECK_INTERVAL",
              "STALE_RECONNECT_S", "RECONNECT_DELAY_S", "RESPONSE_TIMEOUT_S")
SLOW = dict(first_delay=0.2, chunk_delay=0.04)


def scaled(seconds):
    return seconds * SCALE


def scale_timers(monkeypatch, slow_poll=None):
    for name in API_TIMERS:
        if hasattr(api, name):
            monkeypatch.setattr(api, name, getattr(api, name) * SCALE)
    monkeypatch.setattr(coordinator_mod, "SLOW_POLL_S",
                        slow_poll if slow_poll is not None else coordinator_mod.SLOW_POLL_S * SCALE)
    monkeypatch.setattr(coordinator_mod, "SELF_RELOAD_STALE_S",
                        coordinator_mod.SELF_RELOAD_STALE_S * SCALE)
    monkeypatch.setattr(pairing, "PAIR_TIMEOUT_S", pairing.PAIR_TIMEOUT_S * SCALE)


def wire(monkeypatch, charger, transport):
    """Point the integration's Bluetooth seams at the simulated charger."""

    def device_from_address(hass, address, connectable=True):
        hidden = asyncio.get_running_loop().time() < charger.hidden_until
        return object() if charger.visible and not hidden else None

    async def establish(_cls, _device, _address, disconnected_callback=None, **_):
        link = Link(charger, disconnected_callback)
        charger.links.append(link)
        return link

    async def bluez_pair(address, passkey, adapter):
        if transport == "proxy":
            raise pairing.PairingUnsupported("Cannot reach the system D-Bus")
        if charger.pairing_wrong_pin:
            raise pairing.PairingAuthError("Charger rejected the Bluetooth Passcode")
        charger.bonded = True
        return True

    monkeypatch.setattr(api, "async_ble_device_from_address", device_from_address)
    monkeypatch.setattr(api, "establish_connection", establish)
    monkeypatch.setattr(api, "async_bluez_ensure_paired", bluez_pair)
    monkeypatch.setattr(api, "async_adapter_for_address",
                        lambda hass, address: "hci0" if transport == "local" else None)


class Run:
    """A coordinator + client pair, polled like Home Assistant would."""

    def __init__(self, charger, pin):
        self.charger = charger
        self.pin = pin
        self.reloads = 0

    async def start(self):
        async def reload(_entry_id):
            self.reloads += 1

        hass = types.SimpleNamespace(
            async_create_task=asyncio.ensure_future,
            config_entries=types.SimpleNamespace(async_reload=reload),
        )
        self.c = coordinator_mod.WallboxBLEDataUpdateCoordinator(hass)
        self.c.config_entry = types.SimpleNamespace(entry_id="E")
        self.c.wb = await api.WallboxBLEApiClient.create(hass, ADDRESS, self.pin)

    async def poll(self):
        data = await self.c._async_update_data()
        self.c.data = data
        return data

    async def poll_until(self, condition, timeout=8.0, interval=0.1):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            await self.poll()
            if condition():
                return True
            await asyncio.sleep(interval)
        return False

    async def stop(self):
        await self.c.async_shutdown()
        for link in self.charger.links:
            for task in list(link.pending):
                task.cancel()
        await asyncio.sleep(0)


def run_scenario(monkeypatch, charger, transport="proxy", pin=None, body=None,
                 slow_poll=None):
    scale_timers(monkeypatch, slow_poll)
    wire(monkeypatch, charger, transport)
    run = Run(charger, pin)

    async def main():
        await run.start()
        try:
            return await body(run)
        finally:
            await run.stop()
            leftover = [t for t in asyncio.all_tasks()
                        if t is not asyncio.current_task() and not t.done()]
            await asyncio.sleep(0.05)
            leftover = [t for t in leftover if not t.done()]
            assert not leftover, f"tasks still running after shutdown: {leftover}"

    return asyncio.run(main())


def healthy(run):
    return run.c.available and run.c.status == "CHARGING"


# ---------------------------------------------------------------- the matrix

@pytest.mark.parametrize("speed", ["fast", "slow"])
@pytest.mark.parametrize("meter", ["none", "current_only", "voltage"])
@pytest.mark.parametrize("with_pin", [False, True])
@pytest.mark.parametrize("profile", ["pulsar_max", "bgexpress", "zentri"])
@pytest.mark.parametrize("transport", ["proxy", "local"])
def test_matrix(monkeypatch, transport, profile, with_pin, meter, speed):
    # "slow": each 20-byte piece relayed with a delay, the first one late --
    # the shape measured through an ESPHome proxy. The whole status reply
    # then takes longer than the first-piece timeout, which a fixed reply
    # budget would cut off.
    delays = SLOW if speed == "slow" else {}
    pin = PIN if with_pin else None
    charger = Charger(profile=profile, pin=pin, meter=meter, **delays)

    async def body(run):
        assert await run.poll_until(lambda: healthy(run) and run.c.firmware), (
            f"never became healthy: available={run.c.available} status={run.c.status} "
            f"links={len(charger.links)} garbled={charger.garbled}"
        )
        # A few more polls in steady state.
        for _ in range(3):
            await run.poll()
            await asyncio.sleep(0.1)
        return run

    run = run_scenario(monkeypatch, charger, transport, pin, body)
    c = run.c

    # Every frame reached the charger intact.
    assert charger.garbled == 0
    # One connection; logged in exactly once on it.
    assert len(charger.links) == 1, f"reconnected {len(charger.links) - 1}x"
    assert charger.link.logins == 1
    assert run.reloads == 0

    # Charger readings.
    assert c.charge_current == 16 and c.locked is False
    assert c.car_connected is True
    assert c.autolock_seconds == 120 and c.notification_count == 1
    assert (c.firmware, c.serial_number, c.session_count) == ("6.11.26", "1414644", 412)

    # Meter.
    if meter == "none":
        assert c.meter is None and c.meter_supported is False
        assert c.grid_power is None and c.house_power is None
        assert "r_dca" not in [m for m, _ in charger.requests[-3:]], "kept asking for a missing meter"
    else:
        assert c.meter is not None
        assert c.grid_power == pytest.approx(c.house_power + c.charger_power)
        assert c.charger_power == pytest.approx(3760)  # the charger's own cp
        if meter == "voltage":
            assert c.voltage == 238 and c.voltage_source == "meter"
            assert c.grid_power == 4975
        else:
            assert c.voltage_source == "charger"  # cp / I while charging
            assert c.grid_power == pytest.approx(22 * c.voltage)

    # Nothing given up on: every read was answered.
    assert not c._skipped


# ---------------------------------------------------------------- failure scenarios

def test_writes_during_polling(monkeypatch):
    """Entity writes land while polls run; no frame is corrupted."""
    charger = Charger(chunk_delay=0.02)

    async def body(run):
        assert await run.poll_until(lambda: healthy(run))
        const = api.WallboxBLEApiConst
        results = await asyncio.gather(
            run.poll(),
            run.c.async_set_parameter(const.LOCK, 1),
            run.c.async_set_parameter(const.SET_MAX_CHARGING_CURRENT, 10),
            run.c.async_set_autolock(300),
            run.c.async_set_halo("Low"),
            run.poll(),
        )
        assert all(r is not False for r in results[1:5])
        await run.poll()
        return run

    run = run_scenario(monkeypatch, charger, body=body)
    assert charger.garbled == 0
    assert charger.locked == 1 and charger.status["cur"] == 10
    assert charger.autolock == 300
    assert charger.halo == {"bright": 20, "mode": 0, "time_s": 30}
    assert run.c.locked is True


def test_link_drop_recovers_and_logs_in_again(monkeypatch):
    charger = Charger()

    async def body(run):
        assert await run.poll_until(lambda: healthy(run))
        await charger.link._drop()
        assert await run.poll_until(lambda: len(charger.links) == 2 and healthy(run))
        return run

    run_scenario(monkeypatch, charger, body=body)
    assert [link.logins for link in charger.links] == [1, 1]
    assert charger.garbled == 0


def test_silent_reads_are_dropped_without_affecting_the_rest(monkeypatch):
    """r_not (re-read every slow poll) is dropped after three misses; fw_v_
    (identity, read once per connection) costs a single timeout and is not
    asked again because its siblings answered."""
    charger = Charger(silent={"r_not", "fw_v_"})

    async def body(run):
        assert await run.poll_until(lambda: "r_not" in run.c._skipped, timeout=8)
        before = len(charger.requests)
        await asyncio.sleep(0.4)  # past another slow poll
        await run.poll()
        later = [m for m, _ in charger.requests[before:]]
        assert "r_not" not in later and "fw_v_" not in later
        return run

    run = run_scenario(monkeypatch, charger, body=body, slow_poll=0.3)
    asked = [m for m, _ in charger.requests]
    assert asked.count("fw_v_") == 1
    assert asked.count("r_not") == 3
    assert run.c.available and run.c.serial_number == "1414644" and run.c.firmware is None
    assert "r_dat" not in run.c._skipped


def test_status_read_silent_heals_by_reload_not_by_hanging(monkeypatch):
    """A charger that stops answering: polling must keep running, the link is
    rebuilt, and the self-heal reload fires -- nothing blocks forever."""
    charger = Charger()

    async def body(run):
        assert await run.poll_until(lambda: healthy(run))
        charger.silent = {"r_dat"}
        assert await run.poll_until(lambda: run.reloads >= 1, timeout=12), "self-heal never fired"
        assert len(charger.links) >= 2, "link never rebuilt"
        return run

    run = run_scenario(monkeypatch, charger, body=body)
    assert not run.c.available


def test_startup_notify_failures_then_recovers(monkeypatch):
    """Field log: three connects failed with Insufficient authorization (8)
    while subscribing, the fourth worked."""
    charger = Charger(pin=PIN, notify_failures=3)

    async def body(run):
        assert await run.poll_until(lambda: healthy(run), timeout=10)
        return run

    run_scenario(monkeypatch, charger, pin=PIN, body=body)
    assert len(charger.links) == 4 and charger.link.logins == 1
    assert charger.garbled == 0


def test_device_not_visible_at_first(monkeypatch):
    """Field log: "No device found" for ~30 s after a restart."""
    charger = Charger()
    charger.visible = False

    async def body(run):
        await run.poll()
        await asyncio.sleep(0.5)
        assert not run.c.available and not charger.links
        charger.visible = True
        assert await run.poll_until(lambda: healthy(run))
        return run

    run_scenario(monkeypatch, charger, body=body)


def test_wrong_pin_on_local_adapter_asks_for_reauth(monkeypatch):
    charger = Charger(pin=PIN, pairing_wrong_pin=True)

    async def body(run):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5
        while loop.time() < deadline:
            try:
                await run.poll()
            except Exception as err:  # ConfigEntryAuthFailed
                return type(err).__name__
            await asyncio.sleep(0.1)
        return None

    assert run_scenario(monkeypatch, charger, "local", PIN, body) == "ConfigEntryAuthFailed"


def test_proxy_without_passkey_config_fails_cleanly(monkeypatch, caplog):
    """No passkey on the proxy: the link stays unauthenticated. The integration
    must say why, not loop reconnecting every poll, and not corrupt anything."""
    charger = Charger(pin=PIN, proxy_has_passkey=False)

    async def body(run):
        await run.poll_until(lambda: False, timeout=2.5)
        return run

    run = run_scenario(monkeypatch, charger, pin=PIN, body=body)
    assert not run.c.available
    assert charger.garbled == 0
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "authentication requirements (0x03)" in messages
    assert "rejected the command even though a Bluetooth Passcode is configured" in messages
    # Rebuilt only by the health check, not on every rejected write.
    assert len(charger.links) <= 3


def test_shutdown_mid_request_is_clean(monkeypatch):
    """Field warning: "Task could not be canceled and was still running after
    shutdown". Cancel a poll mid-reply, then shut down."""
    charger = Charger(first_delay=0.2, chunk_delay=0.05)

    async def body(run):
        assert await run.poll_until(lambda: healthy(run))
        poll = asyncio.ensure_future(run.poll())
        await asyncio.sleep(0.25)  # in the middle of a reply
        poll.cancel()
        done, _ = await asyncio.wait({poll}, timeout=1)
        assert poll in done, "poll swallowed its cancellation"
        return run

    run_scenario(monkeypatch, charger, body=body)


# ---------------------------------------------------------------- the field link

def test_field_link(monkeypatch):
    """Replays the link in a field log: a Pulsar Max (fw 6.13.6) behind an
    ESPHome proxy, its real replies, the measured latency, and the faults
    that log showed -- a write slower than the old 2 s write timeout, a reply
    that starts after the first-piece timeout, one that stalls halfway, one
    that never comes, and g_halocfg never answered.

    Before the fix each slow write forced a reconnect (after which the
    charger disappeared for minutes), and the stalled reply's tail poisoned
    the next request too. Now exactly the faulted requests fail, nothing
    else, on a single connection.
    """
    charger = Charger(
        first_delay=scaled(REPLY_START_S), chunk_delay=scaled(REPLY_GAP_S),
        write_delay=scaled(WRITE_S), silent={"g_halocfg"},
        # Ordinals of requests as the charger receives them (setup sends
        # r_dat + suser first, then each poll r_dat, r_sta, r_dca, ...).
        faults={9: "stall", 15: "late", 21: "stall", 26: "drop", 31: "stall"},
        slow_writes={40, 70},
        reappear_delay=scaled(240),
    )
    charger.field = True
    outcomes = []
    availability = []

    async def body(run):
        original = run.c.wb.request

        async def recording(method, parameter=None):
            # Identify the request by the frame the charger received for it
            # (requests are serialised, so it is the next one to arrive).
            before = len(charger.request_ids)
            ok, result = await original(method, parameter)
            rid = charger.request_ids[before] if len(charger.request_ids) > before else None
            outcomes.append((rid, method, ok))
            return ok, result

        run.c.wb.request = recording
        for _ in range(24):
            await run.poll()
            availability.append(run.c.available)
            await asyncio.sleep(scaled(1))
        return run

    run = run_scenario(monkeypatch, charger, body=body)

    # Once up, a lost status reply here and there must not blank the device.
    assert True in availability
    first_up = availability.index(True)
    assert all(availability[first_up:]), (
        f"went unavailable after a single fault: {availability}")
    assert len(charger.links) == 1, (
        f"reconnected {len(charger.links) - 1}x -- a slow write must not force a reconnect")
    assert charger.garbled == 0
    assert len(charger.requests) >= 35, "the run did not get through the faults"

    failed = {rid for rid, method, ok in outcomes if not ok and method != "g_halocfg"}
    injected = set(charger.faulted_ids)
    assert failed == injected, (
        f"failed but not faulted: {sorted(failed - injected)} "
        f"(a late reply poisoned the next request); "
        f"faulted but not failed: {sorted(injected - failed)}")

    c = run.c
    assert c.available and c.status == "LOCKED" and c.locked is True
    assert c.firmware == "6.13.6" and c.serial_number == "01414644"
    assert c.meter.measured_power == 351 and c.voltage == 230 and c.voltage_source == "meter"
    assert c.house_power == 351  # charger idle: everything is the house
    assert c.realtime.get("grid") is None  # this firmware has no lifetime counter
