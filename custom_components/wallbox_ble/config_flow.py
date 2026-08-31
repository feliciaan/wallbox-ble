"""Adds config flow for Wallbox BLE."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
import re

from homeassistant import config_entries
from homeassistant.components import onboarding
from homeassistant.components.bluetooth import (
    BluetoothServiceInfo,
    async_ble_device_from_address,
    async_discovered_service_info,
)
from homeassistant.const import CONF_ADDRESS

from .const import CONF_PIN, DOMAIN, LOGGER, MAX_PASSKEY
from .pairing import async_adapter_for_address, async_unpair

def _pin_schema(current: int | None = None) -> vol.Schema:
    """Schema for the passcode form, pre-filled with the stored passcode."""
    return vol.Schema(
        {vol.Optional(CONF_PIN, default="" if current is None else f"{current:06d}"): str}
    )


PIN_SCHEMA = _pin_schema()


def _parse_pin(user_input: dict[str, Any] | None) -> tuple[int | None, str | None]:
    """Turn the submitted passcode into an SMP passkey.

    Returns (passkey, error_key). An empty field is valid and means "this
    charger has no Bluetooth Passcode" -- firmware below 6.11 has none, and
    forcing one there would break a setup that works today.
    """
    raw = (user_input or {}).get(CONF_PIN, "")
    # Users copy the code out of the Wallbox app, where it is often spaced or
    # dashed for readability.
    pin = re.sub(r"[\s-]", "", str(raw))
    if not pin:
        return None, None
    if not pin.isdigit():
        return None, "invalid_pin"
    # A BLE passkey is a 6-digit number; anything longer is not a passcode the
    # SMP layer could ever carry.
    if int(pin) > MAX_PASSKEY:
        return None, "invalid_pin"
    return int(pin), None


class BlueprintFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for Blueprint."""

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovery_info: BluetoothServiceInfo | None = None
        self._discovered_devices: dict[str, BluetoothServiceInfo] = {}

    async def async_step_bluetooth(self, discovery_info: BluetoothServiceInfo):
        """Handle the bluetooth discovery step."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()

        title = discovery_info.name
        self.context["title_placeholders"] = {"name": title}

        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(self, user_input: dict[str, Any] | None = None):
        """Confirm discovery."""
        if not onboarding.async_is_onboarded(self.hass):
            # Discovery flows are auto-confirmed during onboarding, so there is
            # nobody to type a passcode. Set the charger up without one; a
            # charger that needs one triggers the reauth flow on first poll.
            return await self._async_get_or_create_entry()
        if user_input is not None:
            return await self.async_step_pin()

        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders=self.context["title_placeholders"],
        )

    async def async_step_user(
        self,
        user_input: dict | None = None,
    ):
        """Handle a flow initialized by the user."""
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            discovery = self._discovered_devices[address]

            self.context["title_placeholders"] = {"name": discovery.name}

            return await self.async_step_pin()

        current_addresses = self._async_current_ids()
        for discovery_info in async_discovered_service_info(self.hass, connectable=True):
            address = discovery_info.address
            if address in current_addresses or address in self._discovered_devices:
                continue
            if re.match(r"WB\d+", discovery_info.name):
                self._discovered_devices[address] = discovery_info

        if not self._discovered_devices:
            return self.async_abort(reason="no_devices_found")

        titles = {address: discovery.name for (address, discovery) in self._discovered_devices.items()}
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_ADDRESS): vol.In(titles)}),
        )

    async def async_step_pin(self, user_input: dict[str, Any] | None = None):
        """Ask for the charger's Bluetooth Passcode.

        Optional: chargers on firmware below 6.11 have no passcode and pair
        Just Works (or do not pair at all).
        """
        if user_input is None:
            return self.async_show_form(step_id="pin", data_schema=PIN_SCHEMA)

        pin, error = _parse_pin(user_input)
        if error:
            return self.async_show_form(
                step_id="pin", data_schema=PIN_SCHEMA, errors={CONF_PIN: error}
            )

        return await self._async_get_or_create_entry(pin)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]):
        """Handle a passcode the charger rejected."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None):
        """Ask for a corrected Bluetooth Passcode."""
        return await self._async_update_pin("reauth_confirm", user_input, "reauth_successful")

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None):
        """Let the user change the Bluetooth Passcode of a working entry."""
        return await self._async_update_pin("reconfigure", user_input, "reconfigure_successful")

    async def _async_update_pin(self, step_id, user_input, abort_reason):
        """Shared body of the reauth and reconfigure steps."""
        entry_id = self.context.get("entry_id")
        entry = (
            self.hass.config_entries.async_get_entry(entry_id) if entry_id else None
        )
        if entry is None:
            return self.async_abort(reason="unknown_entry")

        schema = _pin_schema(entry.data.get(CONF_PIN))
        if user_input is None:
            return self.async_show_form(
                step_id=step_id,
                data_schema=schema,
                description_placeholders={"name": entry.title},
            )

        pin, error = _parse_pin(user_input)
        if error:
            return self.async_show_form(
                step_id=step_id,
                data_schema=schema,
                description_placeholders={"name": entry.title},
                errors={CONF_PIN: error},
            )

        if pin != entry.data.get(CONF_PIN):
            # BlueZ answers a Pair() for an already-bonded device from its saved
            # keys without re-running SMP, so the new passcode would never be
            # offered. Drop the bond first.
            await self._async_drop_bond(entry.unique_id)

        self.hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_PIN: pin}
        )
        await self.hass.config_entries.async_reload(entry.entry_id)
        return self.async_abort(reason=abort_reason)

    async def _async_drop_bond(self, address):
        """Best-effort removal of an existing BlueZ bond."""
        if not address:
            return
        try:
            await async_unpair(address, async_adapter_for_address(self.hass, address))
        except Exception as err:  # noqa: BLE001 - never block the flow on this
            LOGGER.debug("Could not drop the BLE bond for %s: %s", address, err)

    async def _async_get_or_create_entry(self, pin=None):
        device = async_ble_device_from_address(self.hass, self.unique_id, connectable=True)
        title = device.name if device and device.name else self.unique_id
        return self.async_create_entry(title=title, data={CONF_PIN: pin})
