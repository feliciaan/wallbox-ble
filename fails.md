# What went wrong in this PR

Mistakes made while building BLE passcode pairing, proxy support and the gateway
features, and what each one taught. The CHANGELOG and README describe only the end
result; this file is the record of how we got there.

## Pairing

### "A proxy cannot pair with a passcode"
- **Claim:** passcode pairing is impossible through an ESPHome proxy, because no API
  message carries a passkey.
- **Reality:** `esp32_ble` fans every GAP security event out to all `ble_client`s, and
  `esp_ble_passkey_reply()` is keyed by BD address. A `ble_client` with
  `auto_connect: false` can answer the passkey for the proxy's own link.
- **Lesson:** "no API for it" is not "impossible"; read how the events are dispatched.

### Helper `ble_client` overran the connection slots
- The extra `ble_client` takes a slot, so with three proxy slots the fourth client
  failed with `ESP_GATT_NO_RESOURCES`. Fixed with `max_connections: 4`.
- **Lesson:** validate proxy YAML with `esphome config`, which warns about it.

### Pairing keyed off a failing `start_notify`
- **Assumption:** the charger refuses the notification CCCD write until the link is
  authenticated, so a failing `start_notify` triggers pairing.
- **Reality:** this charger accepts the CCCD write unauthenticated and enforces
  authentication only on the command characteristic. Pairing never ran; every write
  failed with `Insufficient authentication`.
- **Fix:** pair as soon as the link is up, and pair + resend on an authentication
  error from a write.

### Overlapping pairing requests
- Polling started before setup finished, so a rejected write retried pairing while
  the connect path's own pairing request was in flight. The proxy answers once per
  `AUTH_CMPL`, so the second request hung for 30 s, then the forced reconnect dropped
  the link under it.
- **Fix:** one pairing request per link behind a lock, result cached; no reconnect on
  an authentication error (reconnecting cannot authenticate a link).

### Recommended `auth_req_mode: bond_mitm`
- `bond_mitm` clears the Secure Connections bit, so the ESP32 fell back to LE legacy
  pairing and the charger answered SMP 0x03, the same code as a missing
  `io_capability`. Cost a full round of field logs.
- **Fix:** `sc_mitm_bond`, which is also ESP-IDF's default.
- **Lesson:** check the default before recommending a non-default value.

## Power Boost meter

### Power read from `p1` without a voltage reference
- Grid power came from the meter's `p1`, which is 0 when the meter has no voltage
  reference wired. Switched to current × voltage, then, once the voltage was wired,
  back to the meter's measured power with current × voltage as fallback.
- **Lesson:** ask how the meter is wired before choosing a field.

### "Lifetime charged energy" from `r_sta.grid`
- Added a sensor for a field that Pulsar Max 6.13.6 does not send. The reference
  gateway's `grid` lives in `r_dat` and is the session's grid energy. Removed.
- **Lesson:** map fields against a real reply, not the reference code alone.

## Request handling

### Cancellation fix exposed a too-short reply timeout
- A pre-existing bare `except:` swallowed `CancelledError`, so the 2 s reply timeout
  never fired. Fixing that made the timeout real, and through a proxy a full status
  reply takes longer than 2 s, so every read timed out and all entities stayed
  unavailable.
- **Fix:** separate timeouts for the first piece, the gap between pieces, and the
  whole reply.
- **Lesson:** when fixing a bug that disables a timeout, check the timeout's value.

### More reads per poll without serialising requests
- The new reads took a poll from 1–2 requests to about 11. Requests were not
  serialised, so their 20-byte chunks interleaved on the charger's serial link and it
  stopped answering; `ready` was true before setup finished, so polling also raced the
  connect sequence. Every connect and self-heal reload collided with itself
  ("BLE unrecoverable for >180s" every 3 minutes, seen at `093d970`).
- **Fix:** one request in flight at a time; `ready` only after setup.

### Session login tried only once
- `suser` ran once after connecting. If that status read was slow, the session was
  never logged in. Now completed on the first successful status read.

### 2 s write timeout through a proxy
- Each chunk write waits for an ATT response relayed over Wi-Fi; 2 s was hit
  routinely, and each hit forced a reconnect, after which the charger stopped
  advertising for minutes ("comes back for a few seconds, then fails"). Now 10 s.

### Late reply tails poisoned the next request
- A reply that arrived after its timeout landed in the next request's buffer. The
  parser only resynced after a complete message, so that request failed too.
- **Fix:** a resyncing message splitter and sequential request ids.

### One missed status reply blanked the device
- A single unanswered `r_dat` set every entity unavailable; when the next poll also
  did the slow settings read, the gap was 30 s. Now the last values are kept until
  three polls in a row are missed.

## Testing

### Tests passed while the field failed
- The first simulation used instant replies and writes, so it could not reproduce
  any of the request-handling failures above. It caught them only once it replayed a
  field log: per-chunk latency, slow writes, late and stalled replies, and real
  payloads, with timers scaled by one factor so their ratios match production.
- **Lesson:** model the slow transport first; derive simulation timings from logs.
