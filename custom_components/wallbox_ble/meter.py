"""The Power Boost / Power Meter accessory, read over BLE with BAPI ``r_dca``.

Chargers with Wallbox's Power Boost (an EM112/EM340-class meter wired at the
grid connection) report what that meter sees. The field layout, per the
botts7/esp32-wallbox reference gateway:

    c1, c2, c3   current per phase, in 0.1 A
    p1, p2, p3   active power per phase, W
    v1, v2, v3   voltage per phase, V
    e            lifetime energy, Wh

A charger without the accessory answers ``r_dca`` with BAPI error code 4.

Everything here is worked out from the **current** alone. Power Boost only
needs current to do its job, and installers often wire the meter's current
clamp without a voltage reference -- then ``p``, ``v`` and ``e`` are all 0
and only ``c`` means anything. Current is converted to power with the best
voltage available (see ``charger.implied_voltage``), the same one used for the
charger, so the figures add up: grid = house + charger.

Where the meter sits matters for what "house" means. Power Boost meters are
installed at the main connection, so they measure the whole supply *including
the charger*; the rest of the house is the grid reading minus the charger.

Kept free of Home Assistant imports so the arithmetic can be unit tested.
"""
from __future__ import annotations

from dataclasses import dataclass

CURRENT_SCALE = 0.1  # c1..c3 are reported in 0.1 A
WH_PER_KWH = 1000

# BAPI error code a charger returns when no meter accessory is fitted.
ERROR_NOT_SUPPORTED = 4


def _number(value):
    """A reported value as a float, or None when absent or not numeric."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


@dataclass(frozen=True)
class MeterReading:
    """One ``r_dca`` response."""

    current: tuple  # A per phase (L1, L2, L3), None where not reported
    energy_kwh: float | None  # 0 / meaningless without a voltage reference

    @property
    def grid_current(self) -> float:
        """Total current through the grid connection, A.

        A current clamp has no sense of direction without a voltage
        reference, so this is always positive.
        """
        return sum(abs(c) for c in self.current if c is not None)


def parse_meter(response) -> MeterReading | None:
    """Decode an ``r_dca`` result object, or None if it is not one."""
    if not isinstance(response, dict):
        return None
    current = tuple(
        None if (c := _number(response.get(f"c{phase}"))) is None else c * CURRENT_SCALE
        for phase in (1, 2, 3)
    )
    energy = _number(response.get("e"))
    if all(c is None for c in current):
        # Without current there is nothing this integration can use.
        return None
    return MeterReading(
        current=current,
        energy_kwh=None if energy is None else energy / WH_PER_KWH,
    )


def house_current(reading: MeterReading | None, charger_amps) -> float | None:
    """What the rest of the house draws, A.

    The meter sits at the grid connection, so it includes the charger; take
    the charger's own current off. Never below zero: with no generation on
    the connection the house cannot feed the grid, so a negative result is
    only the two instruments disagreeing by a fraction of an amp.
    """
    if reading is None or charger_amps is None:
        return None
    return max(0.0, reading.grid_current - charger_amps)
