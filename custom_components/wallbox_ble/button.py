from __future__ import annotations

from homeassistant.components.button import (
    ButtonDeviceClass,
    ButtonEntity,
    ButtonEntityDescription,
)
from homeassistant.const import EntityCategory

from .const import DOMAIN
from .coordinator import WallboxBLEDataUpdateCoordinator
from .entity import WallboxBLEEntity

REBOOT = ButtonEntityDescription(
    key="reboot",
    name="Reboot charger",
    device_class=ButtonDeviceClass.RESTART,
    entity_category=EntityCategory.CONFIG,
)


async def async_setup_entry(hass, entry, async_add_devices):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_devices([WallboxBLEReboot(coordinator, REBOOT)])


class WallboxBLEReboot(WallboxBLEEntity, ButtonEntity):
    def __init__(
        self,
        coordinator: WallboxBLEDataUpdateCoordinator,
        entity_description: ButtonEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = entity_description
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{entity_description.key}"

    @property
    def available(self):
        return self.coordinator.available

    async def async_press(self) -> None:
        # The charger drops the BLE link as it restarts; the client reconnects
        # on its own once it is back.
        await self.coordinator.async_reboot()
