from __future__ import annotations

import time
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.exceptions import ConfigEntryAuthFailed

from .api import WallboxBLEApiClient, WallboxBLEApiConst
from .const import DOMAIN, LOGGER

# Variant B safety net: if the BLE link stays dead even after the client's own
# reconnect attempts (variant A in api.py), reload the whole config entry once.
# Longer than STALE_RECONNECT_S so a normal reconnect never trips it.
SELF_RELOAD_STALE_S = 180


class WallboxBLEDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the API."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
    ) -> None:
        """Initialize."""
        super().__init__(
            hass=hass,
            logger=LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=10),
        )
        self.hass = hass
        self.locked = False
        self.charge_current = 0
        self.max_charge_current = 0
        self.status = ""
        self.status_code = 0
        self.available = False
        self._last_success_mono = None
        self._reload_scheduled = False

    @classmethod
    async def create(cls, hass, address, pin=None):
        self = WallboxBLEDataUpdateCoordinator(hass)
        self.wb = await WallboxBLEApiClient.create(hass, address, pin)
        return self

    async def async_refresh_later(self, delay):
        async def wrap(*_):
            await self.async_refresh()

        async_call_later(self.hass, delay, wrap)

    def _check_self_heal(self):
        """Reload the config entry if the link has been dead too long (variant B)."""
        now = time.monotonic()
        if self._last_success_mono is None:
            self._last_success_mono = now
            return
        if (now - self._last_success_mono > SELF_RELOAD_STALE_S
                and not self._reload_scheduled):
            self._reload_scheduled = True
            LOGGER.warning(
                "BLE unrecoverable for >%ss; reloading config entry",
                SELF_RELOAD_STALE_S,
            )
            self.hass.async_create_task(
                self.hass.config_entries.async_reload(self.config_entry.entry_id)
            )

    async def _async_update_data(self):
        if self.wb.pairing_auth_failed:
            # The charger rejected the configured Bluetooth Passcode. Retrying
            # cannot help, so ask the user for a new one instead of looping.
            raise ConfigEntryAuthFailed(
                "The charger rejected the configured Bluetooth Passcode"
            )

        if not self.wb.ready:
            self._check_self_heal()
            return {}

        if self.max_charge_current == 0:
            ok, data = await self.wb.async_get_max_charge_current()
            if ok:
                self.max_charge_current = data
                LOGGER.debug(f"SET {self.max_charge_current=}")

        ok, data = await self.wb.async_get_data()
        if ok:
            LOGGER.debug("Update done")
            self._last_success_mono = time.monotonic()
            self._reload_scheduled = False
            self.status_code = data.get("st", 0)
            self.locked = self.status_code == 6
            self.charge_current = data.get("cur", 6)
            self.status = WallboxBLEApiConst.STATUS_CODES[self.status_code]
            self.available = True
            return data
        else:
            self.available = False
            self._check_self_heal()

    async def async_set_parameter(self, parameter, value):
        ok, _ = await self.wb.request(parameter, value)
        return ok
