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
it — including for the link the proxy opened.

Add this to the proxy's ESPHome config, using your charger's MAC and passcode:

```yaml
esp32_ble:
  io_capability: keyboard_only   # default is "none" => Just Works only

esp32_ble_tracker:

bluetooth_proxy:
  active: true

ble_client:
  - id: wallbox_passkey
    mac_address: 54:64:DE:92:BC:7C
    auto_connect: false          # never connect; only answer the passkey
    on_passkey_request:
      then:
        - ble_client.passkey_reply:
            id: wallbox_passkey
            passkey: 123456
```

Still enter the passcode in Home Assistant as well: it is what makes the integration
ask the proxy to start pairing (`bluetooth_device_pair`, i.e. `esp_ble_set_encryption`
on the ESP32) instead of assuming an unencrypted link. Requires ESPHome 2024.3.0 or
newer on the proxy. Note that the helper `ble_client` consumes one of the ESP32's
connection slots, so raise `esp32_ble_tracker: max_connections` if the proxy is
already fully booked.

Chargers without a passcode keep working over a proxy with no extra configuration.

## Implemented features
 - charger status
 - lock / unlock
 - charge current
 - start / stop charging (only available while charging/paused)

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