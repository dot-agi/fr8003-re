# 07 — Proprietary 2.4 GHz dongle link (FR8003A)

## The pivotal finding: the PHY is in mask ROM, not this flash

The proprietary 2.4 GHz radio link layer — RF/synth init, GFSK modulation,
whitening, CRC, sync/access-address, the channel plan and any hopping — is **in
the FR8003A mask ROM** (`0x00000000–0x00020000`), which this flash dump does **not
contain**. The flashed application performs **zero** direct RF programming.

Proof (all confirmed, re-verified against the bytes):
- The RF-SPI base `0x500F0000` (FRSPIM) and the GPIO data bases
  `0x50060000/0x50064000` occur **0 times** in the entire 512 KiB.
- The one `0x50010000` ("modem") reference is a secondary serial/debug channel,
  not the baseband.
- FreqChip's proprietary-2.4G driver is a ROM object literally named `rf_simu.c`
  (present in the SDK ROM symbol table, absent from flash). `rf_em_init`,
  `rf_init_controller`, etc. are all ROM symbols.

The app talks to the radio only by allocating/sending **kernel messages** to the
ROM BLE controller (`ke_msg_alloc` = ROM `0x0000aaa5` via veneer `0x10002298`;
`ke_msg_send` = ROM `0x0000ab19`). To a from-scratch stack the radio presents as
*"advertise with identity X / scan for address Y"* — the modulation underneath is
ROM's business.

**Consequence:** the link/pairing/HID-transport **contract** (addresses,
identities, framing, report formats) is fully recoverable from this dump; the
**on-air PHY constants** (bitrate, sync word, channel/hop list) are not — they
need the mask-ROM image or a dongle-side RE / on-air sniff. Strong evidence points
to BLE-style **1 Mbps GFSK** carrying the same HID payloads.

## Device identity & pairing (NVDS)

This is the richest recoverable layer, decoded from the builder at `0x10016c64`
and verified against the live NVDS bytes. **Per-unit device addresses are redacted (`xx`) in this public release; the byte structure and the forced-`0xC0` static-random signature are preserved.**

### Config / identity struct — NVDS flash `0x7c000`, mirrored to SRAM `0x11004ae8`

| Offset | Field | This unit | Meaning |
|---|---|---|---|
| `+0x000` | magic | `5A5A5A5A` | "config valid" |
| `+0x004` | active slot | `00` | selects the active identity (0–5) |
| `+0x005` | flag | `01` | broadcast self-address in adv data |
| `+0x006` | **self address** | `xx xx xx xx xx c0` | 48-bit BLE **static-random** (top bits forced `11`) |
| `+0x00c` | **6-slot addr table** | 7 B/slot | `[xx xx xx xx 0X c0][flag]`, X = slot index |
| `+0x037` | name table | 19 B/slot | slot0 `HS_KB_DG`, slot1 `Akko 5075-1`, slots 2–5 `HID_UART` |
| `+0x0a9` | manufacturer | UTF-16 `AKKO` | len-prefixed |
| `+0x0d8` | product | UTF-16 `Akko 2.4G VIA KB` | len-prefixed |
| `+0x10c` | sleep timeout | `1e` (30) | idle timeout ×60; `0x80` = never (not a channel) |

Every address has byte 5 = `0xC0` (top bits `0b11`) — the firmware forces this
(`orr r0,#0xc0` at `0x10016e4c`), the BLE static-random signature. **The 2.4 GHz
link reuses BLE addressing.** The six slot addresses differ only in byte 4 (the
slot index) and are the keyboard's own **per-host identities** (a distinct BD_ADDR
per host slot — standard for multi-host wireless keyboards).

### Bonding database — NVDS flash `0x7d000–0x7e000`

A record manager (init `0x10016e5e`) holds a live BLE bond: peer address
`xx:xx:xx:xx:xx:xx` with ~16 bytes of key material (LTK), paired against the
keyboard's own slot-1 identity `xx xx xx xx 01 c0`.

### RF calibration — NVDS flash `0x7f000`

A `FREQUUU…CHIP`-bracketed high-entropy blob: factory RF trim/calibration (and
possibly device keys). Opaque production data.

## Mode select — the WB32 owns the switch

**The radio never reads the rear 3-position BLE/2.4G/USB switch.** The GPIO data
bases occur 0 times in the image; there is no GPIO port read that could sample it.
The switch is wired to the **WB32**, which commands the radio's mode over UART via
the control channel below.

The radio-side mode/power logic is an event state machine at `0x10016ef8` (state
byte `0x11003b78`) — a 5-entry `tbb` for start / stop / relink / UART-init /
radio-on. **There is no PHY-level "BLE vs 2.4G GFSK" branch in the app**; the cases
are power/link-state, not modulation. The BLE-vs-2.4G distinction the app expresses
is *which identity/slot is active* plus whatever start command the WB32 issues; the
actual PHY choice is a ROM-side decision.

## Host ↔ radio control channel (BLE GATT)

Separate from the UART "md" frames ([`05-hid-uart-protocol.md`](05-hid-uart-protocol.md)),
a BLE host writes a vendor GATT characteristic, parsed at `0x10016c1e`:
frame `[0xAA][len][type][payload…]`:

- `0xA0` → sets a mode/state byte and posts an internal event.
- `0xA1` → **the raw-HID bridge**: on a 32-byte payload, repackages it as the
  `[0xAF][0x60][32]…` UART frame to the WB32.
- `0xA2` → posts a 1-byte control command.

Replies to a BLE host are built as `[0x05][type][payload]` GATT notifications:
`0xAB` manufacturer, `0xAC` product, `0xAD` VID/PID, plus a raw-HID forwarder. The
full USB HID report descriptor is in flash at `0x10017410` (boot-kbd ID 1, NKRO
ID 2, vendor raw-HID Usage-Page 0xFF00 ID 3, mouse ID 4, consumer ID 5, system
ID 6).

## Confidence & open questions

**Confirmed:** SRAM = `0x11000000`; RF/PHY entirely in mask ROM (no flash
frspim/modem/channel/hop code); the NVDS identity struct with 6 static-random slot
addresses; the bond DB; the RF-cal blob; the radio reads no switch GPIO; the
event-driven mode state machine; the GATT control channel + raw-HID bridge.

**Inferred:** on-air PHY = BLE 1 Mbps GFSK; the slot addresses are per-host
identities; the 2.4 GHz link carries the same HID payloads over a BLE-style PDU.

**The central undecidable from this dump:** is "2.4G mode" a distinct proprietary
PHY (ROM `rf_simu`, connectionless GFSK) or just a fixed-address BLE connection to
the dongle? No app-side discriminator exists — both are ROM-triggered.

**Next step to close the gap:** dump the FR8003A **mask ROM** (`0x0–0x20000`) via
ROMBOOT (the same CON3 path used for the flash), or RE the bundled dongle's own
firmware. The GFSK/channel/hop constants provably reside there, not in this flash.
