# Changelog

All notable changes to this fork are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

This is a fork of [jagheterfredrik/wallbox-ble](https://github.com/jagheterfredrik/wallbox-ble).

## [0.6.0] - 2026-10-11

Brings over the charger features of the `botts7/esp32-wallbox` gateway that work
through an ordinary Bluetooth proxy, so the charger no longer needs a dedicated ESP32
for them. Left out on purpose: solar (Eco-Smart, live solar session data), phase
switching and dynamic power sharing (single-phase, single-charger installs), and the
gateway's own health sensors and schedule/reminder logic, which Home Assistant
automations cover.

### Added

- **Grid and house current and power from the Power Boost meter** (BAPI `r_dca`).
  With the meter's voltage reference wired, grid power is the meter's own active power
  (`p1..p3`) and its energy counter (`e`, Wh) is exposed. Without one, the meter
  reports 0 for power, voltage and energy, so grid power falls back to current
  (`c1..c3`, 0.1 A) × voltage and the energy sensor stays unavailable instead of
  showing 0. House = grid − the charger's own reported power, since the meter sits
  at the grid connection; never below 0, and the reading's direction is ignored
  (no generation, so negative is only mismatch or a reversed clamp). The reference
  gateway's "House Power" is the raw grid sum, which overstates the house by the
  charging power.
- **Supply voltage**, best source first: the meter when it has a voltage reference;
  else the charger's power ÷ current while charging (`r_dat.cp` is kW; used only at
  ≥ 5 A and 180–260 V), since the charger reports no voltage; else the last value;
  else 230 V. Its `source` attribute names which.
- **Car connected** binary sensor, from the status code; while locked (status 6) it
  uses `r_sta.charger_status` = 19 to tell "locked, car" from "locked, no car".
- **Lock state from `r_sta.lock_status`**, the charger's actual lock, instead of
  inferring it from status 6. Falls back to status 6 if `r_sta` is unavailable.
- **Lifetime charged energy** (`r_sta.grid`, in Wh — not the 10 Wh units of
  `r_dat.en`) and **max available current**.
- **Auto-lock** switch and delay (`g_alo` / `s_alo`). The charger stores one value,
  seconds, 0 = off, and answers with a bare number; switching off and on again
  restores the previous delay.
- **Halo LED** brightness select (`g_halocfg` / `s_halocfg`). The write needs the whole
  object, so standby mode and timeout are kept as last read.
- **Reboot charger** button (`rebot`).
- **Notifications** count and latest message (`r_not`).
- Diagnostics: total sessions (`r_ses.last`; `size` is the log capacity, 99999 on the
  Max), Power Boost limit (`r_hsh`), firmware (`fw_v_`, answered outside `r`), serial
  number, grounding (shape and "healthy" code differ by family, so integers are shown
  as codes), and OCPP status (off by default).
- Settings and slow counters are read every 5 minutes, identity once per connection,
  and a write re-reads them at once.

### Fixed

- **Status code 19 ("connected, no current") no longer breaks every poll.** The status
  list stopped at 18 and the lookup indexed it directly, raising `IndexError`; Power
  Boost holding current back is exactly when 19 appears. Unknown codes now show as
  `UNKNOWN_<n>` instead of raising.
- A poll that returns no data no longer raises on `data.get`.

### Changed

- BAPI error responses are kept rather than discarded, so "not supported" (error 4)
  is told apart from a missed answer: only error 4 stops meter polling, and a missed
  answer keeps the last reading instead of flapping sensors to unavailable.
- Charging power is the charger's own reported power (`r_dat.cp`) where it reports
  one, else current × the supply voltage above, rather than current × a fixed 230 V.

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
- Pulsar Max row in the README's BLE profile table, and a troubleshooting section
  for the `Insufficient authentication` write error.
- `tests/`: pytest suite (47 tests) runnable without Home Assistant installed —
  `python -m pytest tests`. Covers the BlueZ agent against a faked bus, passcode
  parsing, error classification, SMP decoding, shutdown, and the proxy pairing
  race above (which hangs on the code before the fix).

### Fixed

- An `Insufficient authentication` rejection on a command write now triggers
  pairing and resends the frame, instead of only reconnecting in a loop. This
  charger accepts the notification CCCD write on an unauthenticated link and
  only enforces authentication on the command characteristic, so keying the
  pairing retry off a failing `start_notify` never fired. ATT error 5 also
  covers a link encrypted with an unauthenticated ("Just Works") key, which is
  what a Bluetooth proxy left at the default `io_capability` negotiates.
- A rejected write now logs one actionable error naming the fix — set the
  passcode, or configure it on the proxy — rather than repeating a raw
  `BleakError` per chunk per poll.
- The BLE client task is now cancelled when the config entry unloads. It never
  was, so every reload — including the coordinator's own 180s self-heal, which
  fires exactly when a charger is refusing commands — left the previous
  `run_ble_client()` looping forever. The orphans kept reconnecting, so several
  clients fought over the charger's single connection slot and each logged its
  own copy of every failure.
- Warnings from the connect path are logged once per client instead of once per
  reconnect attempt, which is every second while a link is failing — keyed by
  cause, so a changing failure is still reported.
- Pairing over a Bluetooth proxy now sends one request per connection. The
  coordinator's poll starts as soon as the link is up, before setup finishes, so
  a rejected write retried pairing *while the connect path's own request was
  still in flight*. The proxy answers once per `AUTH_CMPL`, so the overlapping
  request got no answer and hung for the full 30 s API timeout; then the failed
  write forced a reconnect, dropping the link under the pending request
  ("changed connection status while waiting for BluetoothDevicePairingResponse").
  Pairing is now serialised behind a lock with the result cached per link, a
  poll that hits a pairing in progress simply fails that poll, and an
  authentication rejection no longer forces a reconnect — reconnecting cannot
  authenticate a link. The 60 s health check still reconnects a link that stays
  unusable, which gives one fresh pairing attempt per minute.
- README: the proxy YAML recommended `auth_req_mode: bond_mitm`, which clears the
  Secure Connections bit and downgrades the ESP32 to LE legacy pairing — the
  opposite of ESP-IDF's own default (`SC_MITM_BOND`, `bta_dm_co.c`) and of the
  reference gateway's NimBLE `setSecurityAuth(bond, mitm, sc)`. A charger that
  insists on Secure Connections rejects that with SMP 0x03, indistinguishable from
  a missing `io_capability`. Now `sc_mitm_bond`, with a `logger.log` line in the
  passkey automation so the proxy log shows whether the request arrives; the 0x03
  hint in the integration's log names both causes.
- A Bluetooth proxy's bare pairing error number is now decoded. ESPHome forwards
  ESP-IDF's `auth_cmpl.fail_reason`, which Bluedroid offsets by
  `BTA_DM_AUTH_FAIL_BASE` (`HCI_ERR_MAX_ERR` 0x43 + 10 = 77), so e.g. `error: 80`
  is SMP 0x03 "authentication requirements" and `error: 102` is SMP 0x19 "timed
  out". The log now names the code and what to change.
- The "cannot pair from Home Assistant" warning now says what happens next
  (falling back to the proxy, which needs the passcode configured on the proxy).
  Home Assistant Container installs have no system D-Bus at all, so that message
  is expected there rather than a fault in itself.

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
