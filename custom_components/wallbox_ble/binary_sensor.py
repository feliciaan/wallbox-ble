from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)

from .const import DOMAIN
from .coordinator import WallboxBLEDataUpdateCoordinator
from .entity import WallboxBLEEntity

CAR_CONNECTED = BinarySensorEntityDescription(
    key="car_connected",
    name="Car connected",
    device_class=BinarySensorDeviceClass.PLUG,
)


async def async_setup_entry(hass, entry, async_add_devices):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_devices([WallboxBLECarConnected(coordinator, CAR_CONNECTED)])


class WallboxBLECarConnected(WallboxBLEEntity, BinarySensorEntity):
    """Plugged in, from the status code (charger.car_connected)."""

    def __init__(
        self,
        coordinator: WallboxBLEDataUpdateCoordinator,
        entity_description: BinarySensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = entity_description
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{entity_description.key}"

    @property
    def available(self):
        return self.coordinator.available

    @property
    def is_on(self):
        return self.coordinator.car_connected
