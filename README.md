<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="wallbox-white.svg">
    <img src="wallbox.svg" alt="Wallbox BLE" width="150">
  </picture>
</p>

# Wallbox BLE for Home Assistant

Local control of a Wallbox charger over Bluetooth Low Energy (BLE) — no cloud, no
WiFi and no MyWallbox account required. Works with the BLE-only **Pulsar** as well
as the **Pulsar Plus**.

## Supported hardware / BLE profiles

Wallbox shipped several BLE radio modules across hardware revisions. They all
speak the same `EaE`+JSON application protocol but expose different GATT UUIDs.
This integration auto-detects the profile on connect:

| Profile | Devices | Service UUID |
| --- | --- | --- |
| `zentri` | original **Pulsar** (no WiFi) and newer units | `175f8f23-…` |
| `bgexpress` | **Pulsar Plus** | `331a36f5-…` |
| `pulsar_max` | **Pulsar Max** | `2456e1b9-…` |

Zentri-based units are additionally switched into "stream" mode, and the session is
logged in with the charger's own user id — both handled automatically.

## Bluetooth Passcode (BLE PIN)

Chargers on firmware **6.11 or newer** — which is every Pulsar Max and Pulsar Pro
built after 1 August 2025 — refuse to enable GATT notifications until the BLE link
is encrypted, and they authenticate that link with a fixed 6-digit *Bluetooth
Passcode*. Find it in the Wallbox app on the charger information page (on 6.11+ you
can also change it there), and enter it when adding the charger to Home Assistant.

Older firmware has no passcode: leave the field empty and the charger is controlled
over an unauthenticated GATT connection, exactly like the official app.

The passcode can be corrected later via *Settings → Devices & Services → Wallbox BLE
→ Reconfigure*; if the charger rejects the stored one, Home Assistant raises a
repair notification and asks for it again.

### Through an ESPHome Bluetooth proxy

With a local adapter, Home Assistant answers the passkey itself and there is nothing
more to configure. Through a proxy it cannot: `bluetooth_proxy` handles only
`ESP_GAP_BLE_SEC_REQ_EVT` and `ESP_GAP_BLE_AUTH_CMPL_EVT`, and no API message carries
a passkey — so the passcode has to live on the ESP32.

It still works, because `esp32_ble` fans every GAP security event out to *all*
registered clients and `esp_ble_passkey_reply()` is keyed by the peer's BD address
rather than by connection. A `ble_client` with `auto_connect: false` therefore never
opens a connection of its own (so it does not compete for the charger's single
connection slot), but still sees the passkey request for that address and can answer
it — including for the link the proxy opened. Home Assistant starts that pairing by
calling `bluetooth_device_pair` (`esp_ble_set_encryption()` on the ESP32) as soon as
it connects, so you still enter the passcode in Home Assistant too.

#### Components needed on the proxy

| Component | Why | Notes |
| --- | --- | --- |
| `esp32_ble` | `io_capability: keyboard_only` — **without this the ESP32 advertises NoInputNoOutput and can only do "Just Works"**; `auth_req_mode: sc_mitm_bond` | Must be listed explicitly; the default is `none`. It is not auto-added with a useful value |
| `esp32_ble_tracker` | Owns the client list that GAP security events are fanned out to | Auto-loaded, but usually already present |
| `bluetooth_proxy` | The proxy itself; `active: true` is required for connections | Needs `api:` |
| `ble_client` | Hosts the `on_passkey_request` automation that answers the passkey | Auto-loads `esp32_ble_client` |

#### Configuration

```yaml
esp32_ble:
  io_capability: keyboard_only   # default is "none" => Just Works only
  auth_req_mode: sc_mitm_bond    # Secure Connections + MITM + bond — keep the SC part
  max_connections: 4             # 3 proxy slots + 1 for the ble_client below

esp32_ble_tracker:

bluetooth_proxy:
  active: true

ble_client:
  - id: wallbox_passkey
    mac_address: 54:64:DE:92:BC:7C   # your charger
    auto_connect: false              # never connect; only answer the passkey
    on_passkey_request:
      then:
        - logger.log: "Wallbox asked for the passkey; answering"
        - ble_client.passkey_reply:
            id: wallbox_passkey
            passkey: 123456          # your Bluetooth Passcode
```

**Keep Secure Connections on.** Use `sc_mitm_bond`, not `bond_mitm`: the latter
drops the Secure Connections bit (`ESP_LE_AUTH_REQ_SC_ONLY`, `0x08`) and falls back to
LE legacy pairing, which the charger rejects with SMP 0x03 "authentication
requirements" — the same error as a missing `io_capability`. `sc_mitm_bond` is also
ESP-IDF's own default (`bte_appl_cfg` in `bta_dm_co.c`) and matches the reference
gateway's NimBLE `setSecurityAuth(bond, mitm, sc)`. The `logger.log` line is optional,
but it shows in the proxy log whether the passkey request ever arrives.

**`max_connections` is the other part that is easy to get wrong.** The helper `ble_client`
consumes a connection slot of its own, so with the defaults (`max_connections: 3`,
three proxy slots) the budget is overrun and ESPHome warns:

```
WARNING BLE components require 4 connection slot(s) but only 3 configured.
        Components: bluetooth_proxy, bluetooth_proxy, bluetooth_proxy, ble_client
```

Ignore it and the fourth client fails at runtime with `ESP_GATT_NO_RESOURCES`. Raise
`max_connections` to 4 as above, or keep the default RAM budget by giving the proxy
one slot fewer with `bluetooth_proxy: connection_slots: 2`.

#### Version notes

Verified against ESPHome 2026.9.0-dev and 2024.3.0 (`esphome config`). 2024.3.0 is
the floor — it is where `bluetooth_proxy` gained the `PAIRING` feature flag that
`bluetooth_device_pair` needs. On that vintage `esp32_ble` has no `max_connections`
or `auth_req_mode` key and there is no slot accounting at all: drop both lines and
budget by hand with a two-entry `bluetooth_proxy: connections:` list instead. On
current ESPHome that legacy `connections:` list is still accepted but no longer
changes the slot count — use `connection_slots:` there.

Chargers without a passcode keep working over a proxy with no extra configuration.

#### Troubleshooting

```
ERROR ... Failed to write to Bluetooth exc=BleakError('Bluetooth GATT Error
      address=54:64:DE:92:BC:7C handle=23 error=5 description=Insufficient authentication')
WARNING ... BLE write did not complete; abandoning and forcing reconnect
```

That wording (`handle=… error=… description=…`) comes from `aioesphomeapi`, so the
charger is being reached **through a proxy**, not a local adapter. ATT error 5 means
the link is not *authenticated* — either nothing paired at all, or it paired "Just
Works" without the passkey. Note that connecting and enabling notifications both
succeed regardless; this charger only enforces authentication on the command
characteristic, so a rejected write is the first sign. Check, in order:

1. **Is a passcode stored in Home Assistant?** *Settings → Devices & Services →
   Wallbox BLE → Reconfigure*. An entry added before this feature existed has none,
   and without one the integration never asks the proxy to pair.
2. **Does the proxy have the YAML above?** Without `io_capability: keyboard_only` the
   ESP32 advertises NoInputNoOutput and can only negotiate Just Works — which
   encrypts the link but leaves it unauthenticated, giving exactly this error.
3. **Is the `ble_client` MAC right, and did the proxy pick up the slot change?**
   Watch the proxy's log while Home Assistant reconnects; you should see the passkey
   request arrive. `ESP_GATT_NO_RESOURCES` there means the connection-slot budget is
   still overrun.

##### Decoding `Pairing failed due to error: N`

A proxy reports the ESP-IDF SMP failure reason as a bare number. Bluedroid offsets
SMP status codes by `BTA_DM_AUTH_FAIL_BASE`, which is `HCI_ERR_MAX_ERR` (0x43) + 10
= **77** (`bta_api.h`), so subtract 77 to get the SMP code. The integration does this
for you and logs the meaning; the common ones are:

| Reported | SMP | Meaning |
| --- | --- | --- |
| 78 | 0x01 passkey entry failed | The charger rejected the passcode — check it in the Wallbox app |
| 80 | 0x03 authentication requirements | The proxy cannot meet the charger's requirements: `io_capability` is still `none`, **or** `auth_req_mode` lacks Secure Connections (`bond_mitm` instead of `sc_mitm_bond`) |
| 81 | 0x04 confirm value failed | Passcode mismatch |
| 86 | 0x09 repeated attempts | The charger is rate-limiting after earlier failures — wait a few minutes or power-cycle it |
| 89 | 0x0C numeric comparison failed | The charger chose numeric comparison — use `on_numeric_comparison_request` with `ble_client.numeric_comparison_reply` |
| 102 | 0x19 SMP timed out | Pairing started and nothing answered — usually no `on_passkey_request` for this MAC, or the MAC does not match |

Values of 77 or below are plain HCI errors, not SMP codes.

## Implemented features

**Controls:** lock, charge current, start/stop charging (while charging or paused),
auto-lock on/off and delay, Halo LED brightness, reboot charger.

**Charger:** status, car connected, session energy, charging
current and power, max available current, notifications. Diagnostics: supply voltage,
total sessions, Power Boost limit, firmware, serial number, grounding, OCPP status
(off by default).

**Home:** grid and house current and power from the Power Boost meter (below).

Settings and slow counters (auto-lock, Halo, notifications, sessions, Power Boost
limit) are read every 5 minutes rather than every 10 s, to keep traffic over a
Bluetooth proxy down; changing a setting re-reads them immediately.

## Grid meter (Power Boost)

Chargers fitted with Wallbox's **Power Boost** meter report what that meter measures,
read over BLE with BAPI `r_dca`.

| Sensor | Meaning |
| --- | --- |
| **Grid current** / **Grid power** | Everything through the grid connection |
| **House current / power (excl. charger)** | Grid minus the charger's own draw |
| **Grid meter energy** | The meter's lifetime counter, kWh. Needs a voltage reference (below) |

**Wire the meter's voltage reference if you can.** Power Boost itself only needs the
current clamp, so meters are often installed without one; the meter then reports 0 for
power, voltage and energy. With it wired:
- *Grid power* is the meter's own **active power**. That accounts for power factor,
  which current × voltage does not.
- *Grid meter energy* becomes available and can be used in the Energy dashboard.
- The meter becomes the voltage source (below).

Without it, grid and house power are worked out as current × voltage, and *Grid meter
energy* stays unavailable rather than showing 0.

**Where the voltage comes from**, best source first; the *Supply voltage* sensor's
`source` attribute says which one is in use:
1. `meter`: the Power Boost meter, when it has a voltage reference.
2. `charger`: the charger reports no voltage, but while charging it reports its power
   (`cp`) and its current, so voltage = power ÷ current.
3. The last value seen, between charges.
4. `nominal`: 230 V before any of the above.

**What "house" means here.** Power Boost meters are installed at the main connection,
so they measure the whole supply *including the charger*. House = grid − charger, using
the charger's own reported power, so grid = house + charger. House is never shown below
0, and the direction of the meter's reading is ignored: without generation on the
connection the house cannot feed the grid, so a negative value is only instrument
mismatch or a clamp fitted the wrong way round.

**Check your installation once.** Start charging while the house is otherwise quiet.
*Grid power* should rise by about the charging power while *House power* barely moves.
If grid power doesn't change when charging starts, your meter only sees the house
circuits; then *Grid power* is your house figure.

On a charger without a meter these sensors stay unavailable. The charger answers
`r_dca` with BAPI error 4, after which the integration stops asking.

For the Energy dashboard, use *Grid meter energy* for grid consumption. For the house alone, run *House power* through Home
Assistant's **Integral** helper (Riemann sum, method *left*).

## Requirements
 - Home Assistant with a working Bluetooth integration — either a local adapter
   in range of the charger, or an [ESPHome Bluetooth proxy](https://esphome.io/components/bluetooth_proxy.html).

## Installation (HACS)
1. HACS → ⋮ → *Custom repositories* → add this repository as an *Integration*.
2. Install **Wallbox BLE** and restart Home Assistant.
3. The charger (advertised as `WBxxxxxx`) is auto-discovered under
   *Settings → Devices & Services*; add it.
4. When asked for the **Bluetooth Passcode**, enter the 6-digit code from the
   Wallbox app, or leave it empty if your charger's firmware predates 6.11.

## Notes
 - Make sure the charger is within Bluetooth range of the HA host or a BT proxy
   (through a proxy, a passcode-protected charger needs the extra YAML above).
 - The bond is stored by BlueZ, so pairing happens once; removing the integration
   removes the bond again.
 - The charger accepts a single BLE connection at a time; if the Wallbox phone app
   is connected it may briefly block Home Assistant (and vice-versa).

## Development

```sh
pip install -r requirements_test.txt
python -m pytest tests -n auto --timeout=60
```

No Home Assistant install is needed. `tests/test_simulation.py` runs the real
connection loop, request path and coordinator against a simulated charger, across
every combination of Bluetooth path (local adapter / ESPHome proxy), radio profile
(Pulsar Max / Plus / original Pulsar), passcode, Power Boost meter and link speed,
plus dropped links, ignored commands, start-up notification failures, a wrong
passcode, a proxy without the passcode, writes during polling and shutdown. The
simulated charger checks every frame byte for byte and stops answering on a corrupt
one, as the real one does.

