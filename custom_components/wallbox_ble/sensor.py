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

# The charger's BLE status frame (GET_STATUS) carries more than status/current:
#   L1/L2/L3 : per-phase current, reported in 0.1 A units
#   en       : session energy counter, ~10 Wh per unit (-> 0.01 kWh)
# We surface these as sensors so energy/power can be read locally (no cloud).
EN_TO_KWH = 0.01          # 'en' counter unit ~= 10 Wh

# r_sta.ocpp_status
OCPP_STATES = {0: "Not available", 1: "Not configured", 2: "Connected", 3: "Charging"}


@dataclass(frozen=True, kw_only=True)
class WallboxBLESensorDescription(SensorEntityDescription):
    """Sensor description with a value function reading the coordinator."""

    value_fn: Callable[[WallboxBLEDataUpdateCoordinator], object] | None = None
    # Extra availability condition on top of the coordinator's; the meter
    # sensors use it to go unavailable on chargers without a meter.
    available_fn: Callable[[WallboxBLEDataUpdateCoordinator], bool] | None = None


def _rounded(value, digits=0):
    if value is None:
        return None
    return round(value) if digits == 0 else round(value, digits)


def _status(coordinator):
    return coordinator.status


def _energy(coordinator):
    d = coordinator.data or {}
    return round(d.get("en", 0) * EN_TO_KWH, 2)


def _has_meter(coordinator) -> bool:
    return coordinator.meter is not None


def _realtime(key, scale=1.0, digits=0):
    def value(coordinator):
        raw = (coordinator.realtime or {}).get(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None
        return _rounded(raw * scale, digits)

    return value


def _has_realtime(key):
    def available(coordinator) -> bool:
        return (coordinator.realtime or {}).get(key) is not None

    return available


def _ocpp(coordinator):
    code = (coordinator.realtime or {}).get("ocpp_status")
    return None if code is None else OCPP_STATES.get(code, f"Code {code}")


def _not_none(attribute):
    def available(coordinator) -> bool:
        return getattr(coordinator, attribute) is not None

    return available


def _attribute(attribute):
    return lambda coordinator: getattr(coordinator, attribute)


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
        value_fn=lambda c: round(c.charger_current, 1),
    ),
    WallboxBLESensorDescription(
        key="power",
        name="Charging power (measured)",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        value_fn=lambda c: round(c.charger_power),
    ),
    WallboxBLESensorDescription(
        key="max_available_current",
        name="Max available current",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        value_fn=_realtime("max_available_current"),
        available_fn=_has_realtime("max_available_current"),
    ),
    # Power Boost meter at the grid connection (r_dca), worked out from current
    # only -- see meter.py. Unavailable on chargers without one.
    WallboxBLESensorDescription(
        key="grid_current",
        name="Grid current",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        value_fn=lambda c: None if c.meter is None else round(c.meter.grid_current, 1),
        available_fn=_has_meter,
    ),
    WallboxBLESensorDescription(
        key="grid_power",
        name="Grid power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        value_fn=lambda c: _rounded(c.grid_power),
        available_fn=_has_meter,
    ),
    WallboxBLESensorDescription(
        key="house_current",
        name="House current (excl. charger)",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        value_fn=lambda c: _rounded(c.house_current, 1),
        available_fn=_has_meter,
    ),
    WallboxBLESensorDescription(
        key="house_power",
        name="House power (excl. charger)",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        value_fn=lambda c: _rounded(c.house_power),
        available_fn=_has_meter,
    ),
    WallboxBLESensorDescription(
        key="grid_energy",
        name="Grid meter energy",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        value_fn=lambda c: None if c.meter is None else _rounded(c.meter.energy_kwh, 2),
        # Only meaningful with a voltage reference: without one the meter
        # cannot integrate power and reports 0.
        available_fn=lambda c: c.meter is not None and c.meter.has_voltage,
    ),
    WallboxBLESensorDescription(
        key="supply_voltage",
        name="Supply voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: round(c.voltage, 1),
    ),
    WallboxBLESensorDescription(
        key="notifications",
        name="Notifications",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:bell-alert-outline",
        value_fn=_attribute("notification_count"),
        available_fn=_not_none("notification_count"),
    ),
    WallboxBLESensorDescription(
        key="latest_notification",
        name="Latest notification",
        icon="mdi:bell-outline",
        value_fn=lambda c: c.latest_notification or "None",
        available_fn=_not_none("notification_count"),
    ),
    WallboxBLESensorDescription(
        key="sessions",
        name="Total charging sessions",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:counter",
        value_fn=_attribute("session_count"),
        available_fn=_not_none("session_count"),
    ),
    WallboxBLESensorDescription(
        key="power_boost_limit",
        name="Power Boost limit",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_attribute("power_boost_limit"),
        available_fn=_not_none("power_boost_limit"),
    ),
    WallboxBLESensorDescription(
        key="firmware",
        name="Charger firmware",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:chip",
        value_fn=_attribute("firmware"),
        available_fn=_not_none("firmware"),
    ),
    WallboxBLESensorDescription(
        key="serial_number",
        name="Serial number",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:identifier",
        value_fn=_attribute("serial_number"),
        available_fn=_not_none("serial_number"),
    ),
    WallboxBLESensorDescription(
        key="grounding",
        name="Grounding",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:earth",
        value_fn=_attribute("grounding"),
        available_fn=_not_none("grounding"),
    ),
    WallboxBLESensorDescription(
        key="ocpp_status",
        name="OCPP status",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_ocpp,
        available_fn=_has_realtime("ocpp_status"),
    ),
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

    @property
    def extra_state_attributes(self):
        if self.entity_description.key == "supply_voltage":
            # meter / charger (power / current) / nominal (230 V).
            return {"source": self.coordinator.voltage_source}
        return None
