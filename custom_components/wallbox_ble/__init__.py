from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import WallboxBLEApiClient
from .const import CONF_PIN, DOMAIN, LOGGER
from .coordinator import WallboxBLEDataUpdateCoordinator
from .pairing import async_adapter_for_address, async_unpair

PLATFORMS: list[Platform] = [
    Platform.LOCK,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.SWITCH,
]


# https://developers.home-assistant.io/docs/config_entries_index/#setting-up-an-entry
async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up this integration using UI."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator = await WallboxBLEDataUpdateCoordinator.create(
        hass=hass,
        address=entry.unique_id,
        pin=entry.data.get(CONF_PIN),
    )
    # https://developers.home-assistant.io/docs/integration_fetching_data#coordinated-single-api-poll-for-data-for-all-entities
    await coordinator.async_config_entry_first_refresh()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Handle removal of an entry."""
    if unloaded := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        coordinator = hass.data[DOMAIN].pop(entry.entry_id, None)
        if coordinator is not None:
            # Must stop the BLE client task here: a reload unloads and sets up
            # again, and an orphaned run_ble_client() would keep reconnecting
            # forever alongside the new one.
            await coordinator.async_shutdown()
    return unloaded


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Drop the BLE bond when the charger is removed.

    Leaving a stale bond behind means a later re-add with a different passcode
    would be answered from BlueZ's saved keys instead of re-running SMP.
    """
    address = entry.unique_id
    if not address:
        return
    try:
        if await async_unpair(address, async_adapter_for_address(hass, address)):
            LOGGER.debug("Removed the BLE bond for %s", address)
    except Exception as err:  # noqa: BLE001 - removal must never fail the flow
        LOGGER.debug("Could not remove the BLE bond for %s: %s", address, err)
