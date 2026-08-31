"""Constants for wallbox_ble."""
from logging import Logger, getLogger

LOGGER: Logger = getLogger(__package__)

NAME = "Wallbox BLE"
DOMAIN = "wallbox_ble"
VERSION = "0.2.0"

# Config entry key holding the charger's "Bluetooth Passcode" (BLE PIN).
# Chargers on firmware >= 6.11 (all Pulsar Max / Pulsar Pro built after
# 2025-08-01) ship with a fixed 6-digit passcode enabled by default; it is
# shown in the Wallbox app under the charger information page. Older firmware
# has no passcode, in which case this key is absent/None.
CONF_PIN = "pin"

# A BLE passkey is a 6-digit decimal number (Bluetooth Core spec: 0-999999).
MAX_PASSKEY = 999999
