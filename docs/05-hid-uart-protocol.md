# 05 — HID-over-UART protocol (WB32 ↔ FR8003A)

The main MCU (Westberry WB32FQ95) and this radio exchange HID reports and control
over a **standard 16550 UART at 115200 8N1**. The wire protocol is the vendor
"**md**" (module) codec. It is **fully reversed and cross-confirmed**: the radio's
receive parser and transmit builders were traced in this image, and every opcode,
length, the checksum, and the ACK match keeberry's independent implementation
(`firmware/src/wireless/md.rs`, `mod.rs`, and `firmware/src/uart.rs`) byte-for-byte. Two
independently-written ends agreeing is decisive — this is the highest-confidence
result in the repo.

Addressing rule: **VMA = 0x10000000 + file offset** for code/strings; runtime
radio state is accessed through a `0x11000000` (RAM/uncached) alias.

## Framing (both directions)

```
[opcode] [payload …] [checksum]
```

- `checksum = (Σ opcode + payload bytes) & 0xFF` — 8-bit additive sum, trailing byte.
- Multi-byte fields little-endian.
- Payload length is **implicit per opcode**, except the variable-length frames
  which carry an **inline length byte** (at payload index 0, or index 1 for RAW).
- **Sync ACK = `61 0D 0A`** — 3 bytes, *no checksum*. The receiver of any valid
  data frame echoes it; the sender waits for it. The link is **stop-and-wait,
  one frame in flight** (keeberry: 10 ms timeout, 40 retries).

## WB32 → radio (radio parses at VMA `0x10019170`)

| Opcode | Name | Payload | Frame len | Confidence |
|---|---|---|---|---|
| `0xA1` | SEND_KB | 8-byte boot keyboard report | 10 | CONFIRMED |
| `0xA2` | SEND_NKRO | 14-byte NKRO bitmap | 16 | CONFIRMED |
| `0xA3` | SEND_CONSUMER | u16 LE usage | 4 | CONFIRMED |
| `0xA4` | SEND_SYSTEM | 1 (bitmask) | 3 | CONFIRMED |
| `0xA6` | DEVCTRL | 1 (sub-command, see below) | 3 | CONFIRMED |
| `0xA8` | SEND_MOUSE | 5 `[btn,x,y,wheel,pan]` | 7 | CONFIRMED |
| `0xA9` | SEND_DEVINFO | `[len][name…]` | var | CONFIRMED |
| `0xAB` | MANUFACTURER | `[len][str…]` | var | CONFIRMED |
| `0xAC` | PRODUCT | `[len][str…]` | var | CONFIRMED |
| `0xAD` | VPID | 4 = `(pid<<16)\|vid` LE | 6 | CONFIRMED |
| `0xAF` | RAW | `[0x61][32][32 data bytes]` | 36 | CONFIRMED |
| `0x61` | (ACK lead-in) | — | 3 | CONFIRMED |

**DEVCTRL (`0xA6`) sub-commands** (payload byte): `0x11` USB · `0x30` 2.4 GHz ·
`0x31/0x32/0x33` BT1/2/3 · `0x51` PAIR · `0x52` CLEAN (unpair) · `0x53` INQ_BAT ·
`0x55/0x56` BT-sleep en/dis · `0x57/0x58` 2.4G-sleep en/dis · `0x64/0x65/0x66`
charging/stop/done · `0x70` FW_VERSION. (Sub-command values from keeberry;
vendor-standard.)

## radio → WB32 (keeberry parses in `MdRx`)

| Opcode | Name | Payload | Frame len | Radio build site | Confidence |
|---|---|---|---|---|---|
| `0x5A` | INDICATOR | 1 (host LED bitmap) | 3 | — | INFERRED |
| `0x5B` | DEVCTRL (conn state) | 1 (sub) | 3 | `0x1000325e`, `0x10017e30` | CONFIRMED |
| `0x5C` | BATVOL | 1 (0..100) | 3 | `0x100194e2` | CONFIRMED |
| `0x5D` | MD_FW_VERSION | 1 | 3 | `0x100194f4` | CONFIRMED |
| `0x60` | HOST_STATE | 1 (`0x01`=resume) | 3 | `0x10011b02`, `0x10019952` | CONFIRMED |
| `0xAF` | RAW (out) | `[0x60][32][32 data bytes]` | 36 | `0x10011afc`+`0x10011b02` | CONFIRMED |

**DEVCTRL-notify (`0x5B`) sub-commands:** `0x31` PAIRING · `0x32` CONNECTED ·
`0x33` DISCONNECTED · `0x36` REJECT. Note `0x60` is overloaded — the `HOST_STATE`
opcode *and* the RAW-out sub-opcode — disambiguated by position (index 0 vs 1).

## Evidence (key addresses)

- **Receive parser** `0x10019170` — byte-at-a-time state machine; state struct at
  `0x11003b6c`, payload buffer `0x11004bf8`. Opcode→length dispatch at
  `0x1001918e`; inline-length handling at `0x1001922c`; checksum loop `0x10019270`;
  ACK short-circuit `0x10019252`.
- **Sync ACK token** `61 0D 0A` — appears exactly once, as `0x000A0D61` at
  `0x1001957c` in the UART handler's literal pool.
- **Transmit builders** — BATVOL at `0x100194e2`, FW_VER at `0x100194f4`, RAW-out
  at `0x10011afc`. Send veneer `0x10018d90 → 0x10017111` (md UART send),
  checksum helper `0x10018e1c → 0x100118f9`.
- **UART layer** — 16550 TX at `0x10016bec` (spin on LSR THRE bit 5, write THR);
  bases `UART0=0x50050000`, `UART1=0x50058000` (link is on UART1, strong-inferred
  from md-state adjacency; the runtime handle blocks a fully static proof).

## Opportunities keeberry does not yet exploit

1. **`HOST_STATE` (0x60)** is decoded but only logged — the radio actively reports
   host suspend/resume, so implementing host-driven low-power (stop scanning on
   suspend) is a real, ready feature.
2. **A few extra WB32→radio commands.** Beyond the five keeberry sends
   (`0xA9/AB/AC/AD/AF`), the dispatch also accepts **`0xA7`** (3-byte),
   **`0xB3`** (5-byte), and **`0xB4`** (3-byte). The other high slots (`0xAA`,
   `0xAE`, `0xB0–0xB2`) fall through to the default/drop path, and `0xA5` is
   length-accepted but dropped — so those are *not* usable commands. Only the
   three real extras (`0xA7`/`0xB3`/`0xB4`) are worth chasing for added capability.

## Open questions

- Physical UART instance (UART1 strong-inferred vs UART0) — not part of the wire
  contract.
- Radio-side baud programming (DLL/DLM write) not isolated; link demonstrably runs
  115200 8N1 (keeberry is hardware-verified).
- The extra commands (`0xA7`/`0xB3`/`0xB4`): a dedicated reset trace decoded them —
  `0xB3` clears hardware-register bits at `0x400000D0`, and none of them reset the
  radio or enter a special mode (see [08](08-software-reflash.md)).
- Exact `0x5A` INDICATOR emit path in the radio not pinned.
