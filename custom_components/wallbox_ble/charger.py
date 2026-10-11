"""Decoding the charger's own BAPI responses.

Formats follow the botts7/esp32-wallbox reference gateway, which records each
against real Pulsar Plus / Max units. Kept free of Home Assistant imports so it
can be unit tested.
"""
from __future__ import annotations

NOMINAL_VOLTAGE = 230.0  # V, used until the charger has told us better
CURRENT_SCALE = 0.1  # r_dat L1/L2/L3 are reported in 0.1 A

# The charger reports no voltage, but while charging it reports both its power
# (r_dat.cp, kW) and its phase currents, so voltage = power / current. Below this
# current the quotient is too coarse to trust: cp has 10 W resolution, which is
# ~1.7 V at 6 A but would be ~5 V at 2 A.
MIN_CURRENT_FOR_VOLTAGE = 5.0  # A
PLAUSIBLE_VOLTAGE = (180.0, 260.0)  # V, line-to-neutral

# r_dat.st codes in which a car is plugged in. Code 6 (locked) folds "locked,
# no car" and "locked, car" together; r_sta.charger_status 19 tells them apart.
CAR_CONNECTED_STATES = frozenset({1, 2, 3, 4, 5, 8, 10, 11, 12, 13, 18, 19})
LOCKED_STATE = 6
REALTIME_LOCKED_WITH_CAR = 19

HALO_LEVELS = ("Off", "Low", "Medium", "High")
# A representative brightness inside each level's band, so a write reads back
# as the same level (bands: 0 / 1-33 / 34-66 / 67-100).
HALO_BRIGHTNESS = {"Off": 0, "Low": 20, "Medium": 50, "High": 100}

AUTOLOCK_MINUTES = (1, 60)  # the Wallbox app's range


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def charger_current(status) -> float:
    """Total current the charger is drawing, A (sum over its phases)."""
    status = status or {}
    return sum(_number(status.get(k)) or 0.0 for k in ("L1", "L2", "L3")) * CURRENT_SCALE


def charging_power(status) -> float | None:
    """The charger's own power reading, W, or None if it does not report one."""
    kw = _number((status or {}).get("cp"))
    return None if kw is None else kw * 1000


def implied_voltage(status) -> float | None:
    """Supply voltage worked out from the charger's power and current, V.

    Only while charging enough current for the quotient to be meaningful, and
    only if the result looks like a mains voltage.
    """
    power = charging_power(status)
    current = charger_current(status)
    if power is None or current < MIN_CURRENT_FOR_VOLTAGE:
        return None
    voltage = power / current
    low, high = PLAUSIBLE_VOLTAGE
    return voltage if low <= voltage <= high else None


def car_connected(status_code, realtime=None) -> bool | None:
    """Whether a car is plugged in, or None when the status is unknown."""
    if status_code is None:
        return None
    if status_code == LOCKED_STATE:
        return (realtime or {}).get("charger_status") == REALTIME_LOCKED_WITH_CAR
    return status_code in CAR_CONNECTED_STATES


def autolock_seconds(result) -> int | None:
    """Auto-lock delay from g_alo, s; 0 means off.

    Pulsar Max answers with a bare number of seconds; older firmware may wrap
    it as {"enabled": ..., "time": ...}.
    """
    if isinstance(result, dict):
        seconds = _number(result.get("time")) or 0
        if not seconds and result.get("enabled"):
            return None  # enabled without a delay: nothing usable to show
        return int(seconds)
    seconds = _number(result)
    return None if seconds is None else int(seconds)


def halo_level(result) -> str | None:
    """Halo LED level from g_halocfg's brightness."""
    if not isinstance(result, dict):
        return None
    bright = _number(result.get("bright"))
    if bright is None or bright < 0:
        return None
    if bright == 0:
        return "Off"
    if bright <= 33:
        return "Low"
    if bright <= 66:
        return "Medium"
    return "High"


def halo_payload(level: str, current) -> dict:
    """s_halocfg body for ``level``.

    The charger wants the whole object; a bare level is silently ignored. Keep
    the standby mode and timeout as last read, changing brightness only.
    """
    current = current if isinstance(current, dict) else {}
    mode = _number(current.get("mode"))
    time_s = _number(current.get("time_s"))
    return {
        "bright": HALO_BRIGHTNESS[level],
        "mode": 1 if mode is None else int(mode),
        "time_s": 10 if time_s is None else int(time_s),
    }


def notifications(result) -> tuple[int, str | None]:
    """(count, latest message) from r_not, which returns a list."""
    if not isinstance(result, list):
        return 0, None
    latest = None
    if result and isinstance(result[0], dict):
        first = result[0]
        latest = first.get("message") or first.get("msg") or first.get("text")
    return len(result), latest


def session_count(result) -> int | None:
    """Lifetime charging sessions from r_ses.

    ``last`` is the count. ``size`` is the log buffer's capacity on some
    firmware (99999 on the Max), so it is only used when it looks like a count.
    """
    if not isinstance(result, dict):
        return None
    last = _number(result.get("last"))
    if last is not None:
        return int(last)
    size = _number(result.get("size"))
    if size is not None and 0 <= size < 50000:
        return int(size)
    return None


def grounding(result) -> str | None:
    """Grounding status from r_wel, as text.

    Families disagree on the shape and on which integer means healthy (Max
    reports 0, Plus 1), so an integer is shown as a code, not judged.
    """
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        for key in ("status", "grounding"):
            if isinstance(result.get(key), str):
                return result[key]
        return None
    number = _number(result)
    return None if number is None else f"Code {int(number)}"


def serial_number(result) -> str | None:
    if isinstance(result, str):
        return result
    if isinstance(result, dict) and isinstance(result.get("sn"), str):
        return result["sn"]
    return None


def firmware(response) -> str | None:
    """Charger app firmware from fw_v_, which puts it at the top level ("s")."""
    if isinstance(response, dict) and isinstance(response.get("s"), str):
        return response["s"]
    return None
