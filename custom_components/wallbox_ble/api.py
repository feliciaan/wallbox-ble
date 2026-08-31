"""Wallbox BLE API Client."""
from __future__ import annotations

import random
import asyncio
import json
import contextlib

from bleak import BleakClient
from bleak_retry_connector import establish_connection

from homeassistant.components.bluetooth import async_ble_device_from_address

from .const import LOGGER
from .pairing import (
    PairingAuthError,
    PairingUnsupported,
    async_adapter_for_address,
    async_ensure_paired as async_bluez_ensure_paired,
)

# BLE link self-heal (variant A): while "connected", wake every
# HEALTH_CHECK_INTERVAL to check liveness, and force a fresh reconnect if no
# successful read has landed for STALE_RECONNECT_S. Needed because a silent link
# death may never trigger bleak's disconnected_callback, leaving run_ble_client
# blocked forever. The coordinator polls every 10 s, so 60 s ~= 6 missed reads.
HEALTH_CHECK_INTERVAL = 15
STALE_RECONNECT_S = 60

# Per-write BLE timeout. A BlueZ/dbus write_gatt_char can wedge in an
# UNCANCELLABLE state: the ATT write already reached the charger (the command
# takes effect) but the dbus reply is lost, so the coroutine never returns.
# asyncio.wait_for() does NOT save us here — on timeout it cancels the write and
# then AWAITS the cancellation, which for a dbus-wedged call never lands, so
# wait_for() itself hangs forever. That pinned an HA service call / automation
# task at current=1 for ~10 h (see write-up 2026-08-01). We therefore race the
# write against this timeout and ABANDON the orphaned task WITHOUT awaiting it,
# so control always returns to the caller after WRITE_TIMEOUT_S.
WRITE_TIMEOUT_S = 2


def _drain_abandoned(task):
    """Retrieve a finished (abandoned) write task's result/exception so asyncio
    does not log 'Task exception was never retrieved'. The task may complete much
    later, when the wedged dbus call finally unblocks or the link is dropped."""
    if not task.cancelled():
        with contextlib.suppress(Exception):
            task.exception()


class WallboxBLEApiConst:
    # Default (Pulsar Plus / "BgExpress" radio). Overridden at runtime once we
    # detect which BLE profile the connected charger actually exposes.
    UART_SERVICE_UUID = "331a36f5-2459-45ea-9d95-6142f0c4b307"
    UART_RX_CHAR_UUID = "a9da6040-0823-4995-94ec-9ce41ca28833"
    UART_TX_CHAR_UUID = "a73e9a10-628f-4494-a099-12efaf72258f"

    # Wallbox ships several BLE radio modules across hardware revisions, each
    # with its own GATT UUIDs but the SAME EaE+JSON application protocol (taken
    # from the official Android app, enum sr.b). "stream_mode" is the byte to
    # write to the mode characteristic to put the module into raw-stream mode so
    # writes to the RX characteristic are forwarded to the charger MCU (Zentri
    # modules need this; the Pulsar Plus does not).
    BLE_PROFILES = [
        {
            "name": "zentri",
            "service": "175f8f23-a570-49bd-9627-815a6a27de2a",
            "rx": "1cce1ea8-bd34-4813-a00a-c76e028fadcb",
            "tx": "cacc07ff-ffff-4c48-8fae-a9ef71b75e26",
            "mode": "20b9794f-da1a-4d14-8014-a0fb9cefb2f7",
            "stream_mode": 0x01,
        },
        {
            "name": "bgexpress",
            "service": "331a36f5-2459-45ea-9d95-6142f0c4b307",
            "rx": "a9da6040-0823-4995-94ec-9ce41ca28833",
            "tx": "a73e9a10-628f-4494-a099-12efaf72258f",
            "mode": "75a9f022-af03-4e41-b4bc-9de90a47d50b",
            "stream_mode": None,
        },
        {
            # Pulsar Max ("u-blox" radio). Single characteristic used for BOTH
            # writes and notifications (rx == tx), no mode/stream characteristic.
            # UUIDs from the botts7/esp32-wallbox reference gateway, verified
            # against a real Max on fw 6.11.16. Firmware >= 6.11 additionally
            # requires an encrypted BLE link (SMP pairing with the charger's
            # "Bluetooth Passcode" as the passkey) before the notification CCCD
            # write is accepted -- see pairing.py and CONF_PIN.
            "name": "pulsar_max",
            "service": "2456e1b9-26e2-8f83-e744-f34f01e9d701",
            "rx": "2456e1b9-26e2-8f83-e744-f34f01e9d703",
            "tx": "2456e1b9-26e2-8f83-e744-f34f01e9d703",
            "mode": None,
            "stream_mode": None,
        },
    ]

    GET_AUTOLOCK = "g_alo"
    GET_BATTERY_CONFIG = "r_socr"
    GET_CHARGER_VERSIONS = "fw_v_"
    GET_DISCHARGE_SESSION = "r_dis"
    GET_DYNAMIC_GRID_CODE = "ggcds"
    GET_DYNAMIC_GRID_CODE_REGULATIONS = "r_gcdl"
    GET_DYNAMIC_GRID_CODE_FEATURES = "r_gcdf"
    GET_DYNAMIC_GRID_CODE_LOGS = "r_gcli"
    GET_DYNAMIC_GRID_CODE_LOGS_DETAIL = "r_gcld"
    GET_DYNAMIC_GRID_CODE_LOGS_SIZE = "r_gcls"
    GET_DYNAMIC_GRID_CODE_ALERT = "r_gcai"
    GET_DYNAMIC_GRID_CODE_ALERT_SIZE = "r_gcas"
    GET_ECO_SMART_CONFIGURATION = "g_ecos"
    GET_GESTURE_CONFIGURATION = "ggsta"
    GET_GRID_CODE = "r_gcd"
    GET_HALO_CONFIG = "g_halocfg"
    GET_HOTSPOT_UPDATE_STATUS = "r_hup"
    GET_IP_MODE = "gimod"
    GET_LOCK_STATUS = "r_lck"
    GET_MAC_ADDRESSES = "g_mac"
    GET_MAX_AVAILABLE_CURRENT = "r_fsI"
    GET_MID_CONFIGURATION = "g_mid"
    GET_MOBILE_CONNECTIVITY = "gmcon"
    GET_NETWORKS_STATUS = "gnsta"
    GET_OCPP = "g_ocpp"
    GET_POWER_BOOST = "r_hsh"
    GET_POWER_BOOST_STATUS = "r_dca"
    GET_POWER_INFUSION = "g_pwi"
    GET_POWER_SHARING = "g_psh"
    GET_PIN = "read_pin"
    GET_PROXY_MODE = "gpmod"
    GET_SCHEDULE = "r_sch"
    GET_SERIAL_NUMBER = "r_sn_"
    GET_SESSIONS_INFO = "r_ses"
    GET_SESSION = "r_log"
    GET_STATUS = "r_dat"
    GET_TIMEZONE = "g_tzn"
    GET_GROUNDING_STATUS = "r_wel"
    GET_WIFI_NETWORKS = "gwnet"
    GET_WIFI_STATUS = "gwsta"
    LOCK = "w_lck"
    REBOOT = "rebot"
    SET_AUTOLOCK = "s_alo"
    SET_BATTERY_CONFIG = "w_socr"
    SEND_TRANSACTION_DATA = "w_td"
    SET_DATA_TRANSACTION_STATUS = "s_dts"
    SET_DYNAMIC_GRID_CODE = "sgcds"
    SET_DYNAMIC_GRID_CODE_REGULATION = "w_gcdr"
    SET_DYNAMIC_GRID_CODE_FEATURE = "w_gcdf"
    SET_ECO_SMART_CONFIGURATION = "s_ecos"
    SET_GESTURE_CONFIGURATION = "sgsta"
    SET_GRID_CODE = "w_gcd"
    SET_HALO_CONFIG = "s_halocfg"
    SET_HOTSPOT_UPDATE = "s_hup"
    SET_HOTSPOT_UPDATE_INFO = "s_deb"
    SET_IP_MODE = "simod"
    SET_MAX_CHARGING_CURRENT = "w_mxI"
    SET_MID_CONFIGURATION = "s_mid"
    SET_MOBILE_CONNECTIVITY = "smcon"
    SET_MOBILE_CONNECTIVITY_STATUS = "smcen"
    SET_MULTIUSER = "s_mus"
    SET_OCPP = "s_ocpp"
    SET_POWER_BOOST = "w_hsh"
    SET_POWER_INFUSION = "s_pwi"
    SET_POWER_SHARING = "s_psh"
    SET_PROXY_MODE = "spmod"
    SET_SCHEDULE = "w_sch"
    SET_TIME = "Wtime"
    SET_TIMEZONE = "s_tzn"
    SET_USER = "suser"
    SET_USER_LIST = "sulis"
    SET_GROUNDING_STATUS = "w_wel"
    SET_WIFI = "swcon"
    SET_WIFI_STATUS = "swsta"
    SOFTWARE_CHECK = "gupdc"
    START_STOP_CHARGING = "w_cha"
    UNLOCK_MOBILE_SIM = "smpuk"
    UPDATE_SOFTWARE_PROGRESS = "supdp"
    UPDATE_SOFTWARE = "supds"

    STATUS_CODES = [
        "READY",  # 0
        "CHARGING",  # 1
        "CONNECTED_WAITING_CAR",  # 2
        "CONNECTED_WAITING_SCHEDULE",  # 3
        "PAUSED",  # 4
        "SCHEDULE_END",  # 5
        "LOCKED",  # 6
        "ERROR",  # 7
        "CONNECTED_WAITING_CURRENT_ASSIGNATION",  # 8
        "UNCONFIGURED_POWER_SHARING",  # 9
        "QUEUE_BY_POWER_BOOST",  # 10
        "DISCHARGING",  # 11
        "CONNECTED_WAITING_ADMIN_AUTH_FOR_MID",  # 12
        "CONNECTED_MID_SAFETY_MARGIN_EXCEEDED",  # 13
        "OCPP_UNAVAILABLE",  # 14
        "OCPP_CHARGE_FINISHING",  # 15
        "OCPP_RESERVED",  # 16
        "UPDATING",  # 17
        "QUEUE_BY_ECO_SMART",  # 18
    ]


class WallboxBLEApiClient:

    def detect_profile(self):
        """Pick the BLE profile (UUIDs) matching the services this charger exposes."""
        for profile in WallboxBLEApiConst.BLE_PROFILES:
            if self.client.services.get_service(profile["service"]) is not None:
                self.service_uuid = profile["service"]
                self.rx_uuid = profile["rx"]
                self.tx_uuid = profile["tx"]
                self.mode_uuid = profile["mode"]
                self.stream_mode = profile["stream_mode"]
                LOGGER.debug(f"Detected BLE profile '{profile['name']}' for {self.address}")
                return
        LOGGER.debug(f"No known BLE profile matched; using default UUIDs for {self.address}")

    async def async_ensure_paired(self, allow_just_works=False):
        """Bond with the charger over SMP so the encrypted link is up.

        Firmware >= 6.11 rejects the notification CCCD write (and the BGX
        stream-mode write) until the link is encrypted, which needs the
        charger's "Bluetooth Passcode" as the SMP passkey. See pairing.py for
        why bleak cannot do this on its own.

        Returns True when the charger is bonded afterwards. Never raises: a
        charger that does not need pairing must keep working unchanged, so a
        failure here only downgrades to an unencrypted link and lets the
        subsequent GATT operation decide whether that is fatal.
        """
        if self.pin is None and not allow_just_works:
            return False

        adapter = async_adapter_for_address(self.hass, self.address)
        try:
            await async_bluez_ensure_paired(self.address, self.pin, adapter)
        except PairingUnsupported as e:
            # Reached through an ESPHome Bluetooth proxy, or no BlueZ at all.
            # Log once -- repeating it every reconnect buries the real error.
            if not self._pairing_unsupported_logged:
                self._pairing_unsupported_logged = True
                LOGGER.warning("Cannot pair with %s: %s", self.address, e)
            return False
        except PairingAuthError as e:
            # A wrong passcode never fixes itself; flag it so the coordinator
            # can trigger a reauth flow rather than reconnect forever.
            self.pairing_auth_failed = True
            LOGGER.error("Bluetooth Passcode rejected by %s: %s", self.address, e)
            return False
        except Exception as e:
            LOGGER.debug("Pairing with %s failed: %s", self.address, e)
            return False

        self.pairing_auth_failed = False
        return True

    async def authenticate(self):
        """Replicate the app's session login.

        The charger expects a SET_USER ("suser") command carrying its own user
        id before it accepts control commands. We read that id back from the
        status response (r_dat -> "usid"). No PIN/bonding is involved.
        """
        try:
            ok, data = await self.async_get_data()
            if ok and isinstance(data, dict) and data.get("usid") is not None:
                usid = data["usid"]
                ok2, _ = await self.request(WallboxBLEApiConst.SET_USER, usid)
                LOGGER.debug(f"Authenticated session with usid={usid} ok={ok2}")
            else:
                LOGGER.debug("No usid in status; skipping session auth")
        except Exception as e:
            LOGGER.debug(f"Authenticate failed: {e}")

    async def run_ble_client(self):
        async def callback_handler(sender, data):
            await self.rx_queue.put(data)

        disconnected_event = self._disconnected_event

        def disconnected_callback(client):
            LOGGER.debug("Disconnected!")
            disconnected_event.set()

        while True:
            LOGGER.debug("Connecting...")
            # Reset any pending reconnect signal (from a prior disconnect or an
            # abandoned write) BEFORE we connect, so the fresh link is not torn
            # down immediately.
            disconnected_event.clear()

            try:
                device = async_ble_device_from_address(self.hass, self.address, connectable=True)
                if not device:
                    raise Exception("No device found")
                # Bond BEFORE connecting when a passcode is configured, so the
                # link is encrypted from the first ATT operation. BlueZ brings
                # the connection up itself as part of Pair(); establish_connection
                # then reuses it. No-op once the bond is stored.
                await self.async_ensure_paired()
                # Use bleak_retry_connector so the connection is established
                # reliably AND all GATT services are fully resolved before we
                # try to use the UART characteristics (otherwise start_notify
                # fails with CharacteristicNotFound on a freshly connected link).
                self.client = await establish_connection(
                    BleakClient,
                    device,
                    self.address,
                    disconnected_callback=disconnected_callback,
                )
                LOGGER.debug("Connected!")
                # Detect which BLE radio profile this charger exposes and use
                # its UUIDs for the rest of the session.
                self.detect_profile()
                # IMPORTANT: replicate the exact order the official app uses, as
                # captured from a BLE HCI snoop. The charger does NOT use BLE
                # bonding/pairing; instead the command characteristic only
                # accepts writes once (1) notifications are enabled on the TX
                # characteristic and (2) the module has been switched to STREAM
                # mode. Doing these in the wrong order yields Write Not Permitted.
                # 1) enable notifications first. A rejected CCCD write means
                # the charger wants an encrypted link (firmware >= 6.11), so
                # pair and retry once -- allowing Just Works here too, since a
                # charger without a passcode can still demand encryption.
                try:
                    await self.client.start_notify(self.tx_uuid, callback_handler)
                except Exception as e:
                    LOGGER.debug(f"start_notify failed ({e}); pairing and retrying")
                    if not await self.async_ensure_paired(allow_just_works=True):
                        raise
                    await self.client.start_notify(self.tx_uuid, callback_handler)
                # 2) then switch the (Zentri) radio into raw STREAM mode
                # (abandon-safe: a wedged setup write must not pin the client task)
                if self.stream_mode is not None and self.mode_uuid:
                    if await self._write_abandonable(self.mode_uuid, bytes([self.stream_mode])):
                        LOGGER.debug(f"Set stream mode {self.stream_mode} on {self.mode_uuid}")
                    else:
                        LOGGER.debug("Failed/timed out setting stream mode")
                # 3) authenticate the session (charger expects "suser" with its
                # own user id, which we read back from r_dat) so that control
                # commands (lock, charge current, ...) are accepted.
                await self.authenticate()
                # Mark the link healthy on (re)connect.
                self.last_success = asyncio.get_running_loop().time()
                # Variant A: do NOT block forever on disconnected_callback (it may
                # never fire on a silent link death). Wake periodically and force a
                # reconnect if no successful read landed for STALE_RECONNECT_S.
                while not disconnected_event.is_set():
                    try:
                        await asyncio.wait_for(
                            disconnected_event.wait(), timeout=HEALTH_CHECK_INTERVAL
                        )
                    except asyncio.TimeoutError:
                        idle = asyncio.get_running_loop().time() - self.last_success
                        if idle > STALE_RECONNECT_S:
                            LOGGER.warning(
                                "BLE link stale (%.0fs without data); forcing reconnect",
                                idle,
                            )
                            break
            except Exception as e:
                LOGGER.debug(f"Error: {type(e)}, {e}")
                await asyncio.sleep(1.0)
            finally:
                if self.client is not None:
                    with contextlib.suppress(Exception):
                        await self.client.disconnect()

            self.client = None
            # NOTE: do not clear disconnected_event here — a reconnect signal may
            # have arrived during the disconnect above; it is cleared at the top
            # of the loop just before the next connect.

    async def connection_established(self):
        while True:
            if self.client and self.client.is_connected:
                return
            asyncio.sleep(0.1)

    @classmethod
    async def create(cls, hass, address, pin=None):
        self = WallboxBLEApiClient()
        self.client = None
        # Charger "Bluetooth Passcode" used as the SMP passkey, or None for
        # firmware that pairs Just Works / does not pair at all.
        self.pin = pin
        # Set once BlueZ tells us the passcode was rejected, so the coordinator
        # can raise ConfigEntryAuthFailed and send the user to the reauth form
        # instead of retrying a PIN that will never work.
        self.pairing_auth_failed = False
        # BLE profile UUIDs; default to the Pulsar Plus profile and refine once
        # connected via detect_profile().
        self.service_uuid = WallboxBLEApiConst.UART_SERVICE_UUID
        self.rx_uuid = WallboxBLEApiConst.UART_RX_CHAR_UUID
        self.tx_uuid = WallboxBLEApiConst.UART_TX_CHAR_UUID
        self.mode_uuid = None
        self.stream_mode = None
        self.rx_queue = asyncio.Queue()
        self.hass = hass
        self.address = address
        self.last_success = 0.0
        # Log the "pairing needs a local adapter" hint once per client instead
        # of on every reconnect attempt.
        self._pairing_unsupported_logged = False
        # Shared reconnect signal: set by the disconnected_callback AND by
        # request() when a write is abandoned, so run_ble_client rebuilds the
        # link (which also clears any orphaned dbus write).
        self._disconnected_event = asyncio.Event()
        self.client_task = asyncio.create_task(self.run_ble_client())
        return self

    async def get_parsed_response(self, request_id):
        data = bytearray()
        while True:
            try:
                data += await self.rx_queue.get()
                parsed_data = json.loads(data)
                LOGGER.debug(f"Got {parsed_data=}")
                if parsed_data["id"] == request_id:
                    return parsed_data.get("r")
                else:
                    data = bytearray()
            except:
                pass

    def clear_rx_queue(self):
        self.rx_queue = asyncio.Queue()

    @property
    def ready(self):
        return self.client and self.client.is_connected

    async def _write_abandonable(self, char, payload) -> bool:
        """Write one BLE chunk, returning True only on confirmed completion.

        On timeout the underlying write_gatt_char is ABANDONED, not awaited:
        asyncio.wait() lets us regain control after WRITE_TIMEOUT_S while the
        (possibly dbus-wedged, uncancellable) write task is left to finish or
        die on its own. This is the whole point — `await asyncio.wait_for(write)`
        would block forever waiting for a cancellation that a wedged dbus call
        never delivers. Returns False on timeout or write error.
        """
        task = asyncio.ensure_future(self.client.write_gatt_char(char, payload, True))
        done, _pending = await asyncio.wait({task}, timeout=WRITE_TIMEOUT_S)
        if task not in done:
            # Wedged: best-effort cancel (harmless if ignored) but DO NOT await
            # it. Drain later so no "exception never retrieved" is logged.
            task.cancel()
            task.add_done_callback(_drain_abandoned)
            return False
        exc = task.exception()
        if exc is not None:
            LOGGER.error(f"Failed to write to Bluetooth {exc=}")
            return False
        return True

    async def request(self, method, parameter=None):
        if not self.ready:
            LOGGER.debug(f"NOT CONNECTED! {self.client}")
            return False, None

        request_id = random.randint(1, 999)

        uart_service = self.client.services.get_service(self.service_uuid)
        if uart_service is None:
            # Services not (yet) resolved - usually because pairing/bonding has
            # not completed. Avoid raising so the coordinator just reports the
            # device as unavailable instead of logging a traceback every poll.
            LOGGER.debug("UART service not found yet (not paired/resolved?)")
            return False, None
        rx_char = uart_service.get_characteristic(self.rx_uuid)

        payload = {"met": method, "par": parameter, "id": request_id}

        data = json.dumps(payload, separators=[",", ":"])
        data = bytes(data, "utf8")
        data = b"EaE" + bytes([len(data)]) + data
        data = data + bytes([sum(c for c in data) % 256])

        self.clear_rx_queue()
        # The charger's command characteristic is a raw UART stream that only
        # accepts small ATT writes; a single large write is rejected with Write
        # Not Permitted. The official app always splits the frame into 20-byte
        # chunks (it keeps the default 23-byte ATT MTU), so we do the same
        # regardless of the negotiated MTU.
        chunk = 20
        for i in range(0, len(data), chunk):
            if not await self._write_abandonable(rx_char, data[i:i + chunk]):
                # Write timed out / failed. Abandon this request AND signal a
                # reconnect so the link (and any orphaned dbus write) is rebuilt.
                # Returning here — instead of hanging — is the fix for the
                # controller task that was pinned at current=1 for hours.
                LOGGER.warning("BLE write did not complete; abandoning and forcing reconnect")
                self._disconnected_event.set()
                return False, None

        try:
            response = await asyncio.wait_for(self.get_parsed_response(request_id), 2)
            LOGGER.debug("Got response!")
            self.last_success = asyncio.get_running_loop().time()
            return True, response
        except asyncio.TimeoutError:
            LOGGER.debug("No response!")
            return False, None

    async def async_get_data(self):
        """Get data from the API."""
        ok, data = await self.request(WallboxBLEApiConst.GET_STATUS)
        return ok, data

    async def async_set_locked(self, locked):
        """Get data from the API."""
        ok, _ = await self.request(WallboxBLEApiConst.LOCK, int(locked))
        return ok

    async def async_get_max_charge_current(self):
        """Get data from the API."""
        ok, data = await self.request(WallboxBLEApiConst.GET_MAX_AVAILABLE_CURRENT)
        return ok, data

    async def async_set_charge_current(self, current):
        """Get data from the API."""
        ok, _ = await self.request(WallboxBLEApiConst.SET_MAX_CHARGING_CURRENT, current)
        return ok
