# Changelog

All notable changes to this fork are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

This is a fork of [jagheterfredrik/wallbox-ble](https://github.com/jagheterfredrik/wallbox-ble).

## [0.6.0] - 2026-10-11

Brings over the charger features of the `botts7/esp32-wallbox` gateway that work
through an ordinary Bluetooth proxy, so no dedicated ESP32 is needed. Left out on
purpose: solar (Eco-Smart), phase switching, dynamic power sharing, and the gateway's
own health sensors and schedules, which Home Assistant automations cover.

### Added

- **Grid and house current and power** from the Power Boost meter (`r_dca`). With the
  meter's voltage reference wired, grid power is the meter's own active power and its
  energy counter is exposed as *Grid meter energy*; without it, power is current ×
  voltage. House = grid − the charger's own power, never below 0.
- **Supply voltage**: the meter when it has a voltage reference, else the charger's
  power ÷ current while charging, else the last value, else 230 V. The `source`
  attribute names which.
- **Car connected** binary sensor.
- **Max available current**, **notifications** (count and latest message).
- **Auto-lock** switch and delay, **Halo LED** brightness select, **Reboot** button.
- Diagnostics: total sessions, Power Boost limit, firmware, serial number, grounding,
  OCPP status (off by default).
- Proxy pairing errors are decoded into the SMP reason and the fix to make.

### Changed

- Lock state comes from the charger's own `r_sta.lock_status`.
- Charging power is the charger's reported power (`r_dat.cp`) where available.
- Pairing through a proxy is requested once per connection, and a command write the
  charger rejects as unauthenticated triggers pairing and is resent.
- Requests to the charger are sent one at a time, and polling waits until the
  connection is fully set up (notifications, pairing, login).
- Reply and write timeouts suit a Bluetooth proxy: up to 8 s for the first piece of a
  reply, 3 s between pieces, 20 s in all; 10 s per write. Leftovers of a late reply are
  dropped, and request ids are sequential.
- Settings and slow counters are read every 5 minutes, identity once per connection;
  a write re-reads them at once. A read the charger has never answered is skipped
  after three tries; one it has answered is always asked again.
- A link that drops mid-request is treated as a lost link and reconnected, not
  reported as a rejected passcode.
- A missed status reply keeps the last values; the device goes unavailable only after
  three missed polls in a row.
- The session login is retried on the first successful status read.
- The "cannot pair from Home Assistant (D-Bus)" message is debug, not a warning;
  it is expected on Home Assistant Container.
- README: the proxy YAML uses `auth_req_mode: sc_mitm_bond`, and gains a
  troubleshooting section.

### Fixed

- Home Assistant shutdown no longer reports "Task could not be canceled", and an
  unanswered request can no longer hang polling.
- The BLE client task is stopped when the entry unloads or reloads.
- Status code 19 ("connected, no current") is recognised; unknown codes show as
  `UNKNOWN_<n>`.
- Connect-path warnings are logged once per cause instead of on every retry.

### Tests

- `tests/`: runnable without Home Assistant (`requirements_test.txt`), including an
  end-to-end simulation of the charger across Bluetooth path, radio profile, passcode,
  meter and link speed, and a replay of a real proxy link.

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
  (`bluetooth_device_pair` → `esp_ble_set_encryption`) as soon as it connects,
  rather than waiting for `start_notify` to fail — a proxy can report the CCCD
  write as successful while the charger drops it. The proxy needs `esp32_ble`
  (with `io_capability: keyboard_only`), `esp32_ble_tracker`, `bluetooth_proxy`
  and `ble_client`, on ESPHome 2024.3.0 or newer; mind that the helper
  `ble_client` consumes a connection slot, so `esp32_ble: max_connections` has to
  be raised (or the proxy's own slot count lowered). The README carries the YAML,
  validated with `esphome config` against 2026.9.0-dev and 2024.3.0. Chargers
  without a passcode are unaffected.

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
