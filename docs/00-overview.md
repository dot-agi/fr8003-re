# 00 — Overview

## What this is

The Akko 5075B keyboard runs on **two** microcontrollers:

- **WB32FQ95** — the main controller (STM32F103-class; **256 KB flash / 36 KB
  SRAM** per its DFU bootloader — more than the 128 KB often assumed, and the
  headroom for keeberry's wireless code). Runs [keeberry](../../keeberry): USB, key
  matrix, RGB. Drives the radio over UART (115200 8N1).
- **FR8003A** — the radio coprocessor analysed here. Does Bluetooth LE and the
  proprietary 2.4 GHz dongle link, and speaks HID-over-UART back to the WB32.

To give keeberry its own wireless we have to speak the radio's protocols (and,
ideally, extend or replace its firmware). This repo reverse-engineers the stock
radio firmware toward that goal.

## The chip

ARM Cortex-M3 @ 96 MHz, BLE 5.1 + proprietary 2.4 GHz, 512 KiB QSPI flash executed
in place (XIP) at `0x10000000`, **SRAM at `0x11000000`**, 128 KiB mask ROM at `0x0`
holding a RivieraWaves BLE stack + the RF-init framework and ROMBOOT (now dumped
and analyzed — see [`09`](09-mask-rom.md)). Built on the FreqChip **FR801x SDK** — see the README for how to fetch
it; it defines the exact structs, memory map, and ROM API this firmware links
against, and was the key to decoding everything here.

## Provenance of the image

`image/fr8003-flash.bin` — the full 512 KiB flash, pulled **non-destructively** over
the CON3 UART header through the ROMBOOT read path, verified two ways: the chip's
own CRC32 (`0xa2e03042`) matched the read-back, and SHA-256 `de48ca83…6695a37`. It
is unmodified factory firmware, and reads back **plaintext — not encrypted, not
read-locked**. (Regenerate with `tools/probe.py` + `tools/dump.py`.)

## Findings

| | |
|---|---|
| Image kind | valid FR801x **APP** image (`image_type=APP`, `checkword` verified) |
| Entry | pointer `0x10002d05` (thumb) → code at flash offset `0x2d04` |
| Encryption | none — plaintext ARM Thumb-2, overall entropy 2.19 bits/byte |
| Occupied flash | header `0x0`, app code+data `0x2000–0x1c000`, NVDS `0x7c000–0x80000` (erased gap at `0x7e000`) |
| Free flash | `0x1d000–0x7c000` — ~380 KB erased (also the OTA "bank B") |
| **HID-over-UART** | **fully reversed**, cross-confirmed byte-for-byte vs keeberry — the WB32↔radio contract ([05](05-hid-uart-protocol.md)) |
| **BLE OTA** | stock FreqChip profile; **unsigned, CRC-32-gated** — a wireless firmware-delivery path (mechanism proven, final acceptance pending) ([06](06-ble-ota-protocol.md)) |
| **2.4 GHz** | identity/pairing/HID transport recovered; RivieraWaves BLE core (ROM) + an **APP-supplied MODEM image** — owned BLE-compatible 2.4G is feasible ([07](07-24ghz-link.md) · [09](09-mask-rom.md)) |
| BLE identity | advertises `Akko 5075-1` / `HS-Bluetooth`; multi-host, static-random addressing |

## Documentation index

| Doc | Topic |
|---|---|
| [00](00-overview.md) | this overview |
| [01](01-memory-map.md) | address spaces + flash layout |
| [02](02-jump-table-header.md) | the decoded boot header |
| [03](03-firmware-features.md) | high-level feature map |
| [04](04-next-steps.md) | open questions + roadmap |
| [05](05-hid-uart-protocol.md) | **HID-over-UART** (WB32 ↔ radio) — the seam our firmware speaks |
| [06](06-ble-ota-protocol.md) | **BLE OTA** service — the wireless firmware-delivery path |
| [07](07-24ghz-link.md) | **2.4 GHz** link — identity, pairing, mode select |
| [08](08-software-reflash.md) | software backup & re-flash without opening the case |

## Status

Structural analysis complete and **all three protocols reverse-engineered** to
differing depths. The HID-over-UART contract is fully established (the highest-
confidence result, cross-checked against keeberry). BLE OTA is characterized
end-to-end — the delivery mechanism is proven, with only the RAM-resident verifier
details open. The 2.4 GHz transport and identity are recovered; its on-air PHY
constants remain in the mask ROM, which is the next dump target
([04](04-next-steps.md)).
