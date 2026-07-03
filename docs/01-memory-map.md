# 01 — Memory map

## Address spaces (FR8003A — confirmed by reverse engineering)

The disassembly resolved the exact map (the generic SDK scatter file, which shows
a different chip variant at `0x20000000`, does **not** apply — the FR8003A puts
SRAM at `0x11000000`).

| Region | Base | Notes |
|---|---|---|
| Mask ROM | `0x00000000–0x00020000` | BLE controller + host stack + the proprietary 2.4G driver (`rf_simu.c`) + ROMBOOT. **Not in this dump.** |
| Flash XIP (cached) | `0x10000000` | the 512 KiB in `image/` executes in place here |
| Flash direct (QSPI DAC) | `0x01000000` | raw QSPI window |
| **SRAM** | **`0x11000000`** | proven by an early-startup/veneer literal `0x110001c4`, `stack_top` `0x11004a8c`, and config mirror `0x11004ae8` |
| Peripherals | `0x50000000` | UART0 `0x50050000`, UART1 `0x50058000`, MODEM `0x50010000`, TRNG `0x500C0000`, RF-SPI (FRSPIM) `0x500F0000`, GPIO-AB `0x50060000`, GPIO-CD `0x50064000` |

**XIP rule used throughout this repo:** flash file offset `N` = CPU address
`0x10000000 + N`. So the entry pointer `0x10002d05` is flash offset `0x2d04`.

The app runs XIP from flash and calls into the mask ROM by fixed address through
small veneers (e.g. `ke_msg_alloc` = ROM `0x0000aaa5` via `0x10002298`). It never
touches RF registers directly — all radio work is ROM kernel messages.

## Flash layout (from `tools/segment_map.py`)

| Range | Size | Contents |
|---|---|---|
| `0x00000–0x01000` | 4 KiB | jump_table boot header + config (mostly zero, e~0.17) |
| `0x01000–0x02000` | 4 KiB | zero padding (BLE-OTA writes the committed boot descriptor here) |
| `0x02000–0x1c000` | 104 KiB (106496 B) | **app code + data** — plaintext Thumb-2, e~6.54 |
| `0x1c000–0x1d000` | 4 KiB | trailing config/data (e~1.02) |
| `0x1d000–0x7c000` | **380 KiB** | **erased (0xFF)** — free; the OTA "bank B" write region starts at `0x27000` |
| `0x7c000–0x7d000` | 4 KiB | **NVDS config / identity** (self + 6 slot addresses, names, VID/PID) |
| `0x7d000–0x7e000` | 4 KiB | **NVDS bonding DB** (paired-host addresses + keys) |
| `0x7e000–0x7f000` | 4 KiB | erased (0xFF) — gap between the NVDS regions |
| `0x7f000–0x80000` | 4 KiB | **NVDS RF calibration** (`FREQUUU…CHIP` trim blob) |

The NVDS regions are decoded in [`07-24ghz-link.md`](07-24ghz-link.md). The actual
firmware occupies only the bottom ~112 KiB; the large erased span is real headroom
(and the OTA updater's second bank).

Regenerate: `python3 tools/segment_map.py` (use `-b 0x200` for finer blocks).
