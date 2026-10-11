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

from . import charger
from .api import WallboxBLEApiClient, WallboxBLEApiConst
from .const import DOMAIN, LOGGER
from .meter import ERROR_NOT_SUPPORTED, house_current, parse_meter

# Variant B safety net: if the BLE link stays dead even after the client's own
# reconnect attempts (variant A in api.py), reload the whole config entry once.
# Longer than STALE_RECONNECT_S so a normal reconnect never trips it.
SELF_RELOAD_STALE_S = 180

# Settings and slow-moving counters (auto-lock, Halo, notifications, sessions,
# Power Boost limit) are read this often rather than every 10 s poll, to keep
# traffic over the Bluetooth proxy down. A write refreshes them immediately.
SLOW_POLL_S = 300

# An optional read the charger has never answered, left unanswered this many
# polls in a row while the status read in the same poll was answered, is taken
# as unsupported and not asked for again until the entry reloads. Each
# unanswered read costs the full response timeout on every poll otherwise. A
# read that has answered before is supported: its misses are the link dropping
# mid-request, so it is always asked again.
MAX_UNANSWERED = 3

# Polls in a row without a status reply before the entities go unavailable.
# Through a Bluetooth proxy one reply in a while simply never arrives; that
# alone should not blank the whole device until the next poll (which may be a
# slow one) succeeds. The last values are kept meanwhile.
MAX_STATUS_MISSES = 3


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
        self._status_misses = 0
        self._last_success_mono = None
        self._reload_scheduled = False
        # Power Boost meter: the latest decoded reading, and whether the
        # charger has one at all (None until it has answered once).
        self.meter = None
        self.meter_supported = None
        # Supply voltage, best source first: the Power Boost meter when it has
        # a voltage reference; else the charger's power / current while it
        # charges (it reports no voltage itself); else the last value seen;
        # else nominal. voltage_source says which.
        self.voltage = charger.NOMINAL_VOLTAGE
        self.voltage_source = "nominal"
        # r_sta: lock state, max available current, lifetime energy, ...
        self.realtime = {}
        # Read every SLOW_POLL_S.
        self.autolock_seconds = None
        self.halo_config = None
        self.notification_count = None
        self.latest_notification = None
        self.session_count = None
        self.power_boost_limit = None
        # Read once per connection.
        self.firmware = None
        self.serial_number = None
        self.grounding = None
        self._info_read = False
        self._next_slow_poll = 0.0
        # Optional reads: consecutive unanswered polls, and those given up on.
        self._unanswered = {}
        self._skipped = set()
        self._answered = set()

    @classmethod
    async def create(cls, hass, address, pin=None):
        self = WallboxBLEDataUpdateCoordinator(hass)
        self.wb = await WallboxBLEApiClient.create(hass, address, pin)
        return self

    async def async_shutdown(self):
        """Stop polling and tear down the BLE client task with the entry."""
        await super().async_shutdown()
        await self.wb.async_shutdown()

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
            return self._status_missed()

        if self.max_charge_current == 0:
            ok, data = await self.wb.async_get_max_charge_current()
            if ok:
                self.max_charge_current = data
                LOGGER.debug(f"SET {self.max_charge_current=}")

        ok, data = await self.wb.async_get_data()
        if ok and isinstance(data, dict):
            LOGGER.debug("Update done")
            self._last_success_mono = time.monotonic()
            self._reload_scheduled = False
            self.status_code = data.get("st", 0)
            self.charge_current = data.get("cur", 6)
            self.status = self._status_name(self.status_code)
            self.available = True
            self._status_misses = 0
            # Completes the session login if setup's own attempt missed it.
            await self.wb.async_login(data)
            await self._async_update_realtime()
            await self._async_update_meter()
            self._update_voltage(data)
            await self._async_update_slow()
            return data
        return self._status_missed()

    def _status_missed(self):
        """A poll without a status reply: keep the last values for a while."""
        self._status_misses += 1
        if self._status_misses >= MAX_STATUS_MISSES:
            self.available = False
        self._check_self_heal()
        return self.data

    @staticmethod
    def _status_name(code):
        """Name for an r_dat.st code; never raises on a code we do not know."""
        codes = WallboxBLEApiConst.STATUS_CODES
        if isinstance(code, int) and 0 <= code < len(codes):
            return codes[code]
        return f"UNKNOWN_{code}"

    def _update_voltage(self, status):
        """Pick the best voltage available this poll (see __init__)."""
        if self.meter is not None and self.meter.measured_voltage is not None:
            self.voltage, self.voltage_source = self.meter.measured_voltage, "meter"
            return
        voltage = charger.implied_voltage(status)
        if voltage is not None:
            self.voltage, self.voltage_source = voltage, "charger"
        # Otherwise keep the last value and its source.

    async def _read_optional(self, method):
        """async_read for anything but the core status read.

        Only called after r_dat has answered in the same poll. A method that
        has never answered and keeps timing out is dropped (MAX_UNANSWERED);
        other failures (write rejected, link lost) are not counted.
        """
        if method in self._skipped:
            return False, None, None, None
        ok, result, error, response = await self.wb.async_read(method)
        if ok:
            self._answered.add(method)
            self._unanswered.pop(method, None)
        elif self.wb.last_failure == "timeout" and method not in self._answered:
            misses = self._unanswered.get(method, 0) + 1
            self._unanswered[method] = misses
            if misses >= MAX_UNANSWERED:
                self._skipped.add(method)
                LOGGER.warning(
                    "Charger has not answered %s in %d polls while otherwise "
                    "responding; no longer asking for it until the integration "
                    "reloads", method, misses,
                )
        return ok, result, error, response

    async def _async_update_realtime(self):
        """r_sta: the charger's own lock state and a few live counters."""
        ok, result, _error, _ = await self._read_optional(WallboxBLEApiConst.GET_REALTIME)
        if ok and isinstance(result, dict):
            self.realtime = result
        # r_sta's lock_status is the charger's actual lock; st == 6 only says
        # "locked" when idle, so use it only when r_sta has nothing.
        lock = self.realtime.get("lock_status")
        self.locked = bool(lock) if lock is not None else self.status_code == 6

    async def _async_update_slow(self, force=False):
        """Settings and counters, every SLOW_POLL_S; identity once."""
        if not self._info_read:
            await self._async_read_info()
        now = time.monotonic()
        if not force and now < self._next_slow_poll:
            return
        self._next_slow_poll = now + SLOW_POLL_S
        read = self._read_optional
        const = WallboxBLEApiConst

        ok, result, _e, _ = await read(const.GET_AUTOLOCK)
        if ok:
            self.autolock_seconds = charger.autolock_seconds(result)
        ok, result, _e, _ = await read(const.GET_HALO_CONFIG)
        if ok and isinstance(result, dict):
            self.halo_config = result
        ok, result, _e, _ = await read(const.GET_NOTIFICATIONS)
        if ok:
            self.notification_count, self.latest_notification = charger.notifications(result)
        ok, result, _e, _ = await read(const.GET_SESSIONS_INFO)
        if ok:
            self.session_count = charger.session_count(result)
        ok, result, _e, _ = await read(const.GET_POWER_BOOST)
        if ok and isinstance(result, (int, float)) and not isinstance(result, bool):
            self.power_boost_limit = result
        LOGGER.debug(
            "Slow poll: autolock=%ss halo=%s notifications=%s sessions=%s power_boost=%s",
            self.autolock_seconds, self.halo_config, self.notification_count,
            self.session_count, self.power_boost_limit,
        )

    async def _async_read_info(self):
        """Firmware, serial and grounding: they do not change while connected."""
        read = self._read_optional
        const = WallboxBLEApiConst
        answered = False
        ok, _r, _e, response = await read(const.GET_CHARGER_VERSIONS)
        if ok:
            answered = True
            self.firmware = charger.firmware(response)
        ok, result, _e, _ = await read(const.GET_SERIAL_NUMBER)
        if ok:
            answered = True
            self.serial_number = charger.serial_number(result)
        ok, result, _e, _ = await read(const.GET_GROUNDING_STATUS)
        if ok:
            answered = True
            self.grounding = charger.grounding(result)
        # Retry next poll if the link dropped before any of it arrived.
        self._info_read = answered
        LOGGER.debug("Charger info: firmware=%s serial=%s grounding=%s",
                     self.firmware, self.serial_number, self.grounding)

    # --- derived figures ------------------------------------------------------
    # Measured power wherever an instrument reports it: the charger's own
    # power reading, and the meter's when it has a voltage reference. Only
    # what is not measured is worked out as current x self.voltage. House is
    # grid minus charger, so grid = house + charger unless house is clamped
    # at 0.

    @property
    def charger_current(self) -> float:
        return charger.charger_current(self.data)

    @property
    def charger_power(self) -> float:
        measured = charger.charging_power(self.data)
        return measured if measured is not None else self.charger_current * self.voltage

    @property
    def grid_power(self) -> float | None:
        if self.meter is None:
            return None
        measured = self.meter.measured_power
        return measured if measured is not None else self.meter.grid_current * self.voltage

    @property
    def house_current(self) -> float | None:
        return house_current(self.meter, self.charger_current)

    @property
    def house_power(self) -> float | None:
        grid = self.grid_power
        if grid is None:
            return None
        # Never below zero, for the same reason as house_current.
        return max(0.0, grid - self.charger_power)

    @property
    def car_connected(self):
        if not self.available:
            return None
        return charger.car_connected(self.status_code, self.realtime)

    # --- writes ---------------------------------------------------------------

    async def async_set_autolock(self, seconds: int) -> bool:
        """s_alo takes a bare number of seconds; 0 turns auto-lock off."""
        ok = await self.async_set_parameter(WallboxBLEApiConst.SET_AUTOLOCK, int(seconds))
        if ok:
            self.autolock_seconds = int(seconds)
            await self._async_update_slow(force=True)
        return ok

    async def async_set_halo(self, level: str) -> bool:
        payload = charger.halo_payload(level, self.halo_config)
        ok = await self.async_set_parameter(WallboxBLEApiConst.SET_HALO_CONFIG, payload)
        if ok:
            self.halo_config = {**(self.halo_config or {}), **payload}
            await self._async_update_slow(force=True)
        return ok

    async def async_reboot(self) -> bool:
        return await self.async_set_parameter(WallboxBLEApiConst.REBOOT, None)

    async def _async_update_meter(self):
        """Poll the grid meter, unless the charger has said it has none."""
        if self.meter_supported is False:
            return
        ok, result, error, _ = await self._read_optional(
            WallboxBLEApiConst.GET_POWER_BOOST_STATUS
        )
        if not ok:
            # No answer this time; keep the last reading rather than flapping
            # the meter sensors to unavailable on one dropped frame.
            return
        if isinstance(error, dict) and error.get("code") == ERROR_NOT_SUPPORTED:
            # Only a definite "not supported" switches polling off, so a
            # transient error does not lose the meter until the next reload.
            LOGGER.info(
                "Charger reports no Power Boost meter (r_dca error %s); "
                "grid and house power sensors will stay unavailable",
                ERROR_NOT_SUPPORTED,
            )
            self.meter_supported = False
            self.meter = None
            return
        reading = parse_meter(result)
        LOGGER.debug("Meter (r_dca): raw=%s decoded=%s", result, reading)
        if reading is not None:
            self.meter_supported = True
            self.meter = reading

    async def async_set_parameter(self, parameter, value):
        ok, _ = await self.wb.request(parameter, value)
        return ok
