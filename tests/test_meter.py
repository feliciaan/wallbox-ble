"""Power Boost meter from current only, the coordinator's poll cycle, and sensors."""
import asyncio
import types

import pytest

from _loader import api, load, meter

coordinator_mod = load("coordinator")
sensor_mod = load("sensor")

# A meter wired without a voltage reference: only the current means anything.
CURRENT_ONLY = {"v1": 0, "v2": 0, "v3": 0, "p1": 0, "p2": 0, "p3": 0,
                "c1": 220, "c2": 0, "c3": 0, "e": 0}


# ---------------------------------------------------------------- decoding

def test_current_only_meter():
    r = meter.parse_meter(CURRENT_ONLY)
    assert r.grid_current == pytest.approx(22.0)  # 0.1 A units


def test_three_phase_current_sums():
    r = meter.parse_meter({"c1": 100, "c2": 50, "c3": 25})
    assert r.grid_current == pytest.approx(17.5)


@pytest.mark.parametrize("response", [None, 4, "x", [], {}, {"v1": 230, "p1": 900}])
def test_no_current_is_no_reading(response):
    assert meter.parse_meter(response) is None


def test_house_current_subtracts_the_charger_and_never_goes_negative():
    r = meter.parse_meter(CURRENT_ONLY)
    assert meter.house_current(r, 16.0) == pytest.approx(6.0)
    # The meter reading 0.2 A under the charger is instrument mismatch, not export.
    assert meter.house_current(meter.parse_meter({"c1": 158}), 16.0) == 0.0
    assert meter.house_current(None, 16.0) is None


# ---------------------------------------------------------------- poll cycle

class FakeCharger:
    """Answers BAPI reads from a table; counts what was asked."""

    ready = True
    pairing_auth_failed = False

    def __init__(self, status, answers=None):
        self.status = status
        self.answers = answers or {}
        self.reads = []
        self.writes = []

    async def async_get_max_charge_current(self):
        return True, 32

    async def async_get_data(self):
        return True, self.status

    async def async_get_meter(self):
        return True, self.answers.get("r_dca", CURRENT_ONLY), None

    async def async_read(self, method):
        self.reads.append(method)
        if method not in self.answers:
            return True, None, {"code": 4}, {"error": {"code": 4}}
        answer = self.answers[method]
        response = answer if method == "fw_v_" else {"r": answer}
        return True, (None if method == "fw_v_" else answer), None, response

    async def request(self, method, parameter=None):
        self.writes.append((method, parameter))
        return True, None


ANSWERS = {
    "r_sta": {"lock_status": 1, "max_available_current": 25, "grid": 1234567,
              "charger_status": 19, "ocpp_status": 1},
    "g_alo": 120,
    "g_halocfg": {"bright": 80, "mode": 0, "time_s": 30},
    "r_not": [{"message": "Grounding fault"}],
    "r_ses": {"last": 412},
    "r_hsh": 25,
    "fw_v_": {"s": "6.11.26", "p": "MAX"},
    "r_sn_": "1414644",
    "r_wel": 0,
}


def make(status, answers=ANSWERS):
    c = coordinator_mod.WallboxBLEDataUpdateCoordinator(hass=None)
    c.wb = FakeCharger(status, dict(answers))
    return c


def run_poll(c):
    data = asyncio.run(c._async_update_data())
    c.data = data  # what DataUpdateCoordinator does with the return value
    return data


def test_full_poll_while_charging():
    # Charging 16 A, charger reports 3.76 kW -> 235 V; meter sees 22 A in total.
    c = make({"st": 1, "L1": 160, "L2": 0, "L3": 0, "cp": 3.76, "cur": 16})
    run_poll(c)

    assert c.status == "CHARGING" and c.car_connected is True
    assert c.voltage == pytest.approx(235.0) and c.voltage_measured
    assert c.charger_power == pytest.approx(3760)  # = cp, via the same voltage
    assert c.grid_power == pytest.approx(22 * 235)
    assert c.house_current == pytest.approx(6.0)
    assert c.house_power == pytest.approx(6 * 235)
    # The three figures shown add up.
    assert c.grid_power == pytest.approx(c.house_power + c.charger_power)

    assert c.locked is True  # from r_sta.lock_status, not st == 6
    assert c.autolock_seconds == 120
    assert c.notification_count == 1 and c.latest_notification == "Grounding fault"
    assert c.session_count == 412 and c.power_boost_limit == 25
    assert (c.firmware, c.serial_number, c.grounding) == ("6.11.26", "1414644", "Code 0")


def test_voltage_is_kept_between_charges():
    c = make({"st": 1, "L1": 160, "cp": 3.76})
    run_poll(c)
    c.wb.status = {"st": 0, "L1": 0, "cp": 0}
    run_poll(c)
    assert c.voltage == pytest.approx(235.0)
    assert c.charger_power == 0
    assert c.grid_power == pytest.approx(22 * 235)  # whole grid draw is house now


def test_nominal_voltage_until_the_first_charge():
    c = make({"st": 0, "L1": 0})
    run_poll(c)
    assert c.voltage == 230.0 and not c.voltage_measured


def test_status_19_no_longer_crashes():
    c = make({"st": 19, "L1": 0})
    run_poll(c)
    assert c.status == "CONNECTED_NO_CURRENT" and c.available


def test_unknown_status_code_does_not_crash():
    c = make({"st": 42, "L1": 0})
    run_poll(c)
    assert c.status == "UNKNOWN_42" and c.available


def test_slow_poll_cadence():
    c = make({"st": 0, "L1": 0})
    run_poll(c)
    first = list(c.wb.reads)
    assert {"g_alo", "g_halocfg", "r_not", "r_ses", "r_hsh", "fw_v_", "r_sn_", "r_wel"} <= set(first)
    c.wb.reads.clear()
    run_poll(c)
    assert c.wb.reads == ["r_sta"], "settings re-read on every 10 s poll"


def test_identity_retried_until_something_answers():
    c = make({"st": 0, "L1": 0}, answers={})
    run_poll(c)
    assert c._info_read is True  # an error answer is still an answer


def test_lock_falls_back_to_status_without_realtime():
    answers = {k: v for k, v in ANSWERS.items() if k != "r_sta"}
    c = make({"st": 6, "L1": 0}, answers)
    run_poll(c)
    assert c.locked is True


def test_autolock_and_halo_writes():
    c = make({"st": 0, "L1": 0})
    run_poll(c)
    asyncio.run(c.async_set_autolock(0))
    asyncio.run(c.async_set_halo("Low"))
    assert ("s_alo", 0) in c.wb.writes
    assert ("s_halocfg", {"bright": 20, "mode": 0, "time_s": 30}) in c.wb.writes


# ---------------------------------------------------------------- sensors

def describe(key):
    return next(d for d in sensor_mod.ENTITY_DESCRIPTIONS if d.key == key)


def value(key, c):
    return describe(key).value_fn(c)


def available(key, c):
    check = describe(key).available_fn
    return check is None or check(c)


def test_sensor_values_from_a_poll():
    c = make({"st": 1, "L1": 160, "cp": 3.76, "en": 512})
    run_poll(c)
    assert value("power", c) == 3760
    assert value("grid_current", c) == 22.0
    assert value("grid_power", c) == 5170
    assert value("house_current", c) == 6.0
    assert value("house_power", c) == 1410
    assert value("supply_voltage", c) == 235.0
    assert value("lifetime_energy", c) == 1234.57  # r_sta.grid is Wh
    assert value("max_available_current", c) == 25
    assert value("ocpp_status", c) == "Not configured"
    assert value("latest_notification", c) == "Grounding fault"


def test_meter_sensors_unavailable_without_a_meter():
    c = make({"st": 0, "L1": 160})
    c.meter = None
    for key in ("grid_current", "grid_power", "house_current", "house_power", "grid_energy"):
        assert not available(key, c)
        assert value(key, c) is None


def test_meter_energy_is_off_by_default():
    # Meaningless on a meter without a voltage reference.
    assert describe("grid_energy").entity_registry_enabled_default is False


def test_status_code_list_covers_19():
    assert api.WallboxBLEApiConst.STATUS_CODES[19] == "CONNECTED_NO_CURRENT"
