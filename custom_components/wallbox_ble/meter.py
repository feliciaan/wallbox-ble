"""The Power Boost / Power Meter accessory, read over BLE with BAPI ``r_dca``.

Chargers with Wallbox's Power Boost (an EM112/EM340-class meter wired at the
grid connection) report what that meter sees. The field layout, verified
against the botts7/esp32-wallbox reference gateway:

    p1, p2, p3   active power per phase, W (signed: negative = exporting)
    v1, v2, v3   voltage per phase, V (0 on phases the meter does not see)
    c1, c2, c3   current per phase, in 0.1 A
    e            lifetime energy, Wh

A charger without the accessory answers ``r_dca`` with BAPI error code 4.

Where the meter sits matters for what "house" means. Power Boost meters are
installed at the main connection, so they measure the whole supply *including
the charger*. What the rest of the house draws is therefore the grid reading
minus the charger's own draw -- which is how ``house_power`` is computed. With
solar on the same connection, the meter only sees the net exchange, so that
figure becomes the house's net draw from the grid rather than its consumption.

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
    """One ``r_dca`` response, decoded into SI units."""

    power: tuple  # W per phase (L1, L2, L3), None where not reported
    voltage: tuple  # V per phase
    current: tuple  # A per phase
    energy_kwh: float | None

    def phase_present(self, index: int) -> bool:
        """Whether the meter sees phase ``index`` (0-based).

        L1 always counts when it reports anything. L2/L3 count only with a
        non-zero voltage: a single-phase meter reports them as 0, and showing
        "0 W on L3" for a phase that does not exist would be misleading.
        """
        if index == 0:
            return any(
                v is not None
                for v in (self.power[0], self.voltage[0], self.current[0])
            )
        voltage = self.voltage[index]
        return voltage is not None and voltage > 0

    @property
    def grid_power(self) -> float | None:
        """Total power at the grid connection, W; negative while exporting."""
        values = [
            p
            for i, p in enumerate(self.power)
            if p is not None and self.phase_present(i)
        ]
        return sum(values) if values else None

    @property
    def mean_voltage(self) -> float | None:
        """Average voltage over the phases the meter sees, V."""
        values = [v for v in self.voltage if v is not None and v > 0]
        return sum(values) / len(values) if values else None


def parse_meter(response) -> MeterReading | None:
    """Decode an ``r_dca`` result object, or None if it is not one."""
    if not isinstance(response, dict):
        return None

    def triple(prefix, scale=1.0):
        out = []
        for phase in (1, 2, 3):
            value = _number(response.get(f"{prefix}{phase}"))
            out.append(None if value is None else value * scale)
        return tuple(out)

    reading = MeterReading(
        power=triple("p"),
        voltage=triple("v"),
        current=triple("c", CURRENT_SCALE),
        energy_kwh=(
            None
            if (e := _number(response.get("e"))) is None
            else e / WH_PER_KWH
        ),
    )
    if not reading.phase_present(0) and reading.energy_kwh is None:
        # An object with none of the meter's fields is not a meter reading.
        return None
    return reading


def house_power(reading: MeterReading | None, charger_power_w) -> float | None:
    """What the rest of the house draws from the grid, W.

    The meter sits at the grid connection, so its reading includes the
    charger; subtract the charger's own draw to get everything else.
    """
    if reading is None or reading.grid_power is None or charger_power_w is None:
        return None
    return reading.grid_power - charger_power_w
