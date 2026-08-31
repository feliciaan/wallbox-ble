# Changelog

All notable changes to this fork are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

This is a fork of [jagheterfredrik/wallbox-ble](https://github.com/jagheterfredrik/wallbox-ble).

## [0.5.0] - 2026-08-31

### Added

- **BLE passkey (PIN) pairing.** Chargers on firmware >= 6.11 — every Pulsar Max
  and Pulsar Pro built after 2025-08-01 — refuse the notification CCCD write until
  the BLE link is encrypted, authenticating it with the fixed 6-digit "Bluetooth
  Passcode" shown in the Wallbox app. The config flow now asks for that passcode
  (optional; leave blank on older firmware), and `pairing.py` performs SMP pairing
  with it before connecting, retrying once after a rejected `start_notify`.
- Reconfigure and reauth flows for the passcode. A passcode the charger rejects
  raises `ConfigEntryAuthFailed`, so Home Assistant asks for a new one instead of
  reconnecting forever; changing it drops the stale BlueZ bond first, since BlueZ
  answers `Pair()` for an already-bonded device from its saved keys without
  re-running SMP. Removing the config entry removes the bond too.
- Pulsar Max row in the README's BLE profile table.

### Notes

- `BleakClient.pair()` cannot carry a passkey: bleak's BlueZ backend just calls
  `org.bluez.Device1.Pair()`, and BlueZ resolves the pairing agent with
  `agent_get(sender)` — the agent registered by the *D-Bus caller* — falling back
  to `NOINPUTNOOUTPUT` (Just Works) when that caller has none. The integration
  therefore opens its own system-bus connection, exports an `org.bluez.Agent1`
  with the `KeyboardOnly` capability on it, and calls `Pair()` from that same
  connection so BlueZ routes `RequestPasskey` back to us. This mirrors the
  `botts7/esp32-wallbox` reference gateway's NimBLE `BLE_HS_IO_KEYBOARD_ONLY` +
  `onPassKeyRequest()`.
- Through an ESPHome Bluetooth proxy the passcode has to be configured on the
  ESP32: `bluetooth_proxy` handles only `ESP_GAP_BLE_SEC_REQ_EVT` and
  `ESP_GAP_BLE_AUTH_CMPL_EVT`, and no API message carries a passkey. It still
  works, because `esp32_ble` fans every GAP security event out to all registered
  clients and `esp_ble_passkey_reply()` is keyed by BD address rather than by
  connection — so a `ble_client` with `auto_connect: false` (which never opens a
  connection of its own) answers `on_passkey_request` for the proxy's link. The
  integration drives the proxy side by falling back to `BleakClient.pair()`
  (`bluetooth_device_pair` → `esp_ble_set_encryption`) when the charger is not on
  a local BlueZ adapter; requires ESPHome 2024.3.0 or newer. See the README for
  the YAML. Chargers without a passcode are unaffected.

## [0.4.0] - 2026-08-01

### Added

- Support for the **Wallbox Pulsar Max** on older firmware (≤ 6.11.16). Adds the
  Max's single-characteristic "u-blox" BLE profile (one characteristic used for
  both writes and notifications, no stream-mode characteristic) and a Bluetooth
  discovery matcher for its service UUID. UUIDs sourced from the
  `botts7/esp32-wallbox` reference gateway.

### Known limitations

- Pulsar Max on firmware ≥ 6.11.26 is **not** supported: that firmware switches
  to the Pulsar Plus dual-char profile and requires an encrypted BLE link (SMP
  pairing, charger PIN used as passkey) before notifications are accepted, which
  is not implemented yet. *(Resolved in 0.5.0.)*

## [0.3.0] - 2026-08-01

### Fixed

- Charging end no longer hangs Home Assistant service calls / automations. A
  BlueZ/dbus `write_gatt_char` can wedge in an uncancellable state where the ATT
  write already reached the charger but the D-Bus reply never arrives, so
  `asyncio.wait_for()` hangs forever. Writes now use an abandonable pattern
  (`asyncio.wait` with a 2s `WRITE_TIMEOUT_S`): on timeout the orphaned write is
  abandoned without awaiting it, control returns to the caller, and the task is
  drained later to avoid "Task exception was never retrieved" log spam.
- A stalled write now signals the shared reconnect event instead of hanging,
  triggering recovery.

## [0.2.0] - 2026-07-10

### Added

- Self-heal watchdog in the coordinator: if no successful update happens for
  `SELF_RELOAD_STALE_S` (180s), the integration reloads itself to recover from a
  hung charger connection.
- New measured sensors decoded from the charger's `GET_STATUS` frame: measured
  charging current, measured charging power, and energy.

### Fixed

- Hanging charger connection recovery.

## [0.1.2] - 2026-07-08

### Changed

- Add `@daniel-meyer-pl` to codeowners.

## [0.1.1] - 2026-07-08

### Fixed

- Point `documentation` and `issue_tracker` manifest URLs to this fork.

## [0.1.0] - 2026-06-30

### Added

- Support for the original Wallbox Pulsar (Zentri firmware).
- Support for ESPHome Bluetooth proxies.
- Pairing/bonding on connect.
- Bundled brand icons for HA 2026.3+ (local brand images).
- Logo in README (light/dark variants).

## [0.0.0] - Base

Inherited from the upstream [jagheterfredrik/wallbox-ble](https://github.com/jagheterfredrik/wallbox-ble)
project: charge start/stop, charge current control, lock status via `GET_STATUS`,
availability state, and automatic reconnect.

[0.4.0]: https://github.com/daniel-meyer-pl/wallbox-ble/releases/tag/0.4.0
[0.3.0]: https://github.com/daniel-meyer-pl/wallbox-ble/releases/tag/0.3.0
[0.2.0]: https://github.com/daniel-meyer-pl/wallbox-ble/releases/tag/0.2.0
[0.1.2]: https://github.com/daniel-meyer-pl/wallbox-ble/releases/tag/0.1.2
[0.1.1]: https://github.com/daniel-meyer-pl/wallbox-ble/releases/tag/0.1.1
[0.1.0]: https://github.com/daniel-meyer-pl/wallbox-ble/releases/tag/0.1.0
