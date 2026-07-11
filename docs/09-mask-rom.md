# 09 — Mask ROM

The **128 KiB mask ROM** (`0x00000000–0x00020000`) — the half of the chip's
firmware the flash dump ([`01`](01-memory-map.md)) does **not** contain. It holds
the boot / ROMBOOT loader, the BLE controller + host stack, and the low-level
radio driver (the 2.4 GHz PHY/MAC). It is **factory-mask-programmed silicon**:
identical on every FR8003A, read-only, and **not re-flashable**. Dumped for RE
only.

Image: `image/fr8003-maskrom.bin` (public repo: SHA-256 + manifest only — it is
FreqChip's ROM, not ours to redistribute).

## Why a second image

The FR8003A splits its firmware across two physically separate memories:

- **Mask ROM** @ `0x0` — FreqChip's, mask-programmed, immutable: the radio stack +
  boot. *This document.*
- **QSPI flash** @ `0x10000000` — Akko's application (keyboard logic,
  HID-over-UART, BLE profiles, NVDS). `fr8003-flash.bin`, decoded across `01`–`08`.

The running system is ROM **+** flash together: the flash app calls down into the
ROM for all radio work. Neither image alone is "the complete firmware." The flash
had the app but not the 2.4 GHz PHY — which is the reason this dump exists.

## How it was dumped

The ROMBOOT flash-`READ` opcode takes **0-based *flash* offsets**, so it only
reaches the QSPI flash — never the ROM at `0x0`. `OP_READ_RAM` (`0x0A`) instead
takes an **absolute address** and returns 4 bytes, so it reads the ROM.
`tools/dump_rom.py`:

- Enter ROMBOOT over CON3 (same rig + handshake as the flash dump — see the README
  and [`03`](03-firmware-features.md)), then `READ_RAM` across `0x0..0x20000`,
  4 bytes per transaction (~minutes).
- **Integrity:** there is no ROM CRC op (the ROM's CRC operates over the XIP
  window, not `0x0`), so faithfulness is proven by dumping **twice and matching
  SHA-256**. Both dumps: `cc0db683…d7ca`.
- Non-destructive and read-only; the ROM cannot be written back.

## Layout

| Range | Contents | Entropy |
|---|---|---|
| `0x00000–0x00098` | Cortex-M vector table (MSP `0x1100c000`, Reset `0x6e4`, SVC `0x720`); boot code begins at `0x98` | — |
| `0x00098–0x1e000` | code (Thumb-2): boot, ROMBOOT, BLE stack, radio driver | ~6.7 |
| `0x1e000–0x1f000` | data: register-init + calibration table | ~3.6 |
| `0x1f000–0x20000` | blank | — |

`MSP = 0x1100c000` places the boot stack at the top of a **48 KiB SRAM**
(`0x11000000`), refining the SRAM-size figure in [`01`](01-memory-map.md).

## ROMBOOT protocol — confirmed from silicon

The download handler at **`0x22d4`** implements exactly the protocol reversed from
the vendor tools (see the `fr_isp.py` header): it emits the `freqchip` banner,
reads 8 bytes over the UART, `memcmp`s them against the token **`FR8000OK`**
(strings at `0x1c870`: `ok` / `freqchip` / `FR800…OK`), and replies `ok` on a
match. This independently **validates the recovered ROMBOOT serial-download
protocol** — banner, handshake token, and reply — against the actual ROM.

## Boot decision (efuse gate)

`Reset` (`0x6e4`) sets MSP, then reads **efuse byte 94** via the efuse controller
(fn `0x5008`, base `0x50020000`) and compares it to `0xc3`, branching between two
boot paths. This is a fused boot-mode / variant / security gate — relevant to
whether (and how) the part can be locked.

## Peripheral map (ROM-observed)

Derived from actual register access in the ROM. This **refines the SDK-nominal map**
in [`01`](01-memory-map.md), which was inferred from the SDK header:

| Base | Block | Evidence |
|---|---|---|
| `0x50050000` | **UART0** | ROMBOOT handler + RX ISR (IRQ9, `0x1c670`) |
| `0x50058000` | **UART1** | RX ISR (IRQ10, `0x1c704`) |
| `0x50020000` | **efuse / OTP** | fn `0x5008` (index→bits[16:23], trigger bit0, poll, read `[+8]`) |
| `0x50060000` | **GPIO / IOMUX** | fn `0x50e8` (pin-function regs `0x20..0x30`) |
| `0x50000000` | **sysctrl / clocks** | early init; present in the `0x1e000` init table |
| `0x40000000` | **BLE / modem link-layer engine** | IRQ0 dispatcher `0x1a6c0` → event ISR `0x1a970` (status `[+16]`, clear `[+24]`; RX/TX/timer events) |
| `0x40004000` | **modem descriptor array** | 16 × 16-byte entries; ISR `0x1baa4` indexes `0x40004000 + (i<<4)`, state field at bits[3:5] |
| `0x500B0000` | command-sequenced peripheral — RF-analog/PLL or QSPI controller (**TBD**) | fn `0x1c3fc`: opcodes `0x38/0x3c/0x3d/0x3e`, poll ready, inside a `cpsid` critical section |

The SDK's **FRSPIM `0x500F0000` is never referenced** anywhere in the ROM — the
radio is not driven through that base.

## The radio — where the 2.4 GHz lives

The radio is **`0x40000000`** (the BLE / modem link-layer engine) + its
**`0x40004000`** descriptor array + SRAM state structs, with `0x500B0000` the
likely RF-analog / PLL sequencer and the `0x1e000` block its init / calibration
table. Crucially, these are **genuine memory-mapped registers driven by ROM code
that is fully present and disassemblable** — the PHY is *not* opaque or off-chip.

## Owned-2.4 G feasibility — not blocked

The register-level radio interface and the complete driver logic are in this ROM.
So custom firmware can, in principle, either **drive the modem registers directly**
(a fully custom on-air protocol) using the ROM as the spec, or **call the ROM's
radio routines** (a ROM API via the SVC dispatch / fixed pointers) from a custom
app. The open question is the *effort tier*, not possibility. This resolves the
[`07`](07-24ghz-link.md) blocker (the PHY that was absent from the flash).

## Open items (in progress)

- The `0x40000000` modem register semantics (packet format, channel/frequency set,
  whitening / CRC, timing) — the depth a from-scratch PHY needs.
- `0x500B0000` identity (RF-analog vs QSPI controller); the `0x1e000` table decode
  (RF calibration vs clock plan).
- The callable ROM radio-API entry points (SVC table at `[0x1c4]`, fixed function
  pointers) a custom app could reuse.
- Reconcile the [`01`](01-memory-map.md) SDK-nominal peripheral table with these
  ROM-observed bases.
