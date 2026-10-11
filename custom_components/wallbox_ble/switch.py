from __future__ import annotations

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory

from .api import WallboxBLEApiConst
from .const import DOMAIN, LOGGER
from .coordinator import WallboxBLEDataUpdateCoordinator
from .entity import WallboxBLEEntity

ENTITY_DESCRIPTIONS = (
    SwitchEntityDescription(
        key="wallbox_ble",
        name="Charge",
    ),
)


AUTOLOCK = SwitchEntityDescription(
    key="autolock",
    name="Auto-lock",
    icon="mdi:lock-clock",
    entity_category=EntityCategory.CONFIG,
)

# Delay to restore when auto-lock is switched on without a known previous one.
DEFAULT_AUTOLOCK_S = 60


async def async_setup_entry(hass, entry, async_add_devices):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_devices(
        [
            *(
                WallboxBLESwitch(
                    coordinator=coordinator,
                    entity_description=entity_description,
                )
                for entity_description in ENTITY_DESCRIPTIONS
            ),
            WallboxBLEAutoLockSwitch(coordinator, AUTOLOCK),
        ]
    )


class WallboxBLESwitch(WallboxBLEEntity, SwitchEntity):
    def __init__(
        self,
        coordinator: WallboxBLEDataUpdateCoordinator,
        entity_description: SwitchEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = entity_description

    @property
    def available(self):
        return self.coordinator.available and self.coordinator.status_code in (1, 4)

    @property
    def is_on(self) -> bool:
        return self.coordinator.status_code == 1

    async def async_turn_on(self, **_: any) -> None:
        await self.coordinator.async_set_parameter(WallboxBLEApiConst.START_STOP_CHARGING, 1)
        await self.coordinator.async_refresh()

    async def async_turn_off(self, **_: any) -> None:
        await self.coordinator.async_set_parameter(WallboxBLEApiConst.START_STOP_CHARGING, 0)
        await self.coordinator.async_refresh()


class WallboxBLEAutoLockSwitch(WallboxBLEEntity, SwitchEntity):
    """Auto-lock after unplugging. The delay lives on the Auto-lock delay number.

    The charger keeps a single value (seconds, 0 = off), so switching off loses
    the delay. Remember it here so switching back on restores it.
    """

    def __init__(
        self,
        coordinator: WallboxBLEDataUpdateCoordinator,
        entity_description: SwitchEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = entity_description
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{entity_description.key}"
        self._last_on_seconds = DEFAULT_AUTOLOCK_S

    @property
    def available(self):
        return self.coordinator.available and self.coordinator.autolock_seconds is not None

    @property
    def is_on(self) -> bool:
        seconds = self.coordinator.autolock_seconds or 0
        if seconds > 0:
            self._last_on_seconds = seconds
        return seconds > 0

    async def async_turn_on(self, **_: any) -> None:
        await self.coordinator.async_set_autolock(self._last_on_seconds)
        self.async_write_ha_state()

    async def async_turn_off(self, **_: any) -> None:
        await self.coordinator.async_set_autolock(0)
        self.async_write_ha_state()
