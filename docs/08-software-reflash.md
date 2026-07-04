# 08 — Software backup & re-flash (no case-opening)

How to back up and re-flash the radio through software, over the existing USB
cable, **without opening the keyboard** — and what the firmware does and doesn't
allow.

## The problem

The proven dump/restore path (`tools/`) speaks the radio's ROMBOOT protocol over
its CON3 UART. To reach ROMBOOT without CON3, the WB32 (running keeberry) would
bridge USB ↔ the radio's UART — but first it has to get the radio *into* ROMBOOT,
which needs a reset. keeberry drives the radio **only over UART3 (PC10/PC11) — there
is no radio reset or power-enable line** it can toggle. So the reset has to come
from either an `md` command the radio honours, or a full power-cycle.

## Is there an `md` soft-reset? — No (traced)

A dedicated disassembly trace answered this exhaustively:

- **No `md`/UART opcode resets the radio.** The complete WB32→radio dispatch —
  every opcode including the previously-unidentified `0xA7`/`0xB3`/`0xB4`, and every
  DEVCTRL (`0xA6`) sub-command — does HID-report forwarding, connection-mode and
  pairing control, string/VPID storage, a raw **echo** (`0xAF`), and status
  replies. None reset, reboot, erase firmware, enter DFU/ISP, or set a
  stay-in-bootloader flag. (`0xB3` merely clears hardware-register bits at
  `0x400000D0`.)
- **The one software reset is BLE-side, not `md`.** The image contains exactly one
  `SYSRESETREQ` (`0x10010240`), inside a **config-region self-heal** routine (the
  FREQCHIP config header at flash `0x1007F000`; if it is magic-valid but its
  enable-byte ≠ `0x55` and non-blank, the routine erases it and resets). It is
  reached only from the BLE kernel message table (`0x10018114`, tag `0x0D00`) —
  over-the-air, never from `md`. The BLE **OTA `REBOOT`** likewise resets via
  `platform_reset_patch`, also BLE-side.
- No app veneer to the ROM `platform_reset`/`app_boot`, no PMU `FT_REBOOT` write,
  no deliberately-armed watchdog reset.

`SYSTEM_OPTION_DISABLE_HANDSHAKE` is clear (`system_option=0x024f040f`), so a reset
*would* re-enter ROMBOOT — but stock firmware gives the WB32 no way to pull that
trigger. **A WB32-triggered reset would require adding one to the radio firmware**
(a new DEVCTRL sub that hits the `SYSRESETREQ`, or arms the watchdog). That is a
bootstrap: the *first* flash needs a power-cycle; custom firmware can then add an
`md` "reset-to-ROMBOOT" so later updates need none.

## Method A — keeberry USB ↔ ROMBOOT bridge (replug-triggered)

**Robust: full dump *and* flash.** Reuses the ROMBOOT protocol and `tools/`.

1. The configurator sends a kcp command → keeberry persists a one-shot **radio-ISP**
   flag (mirroring its existing wb32-dfu entry in `boot.rs`).
2. keeberry prompts a **USB replug** — the only way, absent a reset line, to
   co-power-cycle the WB32 *and* the radio together.
3. On that boot, keeberry sees the flag and — before USB, before anything —
   answers the radio's power-on `freqchip` banner with `FR8000OK`, catching it in
   ROMBOOT command mode.
4. keeberry then bridges ROMBOOT frames over the existing kcp/raw-HID pipe (no new
   USB class needed).
5. The host runs the same `dump.py` / `restore.py` through a thin kcp-HID transport
   shim → full 512 KiB, CRC-verified backup and brick-safe restore.

One cable replug per session; no case-opening.

## Method B — BLE OTA (wireless)

**Backup strong, flash constrained.** The radio's BLE OTA service
([`06-ble-ota-protocol.md`](06-ble-ota-protocol.md)) has flash-**read** ops
(`READ_DATA`, `READ_MEM`), so a BLE central can dump the flash **wirelessly — no
replug, no USB**. Writes go through `WRITE_DATA` (bank B only — cannot overwrite the
running image) + `REBOOT`, so a full re-flash is dual-bank-limited and gated by the
still-open OTA verifier questions. The OTA service is in the GATT table and its RX
characteristic is unauthenticated at the ATT layer; whether the app gates the
commands behind an update mode is the one reachability detail to confirm.

## Recommendation

- **Robust dump + flash without opening:** Method A (replug bridge) — the reliable
  path, reusing our tooling end-to-end.
- **Wireless backup bonus:** Method B, once reachability is confirmed.

The replug is a one-time bootstrap; the first custom radio firmware can add an
`md` reset-to-ROMBOOT command so every subsequent backup/flash needs no replug.
