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

Keep `auth_req_mode: sc_mitm_bond`: the charger requires Secure Connections, and
`bond_mitm` drops it. The `logger.log` line is optional; it shows in the proxy log
that the passkey request arrived.

The helper `ble_client` takes a connection slot of its own, hence
`max_connections: 4` (three proxy slots plus one). Alternatively keep the default and
give the proxy one slot fewer with `bluetooth_proxy: connection_slots: 2`.

Requires ESPHome 2024.3.0 or newer (for `bluetooth_device_pair`). On versions without
`esp32_ble: max_connections` / `auth_req_mode`, drop those lines and use a two-entry
`bluetooth_proxy: connections:` list instead.

Chargers without a passcode keep working over a proxy with no extra configuration.

#### Troubleshooting

| Log | Fix |
| --- | --- |
| `error=5 description=Insufficient authentication` on a write | The link is not authenticated: store the passcode in Home Assistant (*Reconfigure*) and add the YAML above to the proxy |
| `Pairing … through the Bluetooth proxy failed: SMP … (0x01)` or `(0x04)` | Wrong passcode — check it in the Wallbox app |
| Same, with `(0x03)` | Proxy is missing `io_capability: keyboard_only` or `auth_req_mode: sc_mitm_bond` |
| Same, with `(0x09)` | The charger is rate-limiting after failed attempts — wait a few minutes |
| Same, with `(0x19)` | No `on_passkey_request` answered — check the `ble_client` MAC |
| `ESP_GATT_NO_RESOURCES` in the proxy log | Connection slots overrun — see `max_connections` above |

The proxy reports a bare number (`Pairing failed due to error: 80`); the integration
decodes it into the SMP reason above and logs what to change.

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

For the Energy dashboard, use *Grid meter energy* for grid consumption. For the house
alone, run *House power* through Home Assistant's **Integral** helper (Riemann sum,
method *left*).

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
Bluetooth path, radio profile, passcode, Power Boost meter and link speed.

