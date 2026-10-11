"""The new controls: auto-lock switch + delay, Halo select, reboot, car connected."""
import asyncio
import types

from _loader import load

switch = load("switch")
number = load("number")
select = load("select")
button = load("button")
binary_sensor = load("binary_sensor")


class FakeCoordinator:
    def __init__(self, autolock_seconds=None, halo_config=None, car=None):
        self.config_entry = types.SimpleNamespace(entry_id="E")
        self.available = True
        self.autolock_seconds = autolock_seconds
        self.halo_config = halo_config
        self.car_connected = car
        self.calls = []

    async def async_set_autolock(self, seconds):
        self.calls.append(("autolock", seconds))
        self.autolock_seconds = seconds
        return True

    async def async_set_halo(self, level):
        self.calls.append(("halo", level))
        return True

    async def async_reboot(self):
        self.calls.append(("reboot",))
        return True


def test_autolock_switch_restores_the_delay_after_off():
    c = FakeCoordinator(autolock_seconds=300)
    s = switch.WallboxBLEAutoLockSwitch(c, switch.AUTOLOCK)
    assert s.is_on is True
    asyncio.run(s.async_turn_off())
    assert s.is_on is False
    asyncio.run(s.async_turn_on())
    assert c.calls == [("autolock", 0), ("autolock", 300)], "delay lost across off/on"


def test_autolock_switch_unknown_until_read():
    s = switch.WallboxBLEAutoLockSwitch(FakeCoordinator(), switch.AUTOLOCK)
    assert s.available is False


def test_autolock_delay_minutes_to_seconds():
    c = FakeCoordinator(autolock_seconds=120)
    n = number.WallboxBLEAutoLockDelay(c, number.AUTOLOCK_DELAY)
    assert n.native_value == 2
    asyncio.run(n.async_set_native_value(5))
    asyncio.run(n.async_set_native_value(500))  # clamped to the app's 60 min
    assert c.calls == [("autolock", 300), ("autolock", 3600)]


def test_autolock_delay_unavailable_while_off():
    n = number.WallboxBLEAutoLockDelay(FakeCoordinator(autolock_seconds=0), number.AUTOLOCK_DELAY)
    assert n.available is False and n.native_value is None


def test_halo_select():
    c = FakeCoordinator(halo_config={"bright": 50})
    h = select.WallboxBLEHalo(c, select.HALO)
    assert h.current_option == "Medium"
    asyncio.run(h.async_select_option("Off"))
    assert c.calls == [("halo", "Off")]
    assert select.WallboxBLEHalo(FakeCoordinator(), select.HALO).available is False


def test_reboot_and_car_connected():
    c = FakeCoordinator(car=True)
    asyncio.run(button.WallboxBLEReboot(c, button.REBOOT).async_press())
    assert c.calls == [("reboot",)]
    assert binary_sensor.WallboxBLECarConnected(c, binary_sensor.CAR_CONNECTED).is_on is True


def test_new_entities_have_distinct_unique_ids():
    c = FakeCoordinator(autolock_seconds=60, halo_config={})
    ids = [
        switch.WallboxBLEAutoLockSwitch(c, switch.AUTOLOCK).unique_id,
        number.WallboxBLEAutoLockDelay(c, number.AUTOLOCK_DELAY).unique_id,
        select.WallboxBLEHalo(c, select.HALO).unique_id,
        button.WallboxBLEReboot(c, button.REBOOT).unique_id,
        binary_sensor.WallboxBLECarConnected(c, binary_sensor.CAR_CONNECTED).unique_id,
    ]
    # The original single entity per platform keeps the bare entry ID.
    assert "E" not in ids and len(set(ids)) == len(ids)
