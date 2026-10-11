from __future__ import annotations

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory

from .charger import HALO_LEVELS, halo_level
from .const import DOMAIN
from .coordinator import WallboxBLEDataUpdateCoordinator
from .entity import WallboxBLEEntity

HALO = SelectEntityDescription(
    key="halo",
    name="Halo LED",
    icon="mdi:led-on",
    options=list(HALO_LEVELS),
    entity_category=EntityCategory.CONFIG,
)


async def async_setup_entry(hass, entry, async_add_devices):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_devices([WallboxBLEHalo(coordinator, HALO)])


class WallboxBLEHalo(WallboxBLEEntity, SelectEntity):
    """Halo LED brightness, bucketed into the app's four levels."""

    def __init__(
        self,
        coordinator: WallboxBLEDataUpdateCoordinator,
        entity_description: SelectEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = entity_description
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{entity_description.key}"

    @property
    def available(self):
        return self.coordinator.available and self.coordinator.halo_config is not None

    @property
    def current_option(self):
        return halo_level(self.coordinator.halo_config)

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_halo(option)
        self.async_write_ha_state()
