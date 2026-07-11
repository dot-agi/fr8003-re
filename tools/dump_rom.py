#!/usr/bin/env python3
"""Dump the FR8003A 128 KiB mask ROM (0x0..0x20000) over the ROMBOOT UART.

The mask ROM holds what the flash dump does NOT: the BLE controller + host stack,
the boot code, and -- the reason we want it -- the proprietary 2.4G driver
(``rf_simu``) whose on-air PHY/MAC never appears in the flash APP image.  This is
the go/no-go RE gate for an owned 2.4G stack.

Mechanism: the ROMBOOT flash READ opcode takes 0-based *flash* offsets, so it can
only reach the QSPI flash -- never the mask ROM.  ``OP_READ_RAM`` (0x0A) instead
takes an *absolute* address and returns 4 bytes, so it reads the mask ROM mapped
at 0x0.  READ-ONLY and non-destructive (the mask ROM cannot be re-flashed anyway).
It is 4 bytes per round trip, so a full 128 KiB is minutes; ``--dump-baud`` raises
the link speed for the read.

Wiring + entry are identical to the flash dump (see docs/03): CON3 UART to the
radio, power-cycle to (re)enter ROMBOOT, then run this.  There is no ROM CRC op
(the ROM's CRC operates over the XIP window, not 0x0), so faithfulness is proven
by dumping TWICE and matching the SHA-256.

If READ_RAM does not reach 0x0 on this ROM (all-0x00/0xFF, or a short-read error),
fall back to the WRITE_RAM+EXE reader-stub path (docs/08) -- a tiny stub that
streams 0x0..0x20000 out the ROM UART with full memory access.
"""
from __future__ import annotations

import argparse
import hashlib
import struct
import sys

from fr_isp import Transport, RomLink, FrError, ProtocolNotVerified

ROM_BASE = 0x00000000
ROM_SIZE = 0x00020000  # 128 KiB mask ROM


def _progress(done: int, total: int) -> None:
    pct = 100 * done // total if total else 100
    bar = "#" * (pct // 4)
    sys.stderr.write(f"\r    [{bar:<25}] {pct:3d}%  ({done}/{total} B)")
    sys.stderr.flush()
    if done >= total:
        sys.stderr.write("\n")


def _looks_like_vector_table(image: bytes) -> str:
    """Best-effort sanity note on the first two words of a Cortex-M image at 0x0.

    word0 = initial MSP (a RAM address); word1 = reset vector (odd/thumb, and for
    the mask ROM it must land inside 0x0..0x20000).  This is a heuristic hint only,
    printed for the operator to eyeball -- never a hard gate.
    """
    if len(image) < 8:
        return "too short to check"
    msp, reset = struct.unpack_from("<II", image, 0)
    if image[:8] in (b"\x00" * 8, b"\xff" * 8):
        return (f"MSP=0x{msp:08x} reset=0x{reset:08x} -- looks BLANK (0x00/0xFF): "
                "READ_RAM likely did NOT reach the mask ROM; use the stub path (docs/08)")
    ram_ok = 0x11000000 <= msp <= 0x11010000 or 0x20000000 <= msp <= 0x20010000
    reset_ok = (reset & 1) and (reset & ~1) < ROM_SIZE
    verdict = "PLAUSIBLE mask-ROM vector table" if (ram_ok and reset_ok) else \
              "does not look like a mask-ROM vector table -- inspect before trusting"
    return f"MSP=0x{msp:08x} reset=0x{reset:08x} -- {verdict}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-p", "--port", required=True, help="serial port to the radio CON3 UART")
    ap.add_argument("-b", "--baud", type=int, default=115200, help="handshake baud (default 115200)")
    ap.add_argument("--dump-baud", type=int, default=None,
                    help="optional faster baud for the read itself, e.g. 921600")
    ap.add_argument("-o", "--out", default="fr8003-maskrom.bin", help="output image path")
    ap.add_argument("--base", type=lambda s: int(s, 0), default=ROM_BASE, help="start address (default 0x0)")
    ap.add_argument("--size", type=lambda s: int(s, 0), default=ROM_SIZE, help="length (default 0x20000)")
    a = ap.parse_args(argv)

    print(f"[*] opening {a.port} @ {a.baud} 8N1")
    try:
        with Transport(a.port, baud=a.baud) as t:
            link = RomLink(t)
            print("[*] handshaking with ROMBOOT -- power-cycle the radio now if it stalls...")
            link.handshake()
            ident = link.read_chip_id()
            print("[+] chip:", ", ".join(f"{k}={v}" for k, v in ident.items()))
            if a.dump_baud:
                print(f"[*] raising link baud to {a.dump_baud} for the read...")
                link.set_baud(a.dump_baud)
            print(f"[*] reading 0x{a.size:x} bytes of MASK ROM from 0x{a.base:08x} "
                  "via READ_RAM (4 B/txn -- slow)...")
            image = link.read_ram(a.base, a.size, progress=_progress)
    except ProtocolNotVerified as e:
        print("\n[--] protocol not verified:\n", e, file=sys.stderr)
        return 3
    except FrError as e:
        print(f"\n[!] link error: {e}", file=sys.stderr)
        return 1

    if len(image) != a.size:
        print(f"[!] short read: got {len(image)}/{a.size} bytes", file=sys.stderr)
        return 1

    digest = hashlib.sha256(image).hexdigest()
    with open(a.out, "wb") as f:
        f.write(image)
    with open(a.out + ".sha256", "w") as f:
        f.write(f"{digest}  {a.out}\n")

    print(f"[+] wrote {a.out} ({len(image)} bytes)")
    print(f"[+] sha256 {digest}")
    print(f"[+] first-words check: {_looks_like_vector_table(image)}")
    print("\n[+] VERIFY: run this again to a SECOND file and confirm the two SHA-256s")
    print("    match (there is no ROM CRC op, so a matched re-read is the integrity proof).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
