"""Power Boost meter: decoding r_dca, polling it, and the grid/house sensors."""
import asyncio
import types

import pytest

from _loader import load, meter

coordinator_mod = load("coordinator")
sensor_mod = load("sensor")

# Field layout per the botts7/esp32-wallbox reference gateway.
THREE_PHASE = {"v1": 231, "v2": 229, "v3": 230, "p1": 1200, "p2": 800, "p3": 450,
               "c1": 52, "c2": 35, "c3": 20, "e": 1234567}
SINGLE_PHASE = {"v1": 240, "v2": 0, "v3": 0, "p1": 4500, "p2": 7, "p3": 0,
                "c1": 188, "c2": 0, "c3": 0, "e": 50000}


# ---------------------------------------------------------------- decoding

def test_three_phase_reading():
    r = meter.parse_meter(THREE_PHASE)
    assert r.grid_power == 2450
    assert r.current == pytest.approx((5.2, 3.5, 2.0))  # 0.1 A units
    assert r.energy_kwh == pytest.approx(1234.567)  # Wh -> kWh
    assert r.mean_voltage == pytest.approx(230)
    assert all(r.phase_present(i) for i in range(3))


def test_single_phase_ignores_phases_without_voltage():
    r = meter.parse_meter(SINGLE_PHASE)
    assert r.phase_present(0)
    assert not r.phase_present(1) and not r.phase_present(2)
    # A stray reading on a phase with no voltage must not leak into the total.
    assert r.grid_power == 4500
    assert r.mean_voltage == 240


def test_export_is_negative():
    r = meter.parse_meter({**THREE_PHASE, "p1": -3000, "p2": -100, "p3": 0})
    assert r.grid_power == -3100


@pytest.mark.parametrize("response", [None, 4, "x", [], {}, {"foo": 1}])
def test_non_readings_are_rejected(response):
    assert meter.parse_meter(response) is None


def test_non_numeric_fields_are_dropped():
    r = meter.parse_meter({"v1": 230, "p1": True, "c1": "12", "e": 1000})
    assert r.power[0] is None and r.current[0] is None
    assert r.energy_kwh == 1.0


def test_house_power_subtracts_the_charger():
    r = meter.parse_meter(THREE_PHASE)
    assert meter.house_power(r, 2000) == 450
    assert meter.house_power(r, 0) == 2450
    assert meter.house_power(None, 2000) is None
    assert meter.house_power(r, None) is None


# ---------------------------------------------------------------- polling

class FakeApi:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = 0

    async def async_get_meter(self):
        self.calls += 1
        return self.answers.pop(0)


def make_coordinator(api):
    c = coordinator_mod.WallboxBLEDataUpdateCoordinator(hass=None)
    c.wb = api
    return c


def test_reading_is_stored():
    c = make_coordinator(FakeApi((True, THREE_PHASE, None)))
    asyncio.run(c._async_update_meter())
    assert c.meter_supported is True and c.meter.grid_power == 2450


def test_not_supported_stops_polling():
    api = FakeApi((True, None, {"code": 4}))
    c = make_coordinator(api)
    asyncio.run(c._async_update_meter())
    asyncio.run(c._async_update_meter())
    assert c.meter_supported is False and c.meter is None
    assert api.calls == 1, "kept polling a charger that has no meter"


def test_other_errors_keep_polling():
    api = FakeApi((True, None, {"code": 2}), (True, THREE_PHASE, None))
    c = make_coordinator(api)
    asyncio.run(c._async_update_meter())
    assert c.meter_supported is None
    asyncio.run(c._async_update_meter())
    assert c.meter_supported is True and api.calls == 2


def test_missed_answer_keeps_the_last_reading():
    c = make_coordinator(FakeApi((True, THREE_PHASE, None), (False, None, None)))
    asyncio.run(c._async_update_meter())
    asyncio.run(c._async_update_meter())
    assert c.meter.grid_power == 2450


# ---------------------------------------------------------------- sensors

def coordinator_with(data, reading):
    return types.SimpleNamespace(data=data, meter=reading, available=True)


def describe(key):
    return next(d for d in sensor_mod.ENTITY_DESCRIPTIONS if d.key == key)


def value(key, coordinator):
    return describe(key).value_fn(coordinator)


def available(key, coordinator):
    check = describe(key).available_fn
    return check is None or check(coordinator)


def test_house_power_uses_the_measured_voltage():
    # Charging 16 A on one phase while the meter reads 240 V and 4500 W.
    c = coordinator_with({"L1": 160, "L2": 0, "L3": 0}, meter.parse_meter(SINGLE_PHASE))
    assert value("power", c) == 3840  # 16 A x 240 V, not x 230 V
    assert value("grid_power", c) == 4500
    assert value("house_power", c) == 660
    # The figures shown must add up: grid = house + charger.
    assert value("house_power", c) + value("power", c) == value("grid_power", c)


def test_without_a_meter():
    c = coordinator_with({"L1": 160}, None)
    assert value("power", c) == 3680  # falls back to 230 V nominal
    for key in ("grid_power", "house_power", "grid_energy", "grid_power_l1"):
        assert not available(key, c)
        assert value(key, c) is None


def test_per_phase_sensors_follow_the_supply():
    c = coordinator_with({}, meter.parse_meter(SINGLE_PHASE))
    assert available("grid_power_l1", c) and value("grid_power_l1", c) == 4500
    assert available("grid_current_l1", c) and value("grid_current_l1", c) == 18.8
    assert not available("grid_power_l3", c)
    assert describe("grid_power_l3").entity_registry_enabled_default is False


def test_energy_sensor_is_dashboard_ready():
    d = describe("grid_energy")
    assert d.state_class == "total_increasing" and d.native_unit_of_measurement == "kWh"
    assert value("grid_energy", coordinator_with({}, meter.parse_meter(THREE_PHASE))) == 1234.57
