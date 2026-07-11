# 09 — Mask ROM

The **128 KiB mask ROM** (`0x00000000–0x00020000`) — the half of the chip's
firmware the flash dump ([`01`](01-memory-map.md)) does not contain. Dumped for RE
only: factory-mask-programmed silicon, identical on every FR8003A, read-only, not
re-flashable.

Image: `image/fr8003-maskrom.bin` (public repo: SHA-256 + manifest only — FreqChip's
ROM, not ours to redistribute).

**Cross-validated against the FR8000 SDK symbol map** (a **RivieraWaves BLE
stack**), which matches this ROM at anchors like `ke_msg_alloc=0xaaa5` and
`rwble_isr=0x1a6c1` — so the symbols below are *named from the SDK*, not guessed.
Public mirror: `gitee.com/CuZn-come-on/fr8000`.

## Why a second image

The FR8003A splits its firmware across two physically separate memories:

- **Mask ROM** @ `0x0` — FreqChip's, immutable: the BLE stack, boot/ROMBOOT, and the
  **RF-init framework**. *This document.*
- **QSPI flash** @ `0x10000000` — Akko's application. As it turns out, the app also
  carries the **board-specific MODEM register image** and frequency control (see
  [The radio](#the-radio--how-rf-is-actually-reached)). `fr8003-flash.bin`,
  `01`–`08`.

The running system is ROM **+** flash together. Neither alone is "the complete
firmware," and — corrected here — the PHY configuration is *split* across both.

## How it was dumped

`READ`/flash opcodes take 0-based *flash* offsets and can't reach `0x0`;
`OP_READ_RAM` (`0x0A`) takes an **absolute address**, so it reads the ROM.
`tools/dump_rom.py`: enter ROMBOOT over CON3 (same rig as the flash dump), then
`READ_RAM` across `0x0..0x20000`. No ROM CRC op exists, so integrity = **two dumps,
matched SHA-256** (`cc0db683…d7ca`). Non-destructive; the ROM cannot be written back.

## What it is — a RivieraWaves BLE stack

Standard RivieraWaves-style BLE controller+host: `lld` / `llc` / `llm` / `rwip` +
the hardware scheduler, plus boot / ROMBOOT. **There is no distinct proprietary-2.4
GHz MAC in the ROM.** `rf_simu` (`0x1a558–0x1a67f`) is only a small RF-adaptation
shim (`rf_em_init` / `rf_init_api` / `rf_init_controller` / `rf_init_rom` + RSSI /
TX-power helpers) sitting just before `rwble_init@0x1a681` — its filename is not
evidence of a proprietary link.

## Layout

| Range | Contents |
|---|---|
| `0x00000–0x00098` | vector table (MSP `0x1100c000`, Reset `0x6e4`, SVC `0x720`, IRQ0 `0x1a6c0`); boot code from `0x98` |
| `0x00098–0x1d560` | code: BLE stack, ROMBOOT, boot |
| `0x1d560–0x1e830` | **crypto/checksum constants**: P-256 base point + ECC window table, CRC-32 table, SHA-256 `K[64]`, HCI format strings, scatter-load descriptors, initial SRAM images |
| `0x1e830–0x20000` | zero/padding |

`MSP = 0x1100c000` is a *selected stack boundary*, **not** SRAM top: the FR8000 has
**56 KiB system SRAM** (`0x11000000–0x1100dfff`) plus a separate 8 KiB BLE exchange
RAM. The `0x1e000` region is **crypto/checksum tables, not RF calibration** — its
apparent entropy is the CRC/SHA/ECC data.

## ROMBOOT — confirmed from silicon

The handler at `0x22d4` emits `freqchip`, reads 8 bytes, `memcmp`s against
`FR8000OK`, replies `ok`. Adjacent string layout:

```text
0x1c870  "ok"       0x1c872  "MAGIC"      0x1c877  "freqchip"      0x1c87f  "FR8000OK"
```

This validates the recovered ROMBOOT serial-download protocol against the ROM.

## Boot decision — a PMU wake-magic check (not eFuse)

Reset (`0x6e4`) runs `frspim_init(3); frspim_rd(channel=0, address=0x5e)` and
compares the byte to `0xc3`. Per the FR8000 header, reg `0x5e` =
`PMU_REG_SYSTEM_STATUS_SW` and `0xc3` = `PMU_SYS_WK_MAGIC` — a **retention/wake-state
check**, *not* eFuse[94] and *not* a security/variant fuse. FRSPIM (base
`0x50020000`) is a serial portal to the PMU PK/PD registers; control word: bit0 go,
bit1 done, bit2 channel, bits4:6 length, bit8 r/w, bits12:13 clock, bits16:23
address; `+4` write-data, `+8` read-data.

## Peripheral map (SDK-validated)

| Base | Identity | Evidence |
|---|---|---|
| `0x40000000` | **BLECORE** baseband/link-layer | `rwble_isr` reads INTSTAT `+0x10`, acks `+0x18`; RF init writes `+0x70..+0x9c` |
| `0x40004000–0x40005fff` | **8 KiB BLE packet/exchange RAM** | HW descriptors + packet buffers (not a UART/FIFO) |
| `0x50000000` | sysctrl / clocks / IOMUX | `system_set_port_mux` uses `+0x20 + 4*port` |
| `0x50010000` | TIMER0 (`+0x20` TIMER1) | — |
| `0x50020000` | **FRSPIM → PMU** serial portal | `frspim_init/rd/wr` |
| `0x50024000` | **MODEM / RF-transceiver bank** | APP-programmed (below) |
| `0x50028000` | eFuse | — |
| `0x50050000` / `0x50058000` | UART0 / UART1 | ROMBOOT + RX ISRs (IRQ9/IRQ10) |
| `0x50060000` / `0x50064000` | GPIO A/B / C/D | DATA + OutputEN |
| `0x500b0000` | **cache controller** | `system_enable/disable_cache` |
| `0x500f0000` | **QSPI0 APB** (not FRSPIM) | absence from ROM is irrelevant to RF |

## The radio — how RF is actually reached

The packet path is **MODEM `0x50024000` + BLECORE `0x40000000` + exchange RAM
`0x40004000`**, split ROM/APP:

- **ROM** does BLECORE + exchange-memory init + the `rf_init` framework
  (`rf_init_rom` chains `rf_init_api` / `rf_em_init` / `rf_init_controller`,
  programming BLECORE `0x40000070–0x9c`).
- **APP (flash)** supplies the board-specific part: `rwip_init` loads the `rf_init`
  callback from SRAM `0x110001b4`; Akko startup installs `rf_init_app` (flash
  `0x1001a3d0` → code `0x110026cc`), which **copies a 336-byte (84-word) MODEM image
  into `0x50024000–0x5002414f`**, enables RF clocks (sysctrl `0x50000004`), and
  programs BLECORE timing. Four `~0x150`-byte MODEM profiles live in APP data
  (`0x1001b908`…); startup selects profile 3 (profiles differ by only 3–5 bytes —
  likely tuning variants). `calib_set_freq_config` writes synthesizer bytes to
  `0x500240fd–0x50024102` for ~`2360–2511 MHz`, using a BLE channel-order table
  (`37,0..10,38,11..36,39`).
- The scheduler (`sch_prog_push`) builds 16-byte descriptors in exchange RAM
  (`0x40004000 + slot*0x10`) and arms them by writing `slot | 0x80000000` to BLECORE
  `0x40000100`.

So the PHY config is **split**: the ROM has the mechanism + framework; the APP flash
has the MODEM register image + frequency control. This **corrects
[`07`](07-24ghz-link.md)**, which claimed the app performs zero RF programming.

## Callable ROM ABI

The APP→ROM interface is the FR8000 absolute Thumb symbol map — **not** SVC (which
is a ROM **hot-patch** mechanism: `0x720` calls `svc_exception_handler@0x110001c4`,
which walks six `{ROM PC, replacement PC}` patch entries) and **not** `__jump_table`
(`0x110000a0`, just APP boot config). Useful entries:

```text
frspim_init 0x4ff1  frspim_rd 0x5009  frspim_wr 0x5045
lld_test_init 0x188b1  lld_test_start 0x188c5  lld_test_stop 0x18b29
rf_em_init 0x1a559  rf_init_api 0x1a57d  rf_init_controller 0x1a5b5  rf_init_rom 0x1a609
rwble_init 0x1a681  rwble_isr 0x1a6c1
rwip_driver_init 0x1a811  rwip_init 0x1a8d9  rwip_isr 0x1a971  rwip_schedule 0x1aadd
sch_prog_init 0x1bb2d  sch_prog_push 0x1bb5d
```

Writable seams: `rf_init` callback `0x110001b4`, SVC patch `0x110001c4`, `rwip_rf`
API struct `0x11000b54`.

## Owned-2.4 GHz feasibility

**Confirmed feasible now.** Custom firmware can install its own `rf_init`, load/modify
the MODEM register image, select RF frequencies, invoke the BLE RF/test/controller
routines, build exchange-memory descriptors, schedule its own TX/RX, and carry
**arbitrary payloads + MAC over BLE-compatible RF framing**. The PHY is
register-accessible, not ROM-opaque; the ROM entry points are a convenience layer,
not the only path to the hardware.

**Not yet proven:** fully arbitrary *non-BLE* framing — custom preamble/sync word, a
non-BLE packet grammar, custom CRC or whitening polynomial, arbitrary symbol
rate/modulation index, or raw continuous RX/TX outside BLECORE's descriptor model.
These may live in the 336-byte MODEM bank at `0x50024000`, but the offsets/semantics
are not yet identified.

**Verdict:** *owned BLE-compatible 2.4 GHz is feasible now; fully arbitrary non-BLE
PHY framing is plausible but unproven.* Practical path: reuse the FR8000 ROM/SDK ABI
+ the recovered `rf_init_app` sequence + a stock MODEM profile, and replace the
higher-level MAC. A ground-up arbitrary GFSK PHY needs another RE pass over
`0x50024000`, the BLECORE descriptor fields, and the frequency/test routines.

## Claims withdrawn (from earlier inline analysis / docs/07)

- the app performs *zero* RF programming — **false** (it programs `0x50024000`);
- `0x500F0000` = FRSPIM — it's **QSPI**; FRSPIM is at `0x50020000`;
- `0x50020000` = eFuse — it's the **FRSPIM/PMU portal**; eFuse is `0x50028000`;
- `0x500b0000` = RF-analog/PLL — it's the **cache controller**;
- `0x1e000` = RF calibration — it's **crypto/checksum tables**;
- SVC = a radio API — it's a **ROM hot-patch** mechanism;
- a distinct proprietary-2.4 GHz ROM MAC — **none**; RivieraWaves BLE + APP MODEM image.

## Open items

- The `0x50024000` MODEM register semantics (which bytes = sync word / CRC /
  whitening / symbol rate / modulation) — the depth a non-BLE PHY would need.
- The four MODEM profiles' exact selection semantics.
- Import the full FR8000 SDK symbol map for the complete named ROM ABI.
