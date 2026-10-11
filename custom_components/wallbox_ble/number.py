from __future__ import annotations

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import EntityCategory, UnitOfTime

from .api import WallboxBLEApiConst
from .charger import AUTOLOCK_MINUTES
from .const import DOMAIN, LOGGER
from .coordinator import WallboxBLEDataUpdateCoordinator
from .entity import WallboxBLEEntity

ENTITY_DESCRIPTIONS = (
    NumberEntityDescription(
        key="wallbox_ble",
        name="Charge current",
        native_min_value=6,
        device_class=NumberDeviceClass.CURRENT,
        icon="mdi:flash",
    ),
)


AUTOLOCK_DELAY = NumberEntityDescription(
    key="autolock_delay",
    name="Auto-lock delay",
    icon="mdi:timer-lock",
    native_min_value=AUTOLOCK_MINUTES[0],
    native_max_value=AUTOLOCK_MINUTES[1],
    native_step=1,
    native_unit_of_measurement=UnitOfTime.MINUTES,
    mode=NumberMode.BOX,
    entity_category=EntityCategory.CONFIG,
)


async def async_setup_entry(hass, entry, async_add_devices):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_devices(
        [
            *(
                WallboxBLENumber(
                    coordinator=coordinator,
                    entity_description=entity_description,
                )
                for entity_description in ENTITY_DESCRIPTIONS
            ),
            WallboxBLEAutoLockDelay(coordinator, AUTOLOCK_DELAY),
        ]
    )


class WallboxBLENumber(WallboxBLEEntity, NumberEntity):
    def __init__(
        self,
        coordinator: WallboxBLEDataUpdateCoordinator,
        entity_description: NumberEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = entity_description

    @property
    def available(self):
        return self.coordinator.available

    @property
    def native_value(self) -> bool:
        return self.coordinator.charge_current

    @property
    def native_max_value(self):
        return self.coordinator.max_charge_current

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_parameter(WallboxBLEApiConst.SET_MAX_CHARGING_CURRENT, int(value))
        await self.coordinator.async_refresh()


class WallboxBLEAutoLockDelay(WallboxBLEEntity, NumberEntity):
    """Auto-lock delay in minutes; the charger stores seconds.

    Setting a delay also switches auto-lock on, as in the Wallbox app. While
    auto-lock is off the charger holds no delay, so this shows unavailable.
    """

    def __init__(
        self,
        coordinator: WallboxBLEDataUpdateCoordinator,
        entity_description: NumberEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = entity_description
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{entity_description.key}"

    @property
    def available(self):
        return self.coordinator.available and bool(self.coordinator.autolock_seconds)

    @property
    def native_value(self):
        seconds = self.coordinator.autolock_seconds
        if not seconds:
            return None
        return max(AUTOLOCK_MINUTES[0], round(seconds / 60))

    async def async_set_native_value(self, value: float) -> None:
        minutes = min(max(int(value), AUTOLOCK_MINUTES[0]), AUTOLOCK_MINUTES[1])
        await self.coordinator.async_set_autolock(minutes * 60)
        self.async_write_ha_state()
