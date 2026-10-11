from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
)

from .const import DOMAIN, LOGGER
from .coordinator import WallboxBLEDataUpdateCoordinator
from .entity import WallboxBLEEntity
from .meter import house_power

# The charger's BLE status frame (GET_STATUS) carries more than status/current:
#   L1/L2/L3 : per-phase current, reported in 0.1 A units
#   en       : session energy counter, ~10 Wh per unit (-> 0.01 kWh)
# We surface these as sensors so energy/power can be read locally (no cloud).
EN_TO_KWH = 0.01          # 'en' counter unit ~= 10 Wh
CURRENT_SCALE = 0.1       # L1/L2/L3 reported in 0.1 A
NOMINAL_VOLTAGE = 230     # V (line-neutral) for the measured-power estimate


@dataclass(frozen=True, kw_only=True)
class WallboxBLESensorDescription(SensorEntityDescription):
    """Sensor description with a value function reading the coordinator."""

    value_fn: Callable[[WallboxBLEDataUpdateCoordinator], object] | None = None
    # Extra availability condition on top of the coordinator's; the meter
    # sensors use it to go unavailable on chargers without a meter.
    available_fn: Callable[[WallboxBLEDataUpdateCoordinator], bool] | None = None


def _amps(coordinator) -> float:
    d = coordinator.data or {}
    return sum(d.get(k, 0) for k in ("L1", "L2", "L3")) * CURRENT_SCALE


def _status(coordinator):
    return coordinator.status


def _energy(coordinator):
    d = coordinator.data or {}
    return round(d.get("en", 0) * EN_TO_KWH, 2)


def _current(coordinator):
    return round(_amps(coordinator), 1)


def _charger_power(coordinator) -> float:
    """The charger's own draw, W, estimated from its per-phase currents.

    Uses the grid meter's measured voltage when there is one, so the charger
    and house figures are on the same footing; nominal 230 V otherwise.
    """
    meter = coordinator.meter
    voltage = (meter.mean_voltage if meter is not None else None) or NOMINAL_VOLTAGE
    return _amps(coordinator) * voltage


def _power(coordinator):
    return round(_charger_power(coordinator))


def _has_meter(coordinator) -> bool:
    return coordinator.meter is not None


def _grid_power(coordinator):
    if coordinator.meter is None:
        return None
    value = coordinator.meter.grid_power
    return None if value is None else round(value)


def _house_power(coordinator):
    value = house_power(coordinator.meter, _charger_power(coordinator))
    return None if value is None else round(value)


def _meter_energy(coordinator):
    if coordinator.meter is None:
        return None
    value = coordinator.meter.energy_kwh
    return None if value is None else round(value, 2)


def _phase_available(index):
    def available(coordinator) -> bool:
        return coordinator.meter is not None and coordinator.meter.phase_present(index)

    return available


def _phase_value(field, index, digits):
    def value(coordinator):
        if coordinator.meter is None:
            return None
        reading = getattr(coordinator.meter, field)[index]
        return None if reading is None else round(reading, digits)

    return value


def _phase_sensors():
    """Per-phase grid power, current and voltage: diagnostics, off by default.

    Phases the meter does not see (L2/L3 on a single-phase supply) stay
    unavailable rather than reporting a misleading 0.
    """
    kinds = (
        ("power", "Grid power", SensorDeviceClass.POWER, UnitOfPower.WATT, 0),
        ("current", "Grid current", SensorDeviceClass.CURRENT,
         UnitOfElectricCurrent.AMPERE, 1),
        ("voltage", "Grid voltage", SensorDeviceClass.VOLTAGE,
         UnitOfElectricPotential.VOLT, 0),
    )
    for field, label, device_class, unit, digits in kinds:
        for index in range(3):
            yield WallboxBLESensorDescription(
                key=f"grid_{field}_l{index + 1}",
                name=f"{label} L{index + 1}",
                device_class=device_class,
                state_class=SensorStateClass.MEASUREMENT,
                native_unit_of_measurement=unit,
                entity_category=EntityCategory.DIAGNOSTIC,
                entity_registry_enabled_default=False,
                value_fn=_phase_value(field, index, digits),
                available_fn=_phase_available(index),
            )


ENTITY_DESCRIPTIONS = (
    WallboxBLESensorDescription(
        key="wallbox_ble",
        name="Status",
        value_fn=_status,
    ),
    WallboxBLESensorDescription(
        key="energy",
        name="Session energy",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        value_fn=_energy,
    ),
    WallboxBLESensorDescription(
        key="current",
        name="Charging current (measured)",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        value_fn=_current,
    ),
    WallboxBLESensorDescription(
        key="power",
        name="Charging power (measured)",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        value_fn=_power,
    ),
    # Power Boost meter at the grid connection (r_dca). Unavailable on chargers
    # without one. See meter.py for what "house" means here.
    WallboxBLESensorDescription(
        key="grid_power",
        name="Grid power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        value_fn=_grid_power,
        available_fn=_has_meter,
    ),
    WallboxBLESensorDescription(
        key="house_power",
        name="House power (excl. charger)",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        value_fn=_house_power,
        available_fn=_has_meter,
    ),
    WallboxBLESensorDescription(
        key="grid_energy",
        name="Grid meter energy",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        value_fn=_meter_energy,
        available_fn=_has_meter,
    ),
    *_phase_sensors(),
)


async def async_setup_entry(hass, entry, async_add_devices):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_devices(
        WallboxBLESensor(
            coordinator=coordinator,
            entity_description=entity_description,
        )
        for entity_description in ENTITY_DESCRIPTIONS
    )


class WallboxBLESensor(WallboxBLEEntity, SensorEntity):
    def __init__(
        self,
        coordinator: WallboxBLEDataUpdateCoordinator,
        entity_description: WallboxBLESensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = entity_description
        # Keep the original Status sensor's unique_id = entry_id (unchanged);
        # give the new sensors a distinct suffix so they don't collide.
        if entity_description.key != "wallbox_ble":
            self._attr_unique_id = (
                f"{coordinator.config_entry.entry_id}_{entity_description.key}"
            )

    @property
    def available(self):
        if not self.coordinator.available:
            return False
        check = self.entity_description.available_fn
        return check is None or check(self.coordinator)

    @property
    def native_value(self):
        return self.entity_description.value_fn(self.coordinator)
