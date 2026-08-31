"""BLE passkey (PIN) pairing for the Wallbox BLE integration.

Wallbox firmware >= 6.11 gates the charger's GATT service behind an encrypted
BLE link: writing the notification CCCD (and, on the BGX-based radios, the
stream-mode characteristic) is rejected until SMP pairing has completed with
the charger's "Bluetooth Passcode" as the passkey. The reference ESP32 gateway
(botts7/esp32-wallbox) does this with NimBLE by declaring
``BLE_HS_IO_KEYBOARD_ONLY`` and answering ``onPassKeyRequest()`` with the
configured PIN; this module is the BlueZ equivalent.

Why we cannot just call ``BleakClient.pair()``:

  bleak's BlueZ backend calls ``org.bluez.Device1.Pair()`` and nothing else. In
  BlueZ, ``pair_device()`` resolves the pairing agent with ``agent_get(sender)``
  -- the agent registered by the *D-Bus caller* -- and falls back to
  ``MGMT_IO_CAPABILITY_NOINPUTNOOUTPUT`` when that caller has no agent. No
  agent therefore means "Just Works" pairing, which a passkey-protected charger
  rejects. There is no way to hand bleak a passkey.

So we open our own system-bus connection, export an ``org.bluez.Agent1`` with
the ``KeyboardOnly`` capability on it, and call ``Pair()`` from that same
connection -- which makes BlueZ route ``RequestPasskey`` back to us.

This module is Linux/BlueZ only, so it covers chargers reached through Home
Assistant's own adapter. A charger reached through an ESPHome Bluetooth proxy
pairs on the proxy instead: ESPHome's ``bluetooth_proxy`` handles only
``ESP_GAP_BLE_SEC_REQ_EVT`` (auto-accept) and ``ESP_GAP_BLE_AUTH_CMPL_EVT``, and
no API message carries a passkey, so the passcode has to be configured on the
ESP32. A non-connecting ``ble_client`` with an ``on_passkey_request`` automation
does that -- ``esp32_ble`` fans every GAP security event out to all registered
clients and ``esp_ble_passkey_reply()`` is keyed by BD address, not by
connection, so it answers for the proxy's link too. See the README for the YAML;
``WallboxBLEApiClient._async_pair_over_link`` drives the proxy side from here.
"""

import asyncio
import re

from .const import LOGGER

BLUEZ_SERVICE = "org.bluez"
ADAPTER_INTERFACE = "org.bluez.Adapter1"
AGENT_INTERFACE = "org.bluez.Agent1"
AGENT_MANAGER_INTERFACE = "org.bluez.AgentManager1"
DEVICE_INTERFACE = "org.bluez.Device1"
OBJECT_MANAGER_INTERFACE = "org.freedesktop.DBus.ObjectManager"

AGENT_MANAGER_PATH = "/org/bluez"
AGENT_PATH_PREFIX = "/org/bluez/wallbox_ble/agent"

# "KeyboardOnly" is what makes BlueZ ask *us* for the passkey instead of
# negotiating Just Works: paired against the charger's DisplayOnly capability
# it selects Passkey Entry with the initiator doing the input. It matches the
# reference gateway's BLE_HS_IO_KEYBOARD_ONLY.
AGENT_CAPABILITY = "KeyboardOnly"

# BlueZ gives up on its own well before this; the timeout only guards against a
# Pair() call that never returns at all.
PAIR_TIMEOUT_S = 60

# BlueZ errors that mean "the passkey was not accepted" as opposed to "the link
# went away" -- only these should send the user back to the PIN form.
AUTH_ERRORS = frozenset(
    {
        "org.bluez.Error.AuthenticationFailed",
        "org.bluez.Error.AuthenticationRejected",
        "org.bluez.Error.AuthenticationCanceled",
        "org.bluez.Error.AuthenticationTimeout",
    }
)


class PairingError(Exception):
    """Pairing failed for a reason that may be transient."""


class PairingAuthError(PairingError):
    """The charger rejected the passkey (wrong or missing Bluetooth Passcode)."""


class PairingUnsupported(PairingError):
    """No local BlueZ adapter knows this charger, so we cannot pair with it."""


def _import_dbus():
    """Import dbus-fast lazily.

    It ships with Home Assistant's Bluetooth stack on Linux (bleak's BlueZ
    backend uses it), but is absent on macOS/Windows installs, where this whole
    module is inapplicable anyway.
    """
    try:
        from dbus_fast import BusType, DBusError, Variant
        from dbus_fast.aio import MessageBus
        from dbus_fast.service import ServiceInterface, method
    except ImportError as err:  # pragma: no cover - platform dependent
        raise PairingUnsupported(f"dbus-fast/BlueZ not available: {err}") from err
    return BusType, DBusError, Variant, MessageBus, ServiceInterface, method


def _build_agent_class():
    """Build the Agent1 implementation.

    Defined inside a function because it has to subclass dbus-fast's
    ``ServiceInterface``, which we can only import lazily (see ``_import_dbus``).
    """
    _, DBusError, _, _, ServiceInterface, method = _import_dbus()

    class PasskeyAgent(ServiceInterface):
        """An org.bluez.Agent1 that answers passkey requests with a fixed PIN.

        Method names are the D-Bus member names BlueZ calls, so they keep their
        CamelCase spelling. Signatures are per BlueZ's agent-api.
        """

        def __init__(self, passkey):
            super().__init__(AGENT_INTERFACE)
            self._passkey = passkey
            # Whether BlueZ actually asked us for the PIN. If it never did, the
            # charger did Just Works and the configured PIN was irrelevant --
            # worth knowing when diagnosing a failure.
            self.passkey_requested = False

        def _require_passkey(self):
            if self._passkey is None:
                raise DBusError(
                    "org.bluez.Error.Rejected",
                    "No Bluetooth Passcode configured for this charger",
                )
            self.passkey_requested = True
            return self._passkey

        @method()
        def Release(self):
            LOGGER.debug("BlueZ released our pairing agent")

        @method()
        def RequestPasskey(self, device: "o") -> "u":
            LOGGER.debug("BlueZ asked for a passkey for %s", device)
            return self._require_passkey()

        @method()
        def RequestPinCode(self, device: "o") -> "s":
            # Legacy (BR/EDR) pairing. A BLE charger asks via RequestPasskey,
            # but answering here too costs nothing and avoids a hang if some
            # firmware revision does take this path.
            LOGGER.debug("BlueZ asked for a PIN code for %s", device)
            return f"{self._require_passkey():06d}"

        @method()
        def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):
            LOGGER.debug("BlueZ wants %s to display passkey %06d", device, passkey)

        @method()
        def DisplayPinCode(self, device: "o", pincode: "s"):
            LOGGER.debug("BlueZ wants %s to display a PIN code", device)

        @method()
        def RequestConfirmation(self, device: "o", passkey: "u"):
            # Numeric comparison. There is nobody to compare the number with, so
            # accept -- the charger is the one that authenticates us, not the
            # other way round.
            LOGGER.debug("Confirming numeric comparison for %s", device)

        @method()
        def RequestAuthorization(self, device: "o"):
            LOGGER.debug("Authorising Just Works pairing for %s", device)

        @method()
        def AuthorizeService(self, device: "o", uuid: "s"):
            LOGGER.debug("Authorising service %s on %s", uuid, device)

        @method()
        def Cancel(self):
            LOGGER.debug("BlueZ cancelled the pairing request")

    return PasskeyAgent


async def _connect_system_bus():
    """Open a private connection to the system bus."""
    BusType, _, _, MessageBus, _, _ = _import_dbus()
    try:
        return await MessageBus(bus_type=BusType.SYSTEM).connect()
    except Exception as err:  # noqa: BLE001 - any D-Bus failure means "no BlueZ"
        raise PairingUnsupported(f"Cannot reach the system D-Bus: {err}") from err


async def _get_interface(bus, path, interface):
    introspection = await bus.introspect(BLUEZ_SERVICE, path)
    return bus.get_proxy_object(BLUEZ_SERVICE, path, introspection).get_interface(
        interface
    )


def _find_device_path(objects, address, adapter=None):
    """Find the BlueZ object path of ``address``, preferring ``adapter``.

    The same charger can be known to several adapters; the bond has to be
    created on the one Home Assistant actually connects through, otherwise we
    would pair over a radio that is not the one bleak uses.
    """
    target = address.upper()
    matches = [
        path
        for path, interfaces in objects.items()
        if (device := interfaces.get(DEVICE_INTERFACE)) is not None
        and (variant := device.get("Address")) is not None
        and str(variant.value).upper() == target
    ]
    if not matches:
        return None
    if adapter:
        prefix = f"/org/bluez/{adapter}/"
        for path in matches:
            if path.startswith(prefix):
                return path
    return matches[0]


async def _get_managed_objects(bus):
    object_manager = await _get_interface(bus, "/", OBJECT_MANAGER_INTERFACE)
    return await object_manager.call_get_managed_objects()


async def _resolve_device(bus, address, adapter):
    """Return (device_path, Device1 proxy) or raise PairingUnsupported."""
    objects = await _get_managed_objects(bus)
    path = _find_device_path(objects, address, adapter)
    if path is None:
        raise PairingUnsupported(
            f"{address} is not known to any local BlueZ adapter. If it is served "
            "by an ESPHome Bluetooth proxy, configure the passcode on the proxy "
            "(esp32_ble io_capability: keyboard_only plus a non-connecting "
            "ble_client with an on_passkey_request automation) -- see the README."
        )
    return path, await _get_interface(bus, path, DEVICE_INTERFACE)


async def _pair(bus, device, device_path, passkey):
    """Register an agent on ``bus`` and pair, translating BlueZ errors."""
    _, DBusError, _, _, _, _ = _import_dbus()

    agent = _build_agent_class()(passkey)
    agent_path = f"{AGENT_PATH_PREFIX}_{device_path.rsplit('/', 1)[-1]}"
    bus.export(agent_path, agent)
    try:
        agent_manager = await _get_interface(
            bus, AGENT_MANAGER_PATH, AGENT_MANAGER_INTERFACE
        )
        try:
            await agent_manager.call_register_agent(agent_path, AGENT_CAPABILITY)
        except DBusError as err:
            raise PairingError(
                f"Could not register a pairing agent: {err.type}: {err.text}"
            ) from err
        LOGGER.debug(
            "Registered pairing agent at %s (%s)", agent_path, AGENT_CAPABILITY
        )

        try:
            await asyncio.wait_for(device.call_pair(), PAIR_TIMEOUT_S)
        except asyncio.TimeoutError as err:
            raise PairingError(
                f"BlueZ did not answer Pair() within {PAIR_TIMEOUT_S}s"
            ) from err
        except DBusError as err:
            if err.type == "org.bluez.Error.AlreadyExists":
                LOGGER.debug("Charger was already paired")
                return
            if err.type in AUTH_ERRORS:
                raise PairingAuthError(
                    f"Charger rejected the Bluetooth Passcode ({err.type})"
                    if agent.passkey_requested
                    else f"Pairing was not authenticated ({err.type}); the charger "
                    "did not ask for a passcode"
                ) from err
            raise PairingError(f"Pairing failed: {err.type}: {err.text}") from err
        finally:
            try:
                await agent_manager.call_unregister_agent(agent_path)
            except Exception as err:  # noqa: BLE001 - best effort cleanup
                LOGGER.debug("Could not unregister pairing agent: %s", err)
    finally:
        bus.unexport(agent_path, agent)

    if not agent.passkey_requested and passkey is not None:
        LOGGER.debug(
            "Paired without the charger asking for a passkey (Just Works); the "
            "configured Bluetooth Passcode was not needed"
        )


async def async_ensure_paired(address, passkey=None, adapter=None):
    """Make sure the charger at ``address`` is bonded, pairing if it is not.

    Returns True if a bond was created by this call, False if one already
    existed. Raises PairingUnsupported/PairingAuthError/PairingError otherwise.
    """
    bus = await _connect_system_bus()
    try:
        device_path, device = await _resolve_device(bus, address, adapter)
        if await device.get_paired():
            LOGGER.debug("%s is already paired with BlueZ", address)
            return False

        LOGGER.debug("Pairing with %s at %s", address, device_path)
        try:
            await _pair(bus, device, device_path, passkey)
        except PairingAuthError:
            # BlueZ keeps a half-built bond after a rejected passkey and will
            # answer the next Pair() from that stale state instead of running
            # SMP again, so a corrected PIN would never be tried. Drop the
            # device; Home Assistant's scanner re-adds it on the next
            # advertisement.
            await _async_remove_device(bus, device_path)
            raise

        try:
            # Trusted bonds do not need per-service authorisation later on.
            await device.set_trusted(True)
        except Exception as err:  # noqa: BLE001 - purely a convenience
            LOGGER.debug("Could not mark %s trusted: %s", address, err)

        LOGGER.info("Paired with Wallbox charger %s", address)
        return True
    finally:
        bus.disconnect()


async def _async_remove_device(bus, device_path):
    """Ask the owning adapter to forget a device (drops the bond)."""
    adapter_path = device_path.rsplit("/", 1)[0]
    try:
        adapter = await _get_interface(bus, adapter_path, ADAPTER_INTERFACE)
        await adapter.call_remove_device(device_path)
        LOGGER.debug("Removed %s from BlueZ", device_path)
    except Exception as err:  # noqa: BLE001 - best effort
        LOGGER.debug("Could not remove %s from BlueZ: %s", device_path, err)


async def async_unpair(address, adapter=None):
    """Drop the bond for ``address``. Returns True if a device was removed."""
    try:
        bus = await _connect_system_bus()
    except PairingUnsupported:
        return False
    try:
        objects = await _get_managed_objects(bus)
        device_path = _find_device_path(objects, address, adapter)
        if device_path is None:
            return False
        await _async_remove_device(bus, device_path)
        return True
    finally:
        bus.disconnect()


def async_adapter_for_address(hass, address):
    """Name of the local BlueZ adapter Home Assistant sees ``address`` on.

    Returns None when the charger is only reachable through a remote scanner
    (an ESPHome Bluetooth proxy), which is the case where the passkey has to be
    answered on the proxy instead -- remote scanner "adapters" are not hciN
    devices, so the pattern match rejects them.
    """
    try:
        from homeassistant.components.bluetooth import (
            async_scanner_devices_by_address,
        )
    except ImportError:  # pragma: no cover - very old cores
        return None

    for scanner_device in async_scanner_devices_by_address(
        hass, address, connectable=True
    ):
        adapter = getattr(scanner_device.scanner, "adapter", None)
        if isinstance(adapter, str) and re.fullmatch(r"hci\d+", adapter):
            return adapter
    return None
