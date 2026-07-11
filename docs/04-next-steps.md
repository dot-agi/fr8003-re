# 04 — Open questions & roadmap

## Resolved since the first pass

- **`0x11000000` = SRAM** (was "unidentified") — pinned by an early-startup/veneer
  literal (`0x110001c4`), the header's `stack_top_address` (`0x11004a8c`), and the
  config mirror (`0x11004ae8`).
- **All three protocols reverse-engineered** — the HID-over-UART contract fully
  established ([05](05-hid-uart-protocol.md)); the BLE OTA mechanism characterized
  ([06](06-ble-ota-protocol.md)); 2.4 GHz transport/identity recovered
  ([07](07-24ghz-link.md)).
- **NVDS largely decoded** — the identity/config struct, bonding DB, and RF-cal
  region ([07](07-24ghz-link.md)).
- **`image_size = 0x25000`** is the app partition size (drives the OTA bank-B base
  at `0x27000`), not the code byte count.

## Remaining open questions

- **The 2.4 GHz PHY constants** (bitrate, sync/access word, channel + hop plan) —
  they live in the **mask ROM**, not this flash. This is the single biggest gap.
- **OTA specifics needing a live device:** does the Akko link gate RX writes on
  pairing? which `REBOOT` arg is CRC vs length? the ROM CRC final-XOR convention.
- **Exact `jump_table_t` layout** for this build (the 8-byte delta) → the precise
  offsets of `firmware_version` and `system_option`.
- **Is the linked BLE Mesh code live or dead** SDK default?
- The `0x7f000` RF-cal blob contents and the `cfg+0x108` token.

## The frontier — next targets

1. **Dump the mask ROM (`0x0–0x20000`)** via ROMBOOT — the same CON3 path and the
   `tools/` ISP client used for the flash. This unlocks the 2.4 GHz PHY, the
   `rf_simu` driver, and the full ROM API. Highest-value next acquisition.
2. **Build custom radio firmware** from the FR801x SDK as an APP image carrying the
   jump_table header decoded in [02](02-jump-table-header.md), and deliver it via
   either the CON3 ROMBOOT write path (`tools/restore.py` territory) or the stock
   **BLE OTA** ([06](06-ble-ota-protocol.md)). The ~380 KB of free flash is the
   headroom; `image/fr8003-flash.bin` is the proven recovery point.
3. **Dynamic probing** on a live device to close the OTA/NVDS gaps above.

## Two proven routes to "build on the wireless"

- **Interpose (no reflash).** keeberry already speaks the HID-over-UART contract;
  [05](05-hid-uart-protocol.md) documents it fully, including capabilities keeberry
  doesn't yet use (host-suspend via `HOST_STATE`, and the extra `0xA7`/`0xB3`/`0xB4`
  commands). Much can be gained purely from the WB32 side.
- **Replace / extend (reflash).** Custom radio firmware is deliverable over the wire
  (ROMBOOT, cable — proven) or over the air (BLE OTA — unsigned, CRC-32-only; the
  mechanism is proven, final image acceptance pending the RAM-verifier details in
  [06](06-ble-ota-protocol.md)).
