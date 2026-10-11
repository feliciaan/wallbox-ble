"""The Power Boost / Power Meter accessory, read over BLE with BAPI ``r_dca``.

Chargers with Wallbox's Power Boost (an EM112/EM340-class meter wired at the
grid connection) report what that meter sees. The field layout, per the
botts7/esp32-wallbox reference gateway:

    c1, c2, c3   current per phase, in 0.1 A
    p1, p2, p3   active power per phase, W
    v1, v2, v3   voltage per phase, V
    e            lifetime energy, Wh

A charger without the accessory answers ``r_dca`` with BAPI error code 4.

With a voltage reference wired to the meter, ``p`` is real active power
(it accounts for power factor, which current x voltage does not) and ``v``
and ``e`` are meaningful, so they are used. Power Boost itself only needs
current, though, and installers often wire the clamp without a voltage
reference -- then ``p``, ``v`` and ``e`` are all 0 and only ``c`` means
anything. ``has_voltage`` tells the two apart; without it the coordinator
falls back to current x the best voltage it has.

Direction is ignored: an install without generation can only import, and a
current clamp fitted the wrong way round would otherwise read negative.

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


# Below this a phase is "not wired to a voltage reference", not a brown-out.
PLAUSIBLE_VOLTAGE = (180.0, 260.0)  # V, line-to-neutral


@dataclass(frozen=True)
class MeterReading:
    """One ``r_dca`` response."""

    current: tuple  # A per phase (L1, L2, L3), None where not reported
    voltage: tuple  # V per phase; 0 without a voltage reference
    power: tuple  # W per phase; 0 without a voltage reference
    energy_kwh: float | None  # 0 / meaningless without a voltage reference

    def _live_phases(self):
        low, high = PLAUSIBLE_VOLTAGE
        return [
            i for i, v in enumerate(self.voltage)
            if v is not None and low <= v <= high
        ]

    @property
    def has_voltage(self) -> bool:
        """Whether the meter is wired to a voltage reference."""
        return bool(self._live_phases())

    @property
    def measured_voltage(self) -> float | None:
        """Mean voltage over the phases that have one, V."""
        phases = self._live_phases()
        if not phases:
            return None
        return sum(self.voltage[i] for i in phases) / len(phases)

    @property
    def measured_power(self) -> float | None:
        """Active power through the grid connection, W; None without voltage."""
        phases = self._live_phases()
        if not phases:
            return None
        return sum(abs(self.power[i] or 0.0) for i in phases)

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

    def triple(prefix, scale=1.0):
        return tuple(
            None if (x := _number(response.get(f"{prefix}{phase}"))) is None else x * scale
            for phase in (1, 2, 3)
        )

    current = triple("c", CURRENT_SCALE)
    energy = _number(response.get("e"))
    if all(c is None for c in current):
        # Every meter reports current; an object without it is not a reading.
        return None
    return MeterReading(
        current=current,
        voltage=triple("v"),
        power=triple("p"),
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
