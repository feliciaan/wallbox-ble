"""Decoding the charger's own responses (charger.py)."""
import pytest

from _loader import charger


# ------------------------------------------------------- voltage from power

def test_voltage_from_power_and_current():
    # 16 A on one phase, charger reports 3.76 kW -> 235 V
    assert charger.implied_voltage({"L1": 160, "cp": 3.76}) == pytest.approx(235.0)


def test_voltage_uses_total_current_over_phases():
    # 3 x 16 A, 11.04 kW -> 230 V per phase
    status = {"L1": 160, "L2": 160, "L3": 160, "cp": 11.04}
    assert charger.implied_voltage(status) == pytest.approx(230.0)


@pytest.mark.parametrize(
    "status",
    [
        {"L1": 0, "cp": 0},  # idle
        {"L1": 30, "cp": 0.69},  # 3 A: below the trustworthy current
        {"L1": 160},  # no power reported
        {"L1": 160, "cp": 1.0},  # 62 V: not a mains voltage
        {"L1": 160, "cp": 9.0},  # 562 V: not a mains voltage
        None,
    ],
)
def test_no_voltage_when_it_cannot_be_trusted(status):
    assert charger.implied_voltage(status) is None


def test_charger_current_and_power():
    assert charger.charger_current({"L1": 160, "L2": 5}) == pytest.approx(16.5)
    assert charger.charging_power({"cp": 3.68}) == pytest.approx(3680)
    assert charger.charging_power({}) is None


# ------------------------------------------------------- car connected

@pytest.mark.parametrize("st", [1, 2, 3, 4, 5, 8, 10, 11, 12, 13, 18, 19])
def test_car_connected_states(st):
    assert charger.car_connected(st) is True


@pytest.mark.parametrize("st", [0, 7, 9, 14, 15, 16, 17])
def test_car_not_connected_states(st):
    assert charger.car_connected(st) is False


def test_locked_needs_realtime_to_tell():
    assert charger.car_connected(6, {"charger_status": 19}) is True
    assert charger.car_connected(6, {"charger_status": 6}) is False
    assert charger.car_connected(6, None) is False
    assert charger.car_connected(None) is None


# ------------------------------------------------------- settings

@pytest.mark.parametrize(
    "result,expected",
    [(60, 60), (0, 0), ({"enabled": 1, "time": 120}, 120), ({"enabled": 0, "time": 0}, 0),
     ({"enabled": 1}, None), ("x", None), (None, None)],
)
def test_autolock_seconds(result, expected):
    assert charger.autolock_seconds(result) == expected


@pytest.mark.parametrize(
    "bright,level",
    [(0, "Off"), (1, "Low"), (33, "Low"), (34, "Medium"), (66, "Medium"), (67, "High"), (100, "High")],
)
def test_halo_bands(bright, level):
    assert charger.halo_level({"bright": bright}) == level


def test_halo_write_round_trips_and_keeps_standby_settings():
    current = {"bright": 80, "mode": 0, "time_s": 30}
    for level in charger.HALO_LEVELS:
        payload = charger.halo_payload(level, current)
        assert charger.halo_level(payload) == level
        assert payload["mode"] == 0 and payload["time_s"] == 30
    # Defaults when the standby settings were never read.
    assert charger.halo_payload("High", None) == {"bright": 100, "mode": 1, "time_s": 10}


# ------------------------------------------------------- counters and info

def test_notifications():
    assert charger.notifications([{"message": "Grounding fault"}, {"msg": "x"}]) == (2, "Grounding fault")
    assert charger.notifications([]) == (0, None)
    assert charger.notifications(None) == (0, None)


@pytest.mark.parametrize(
    "result,expected",
    [({"last": 412}, 412), ({"size": 99999}, None), ({"size": 37}, 37), (None, None)],
)
def test_session_count_ignores_buffer_capacity(result, expected):
    assert charger.session_count(result) == expected


@pytest.mark.parametrize(
    "result,expected",
    [("OK", "OK"), ({"status": "Fault"}, "Fault"), ({"grounding": "OK"}, "OK"), (0, "Code 0"), ({}, None)],
)
def test_grounding_shapes(result, expected):
    assert charger.grounding(result) == expected


def test_identity():
    assert charger.serial_number("1414644") == "1414644"
    assert charger.serial_number({"sn": "1414644"}) == "1414644"
    # fw_v_ answers outside "r".
    assert charger.firmware({"id": 5, "s": "6.11.26", "p": "MAX"}) == "6.11.26"
    assert charger.firmware({"r": None}) is None
