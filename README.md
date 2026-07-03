# FR8003A Radio Firmware — Reverse Engineering

Reverse engineering of the **Freqchip FR8003A** Bluetooth-LE / 2.4 GHz radio
coprocessor inside the **Akko 5075B** wireless keyboard — its factory firmware
dump, a full protocol analysis, and the tooling to dump, analyze, and reflash it.
The end goal is custom wireless for the [keeberry](../keeberry) firmware project.

---

## Overview

The Akko 5075B has **two** microcontrollers:

| MCU | Role |
|---|---|
| **WB32FQ95** (main) | USB, key matrix, RGB. Runs [keeberry](../keeberry). |
| **FR8003A** (radio) | Bluetooth LE + proprietary 2.4 GHz. Talks HID-over-UART to the WB32. |

They are wired together by a UART. To give keeberry its own wireless, we must
speak the radio's protocols and, ideally, extend or replace its firmware — which
requires understanding what the stock radio firmware does. This repo is that
understanding, plus everything needed to reproduce it.

**Status:** the factory firmware is dumped (CRC-verified), confirmed plaintext,
fully mapped, and **all three of its protocols reverse-engineered** — the
HID-over-UART contract fully; BLE OTA and the 2.4 GHz link with documented open
items. See [`docs/00-overview.md`](docs/00-overview.md) for the guided tour.

---

## The chip

| | |
|---|---|
| Part | Freqchip FR8003A (FR800x / "800X" family) |
| Core | ARM Cortex-M3 @ 96 MHz |
| Radio | BLE 5.1 + proprietary 2.4 GHz, on-chip balun |
| Flash | 512 KiB stacked QSPI, executed in place (XIP) at `0x10000000` |
| SRAM | `0x11000000` |
| Mask ROM | `0x00000000–0x00020000` — BLE stack, the 2.4 GHz driver (`rf_simu.c`), ROMBOOT |
| SDK | FreqChip FR801x SDK (see [Developing](#developing-on-it) for how to fetch it) |

Full address map and flash layout: [`docs/01-memory-map.md`](docs/01-memory-map.md).

---

## The firmware

The 512 KiB flash was pulled **non-destructively** over the CON3 UART header
through the chip's ROMBOOT read path. It is unmodified factory firmware and reads
back **plaintext — not encrypted, not read-locked**. The binary itself is **not
included in this repo** (it is Akko's proprietary firmware); `image/` keeps its
SHA-256 and manifest so you can dump and verify an identical copy — see
[`image/README.md`](image/README.md):

- A valid FR801x **APP** image (`image_type=APP`, `checkword=0x51525251` verified),
  entry at `0x10002d05`, boot header decoded against the SDK
  ([`docs/02`](docs/02-jump-table-header.md)).
- Advertises as `Akko 5075-1` / `HS-Bluetooth`; multi-host BLE with static-random
  addressing; a FreqChip RTOS + BLE stack; linked BLE Mesh code.
- Integrity: chip CRC32 `0xa2e03042` (hardware-computed, matched the read-back);
  SHA-256 `de48ca83…6695a37`.

### The three protocols

| Protocol | What it is | Status | Doc |
|---|---|---|---|
| **HID-over-UART** | the WB32 ↔ radio wire contract ("md" codec) | **fully reversed**, cross-confirmed byte-for-byte against keeberry | [05](docs/05-hid-uart-protocol.md) |
| **BLE OTA** | stock FreqChip firmware-update service | **unsigned, CRC-32-gated** wireless delivery path — mechanism proven, final acceptance pending | [06](docs/06-ble-ota-protocol.md) |
| **2.4 GHz** | the proprietary dongle link | identity/pairing/transport recovered; the on-air **PHY lives in mask ROM**, not this flash | [07](docs/07-24ghz-link.md) |

---

## Repository layout

```
image/   the dump's SHA-256 + provenance manifest (the binary is not redistributed)
docs/    reverse-engineering findings, one topic per file (start at 00)
tools/   read-only analysis scripts + the ROMBOOT dump/restore ISP client
```

---

## Developing on it

### Prerequisites

- **Python 3** (the analysis scripts are stdlib-only; the dump/restore ISP client
  needs `pyserial`: `pip install pyserial`).
- **arm-none-eabi-binutils** for `objdump` (`brew install arm-none-eabi-binutils`).
- **radare2** and/or **Ghidra** for interactive disassembly (optional).
- For dumping/reflashing a real chip: a **3.3 V USB-UART adapter** (or a Raspberry
  Pi's UART).

### Getting the FreqChip SDK (not bundled)

The SDK is FreqChip's, BSD-2-Clause licensed, and published by the vendor — so it
is **referenced, not vendored**. Clone it alongside this repo:

```bash
git clone https://github.com/qdfreqchip/FR801xH-SDK.git   # H family — jump_table.h, the ble_ota profile
git clone https://github.com/qdfreqchip/FR801x-SDK.git    # non-H family — LICENSE, extra driver/2.4G examples
```

The RE leans on: `components/modules/platform/include/jump_table.h` (the boot
header struct), the `*.sct` / `ldscript.ld` scatter files (memory map),
`components/ble/profiles/ble_ota/` (the OTA profile), and `driver_plf.h` /
`driver_frspim.h` (peripheral bases). The docs cite files by SDK-relative path.

### Reproduce the analysis (read-only)

```bash
python3 tools/segment_map.py                 # flash layout / entropy map
python3 tools/parse_jump_table.py            # decode the boot header
# full disassembly (XIP base 0x10000000, Thumb):
arm-none-eabi-objdump -D -b binary -marm -Mforce-thumb \
    --adjust-vma=0x10000000 image/fr8003-dump.bin | less
```

Every address cited in `docs/` is reproducible from that disassembly.

### Dump / restore a real chip

The keyboard exposes the radio's UART on the **CON3** header (pads, top→bottom:
`3.3V · TX · RX · GND`). Wire a 3.3 V UART to it (cross TX↔RX, common GND), then
power-cycle the radio to enter ROMBOOT — the ROM emits a `freqchip` banner and the
tools answer it.

```bash
python3 tools/probe.py   -p /dev/tty.usbserial-XXXX      # verify link + CRC faithfulness
python3 tools/dump.py    -p /dev/tty.usbserial-XXXX -o my-dump.bin
python3 tools/restore.py -p /dev/tty.usbserial-XXXX -i image/fr8003-dump.bin
```

`probe.py` and `dump.py` are strictly read-only and CRC-verify the read-back
against the chip's own CRC. `restore.py` is brick-safe (header-sector written last,
per-sector + final verification). `image/fr8003-dump.bin` is the proven-good
recovery point.

### Building custom firmware (the roadmap)

Two routes, detailed in [`docs/04-next-steps.md`](docs/04-next-steps.md):

1. **Interpose (no reflash):** keeberry drives the radio over the now-fully-known
   HID-over-UART contract — including capabilities the stock link exposes but
   keeberry doesn't yet use.
2. **Replace / extend (reflash):** build an APP image from the SDK carrying the
   jump_table header decoded here, and deliver it over the cable (CON3 ROMBOOT —
   proven) or over the air (BLE OTA — unsigned, CRC-32-only; mechanism proven,
   final acceptance pending). ~380 KB of flash is free.

The immediate next acquisition is dumping the **mask ROM** (`0x0–0x20000`) over the
same CON3 path — it holds the 2.4 GHz PHY constants that this flash does not.

---

## Provenance, scope & license

- **The firmware image is not included** in this public repo — it is Akko's
  proprietary firmware. `image/` keeps only its SHA-256 and provenance manifest;
  dump your own identical copy with the tooling and verify it (see
  [`image/README.md`](image/README.md)). This RE is of my own hardware, for
  **interoperability**.
- **The SDK** is FreqChip's (BSD-2-Clause) and is referenced, not included.
- **The tools and docs** in this repo are original work.

---

## Documentation index

| Doc | Topic |
|---|---|
| [00](docs/00-overview.md) | overview & guided tour |
| [01](docs/01-memory-map.md) | address spaces + flash layout |
| [02](docs/02-jump-table-header.md) | the decoded boot header |
| [03](docs/03-firmware-features.md) | high-level feature map |
| [04](docs/04-next-steps.md) | open questions + roadmap |
| [05](docs/05-hid-uart-protocol.md) | HID-over-UART (WB32 ↔ radio) |
| [06](docs/06-ble-ota-protocol.md) | BLE OTA service |
| [07](docs/07-24ghz-link.md) | proprietary 2.4 GHz link |
