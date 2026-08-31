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

> **A passcode-protected charger needs a local Bluetooth adapter.** Supplying a
> passkey means answering BlueZ's `RequestPasskey` from a D-Bus pairing agent, which
> only exists on the Home Assistant host. ESPHome Bluetooth proxies cannot do it:
> their `esp32_ble_client` handles only `ESP_GAP_BLE_SEC_REQ_EVT` (auto-accept) and
> `ESP_GAP_BLE_AUTH_CMPL_EVT`, never `ESP_GAP_BLE_PASSKEY_REQ_EVT`, so a proxy can
> only pair "Just Works". Chargers without a passcode keep working over a proxy.

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
   (a passcode-protected charger needs the HA host's own adapter — see above).
 - The bond is stored by BlueZ, so pairing happens once; removing the integration
   removes the bond again.
 - The charger accepts a single BLE connection at a time; if the Wallbox phone app
   is connected it may briefly block Home Assistant (and vice-versa).