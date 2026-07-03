# 06 — BLE OTA service (FR8003A)

The radio runs a lightly-customized copy of **FreqChip's stock `ble_ota` profile**
(SDK `components/ble/profiles/ble_ota/`). The dispatch in `app_otas_recv_data`
(VMA `0x10002f2c`) reproduces the SDK's `ota_cmd_t` enum exactly, and the format
strings match the SDK verbatim. Akko's additions: a 5-second watchdog and a
CRC-32 commit gate.

**Bottom line: custom firmware is deliverable over this path.** It is
**integrity-checked (CRC-32) but not authenticated (no signature)** — a
well-formed FR801x image with a correct CRC-32 is accepted. There is also an
unauthenticated arbitrary-memory-write opcode (`WRITE_MEM`).

Image windows in the disassembly: `0x00xxxxxx` = boot ROM · `0x10xxxxxx` = flash
XIP · `0x11xxxxxx` = RAM (`ram_code`, zeroed in a static dump). Confidence tags:
**[C]** confirmed · **[I]** inferred.

## GATT service

Client writes commands to **RX**, device replies via **Notify**. UUIDs below are
the canonical 128-bit form (on-air bytes are byte-reversed).

| Role | 128-bit UUID | Properties |
|---|---|---|
| Service | `02F00000-0000-0000-0000-00000000FE00` | Primary |
| Version Info | `…00000000FF03` | Read |
| Notify (responses) | `…00000000FF02` | Read + **Notify** |
| TX | `…00000000FF00` | Read |
| **RX (command input)** | `…00000000FF01` | **Write** |

The RX write characteristic carries `GATT_PROP_WRITE` with **no `GATT_PERM_*`
authentication** in the att table → writable without bonding at the ATT layer.
[C] (Whether the Akko link additionally gates on pairing is an open question.)

## Command protocol

Handler `app_otas_recv_data` @ `0x10002f2c`. **Command header** (written to RX):
`opcode`(u8) · `length`(u16 LE) · payload. **Response header** (notified):
`result`(u8; 0=OK,1=ERR,2=UNKNOWN) · `org_opcode`(u8) · `length`(u16) · payload.

| Op | Name | Payload | Effect |
|---|---|---|---|
| 0 | `NVDS_TYPE` | — | returns storage type |
| 1 | `GET_STR_BASE` | — | returns write base = `image_size+0x2000 = 0x27000`; seeds cursor |
| 2 | `READ_FW_VER` | — | returns `jump_table.firmware_version` |
| 3 | `PAGE_ERASE` | `base`(u32) | 4 KiB erase; **disconnects if `base < 0x27000`** |
| 4 | `CHIP_ERASE` | — | no-op |
| 5 | `WRITE_DATA` | `base`(u32),`len`(u16),`data` | flash write; **contiguity-checked** |
| 6 | `READ_DATA` | `base`(u32),`len`(u16) | flash read |
| 7 | `WRITE_MEM` | `base`(u32),`len`(u16),`data` | **memcpy to arbitrary address** (no check) |
| 8 | `READ_MEM` | `base`(u32),`len`(u16) | read arbitrary address |
| 9 | `REBOOT` | `argA`(u32),`argB`(u32) = image length + expected CRC-32 | verify + commit + reset |

**Session lifecycle:** the first RX write arms a 5000 ms one-shot watchdog
(`ota_start` → `os_timer_ota_cb`); each command feeds it; expiry aborts with
`OTA_TIMOUT`. `ota_stop(status)` maps 0/1/2 →
`OTA_TIMOUT`/`OTA_ADDR_ERROR`/`OTA_CHECK_FAIL`. [C]

## Flash-write path + address gates

Dual-bank A/B updater. Erase/write execute from RAM `ram_code` (can't run from
flash while erasing it). With `image_size = 0x25000`:

- **`GET_STR_BASE`** seeds the write cursor to `0x27000` (`0x25000 + 0x2000`). [C]
- **`WRITE_DATA`** accepts `base` only if it equals the cursor or `cursor+prev_len`
  — **strictly contiguous, ascending from `0x27000`**. A non-matching base →
  `OTA_ADDR_ERROR`, abort. [C]
- **`PAGE_ERASE`** erases only if `base ≥ 0x27000`; otherwise `gap_disconnect_req`. [C]

**Net writable region via the flash path: bank B only, contiguous from `0x27000`.**
The running image (bank A, `[0, 0x25000)`) and the boot header are **protected** —
you cannot overwrite them through `WRITE_DATA`/`PAGE_ERASE`. [C]

**Escape hatch — `WRITE_MEM` (op 7):** `memcpy(base, &packet[9], len)` with **no
range check** → arbitrary write to any address (incl. RAM) → code execution. Stock
SDK behavior, unmodified. [C]

## CRC-32 verification

On `REBOOT`: read the new image's 100-byte header at `0x27000`, compute **CRC-32
via the ROM `crc32()`** seeded `0xFFFFFFFF` over the bank-B body, then call a
RAM-resident verify fn with the image length + expected CRC. Pass → write the boot
descriptor to flash `0x1000` (commit), `crc32 check success`, reset. Fail →
`crc32 check fail`, `OTA_CHECK_FAIL`, no commit. [C]

**Polynomial: standard IEEE-802.3 / zlib CRC-32.** The lookup table at file
`0x17adc` is the canonical reflected table (`table[128]=0xEDB88320`). [C] Which
`REBOOT` arg is CRC vs length, and the ROM routine's final-XOR convention, are
[I] (the verify fn is `ram_code`, zeroed in a static dump).

## Can we push custom firmware?

Two independent, unauthenticated avenues — one **proven**, one **mechanism-proven
but not verified end-to-end**:

1. **Persistent flash OTA (intended path) — mechanism proven, final acceptance
   unverified.** The path is: connect → `GET_STR_BASE` (device says write at
   `0x27000`) → `PAGE_ERASE` bank B → stream a valid FR801x image with `WRITE_DATA`
   **contiguously from `0x27000`** (each chunk within the 5 s watchdog) → `REBOOT`
   with the image length and its CRC-32 (poly `0xEDB88320`, seed `0xFFFFFFFF`). The
   unauthenticated RX, the writable-region checks, the CRC table, and the commit
   path are all confirmed from the binary, and there is **no code signature** (only
   CRC-32 integrity). What is **not** yet confirmed is the final acceptance: the
   verify function is `ram_code` (`0x11001a59`, zeroed in a static dump), so the CRC
   arg order / final-XOR and the `firmware_version ≥ current` rule are unresolved.
   So custom firmware *should* flash — pending those details on a live device.
2. **`WRITE_MEM` (op 7) — proven.** Unauthenticated arbitrary RAM write with no
   bounds check → redirect execution. Full control without even a valid image.

This characterizes the earlier "radio has a BLE OTA" note: a real wireless channel,
complementary to the CON3 ROMBOOT path used to dump the chip.

## Confidence & open questions

- **CONFIRMED:** service/char UUIDs; the 10-opcode protocol + packet layout; 5 s
  watchdog; contiguous-write enforcement + `0x27000` base; bank-A/header
  protection; `WRITE_MEM` arbitrary write; CRC-gated commit; CRC-32 = IEEE/zlib
  `0xEDB88320` (table-proven); no ATT-layer auth on RX.
- **INFERRED:** exact A/B geometry and the `+0x2000` rationale; which `REBOOT` arg
  is CRC vs length; CRC final-XOR; the boot-descriptor-at-`0x1000` hand-off.
- **Resolve before an OTA attempt:** (1) does the Akko link require pairing before
  RX writes are honored? (2) pin the RAM verify fn (dynamic probe) to fix CRC
  seed/XOR + arg order; (3) confirm the `firmware_version ≥ current` rule; (4) is
  the OTA service advertised in normal use or only in an update mode?
