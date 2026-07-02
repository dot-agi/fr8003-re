#!/usr/bin/env python3
"""Probe the Akko 5075B radio (Freqchip FR8003A) over UART.

Handshakes with the ROMBOOT, prints the chip type + chip id + flash JEDEC id,
and -- critically -- reports the readout-lock / code-encryption status by
sampling the flash and analysing it.  Fails LOUD if the readout looks blocked or
encrypted, so you learn *before* trusting a dump that Akko has locked the part.

READ-ONLY: this never erases or writes.

Until the ROMBOOT wire protocol is captured (see .planning/fr8003-dump.md), the
handshake step raises ProtocolNotVerified with the exact next steps; the safe
bring-up helpers to capture it live in ``fr_isp.py`` (bootlog / autobaud / raw).
"""

from __future__ import annotations

import argparse
import sys
import zlib

import fr_isp
from fr_isp import (
    FLASH_SIZE,
    ProtocolNotVerified,
    RomLink,
    Transport,
    assess_readout,
    shannon_entropy,
)


def _try(fn):
    """Call an optional protocol read; return None if it isn't available yet."""
    try:
        return fn()
    except (ProtocolNotVerified, fr_isp.FrError):
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-p", "--port", required=True,
                    help="serial device wired to CON3, e.g. /dev/tty.usbserial-XXXX")
    ap.add_argument("-b", "--baud", type=int, default=115200,
                    help="handshake baud (default 115200; the ROM may renegotiate)")
    ap.add_argument("--sample", type=lambda s: int(s, 0), default=0x1000,
                    help="bytes to sample for the lock/encryption check (default 0x1000)")
    args = ap.parse_args(argv)

    # Head and tail samples must fit and not require a negative offset.
    if not (1 <= args.sample <= FLASH_SIZE // 2):
        ap.error(f"--sample must be 1..0x{FLASH_SIZE // 2:x}")

    print(f"[*] opening {args.port} @ {args.baud} 8N1")
    try:
        with Transport(args.port, baud=args.baud) as t:
            link = RomLink(t)

            print("[*] handshaking with ROMBOOT (power-cycle the radio now if it stalls)...")
            link.handshake()

            ident = link.read_chip_id()
            print("[+] chip identity:")
            for k, v in ident.items():
                print(f"      {k:<14}: {v}")
            # Best-effort provenance reads; absent until the protocol exposes them.
            status = _try(link.read_status_registers)
            euid = _try(link.read_efuse_unique_id)
            if status is not None:
                print(f"      status_regs   : {status}")
            if euid is not None:
                print(f"      efuse/uid     : {euid.hex()}")

            print(f"[*] sampling first 0x{args.sample:x} bytes of flash for readout check...")
            head = link.read_flash(0, args.sample)
            tail = link.read_flash(FLASH_SIZE - args.sample, args.sample)
            report = assess_readout(head, full_entropy=shannon_entropy(head + tail))
            print(f"[+] readout heuristic: {report['verdict']} "
                  f"(header entropy {report['entropy_head']:.2f} bits/byte)")
            for note in report["notes"]:
                print(f"      - {note}")

            # Hard stop on the pathological cases (a locked / encrypted part): the
            # flash reads back blank or near-random.
            if report["verdict"] in ("BLOCKED", "ENCRYPTED"):
                print("\n[!] READOUT LOOKS BLOCKED/ENCRYPTED -- a dump taken now would "
                      "likely be useless (Akko may have locked the part, or the ROM "
                      "read path differs). See .planning/fr8003-dump.md.", file=sys.stderr)
                return 2

            # Authoritative check: does the ROM's own CRC32 of the sampled span match
            # our readback?  A match PROVES the reads are faithful (the CONFIRMED
            # CODE_CRC op), independent of the entropy heuristic and the medium-
            # confidence READ opcode.
            try:
                chip_crc = link.chip_crc32(0, args.sample)
                local_crc = zlib.crc32(head) & 0xFFFFFFFF
                if chip_crc == local_crc:
                    print(f"\n[+] CRC-verified faithful readout (0x{chip_crc:08x}) -- "
                          "safe to run dump.py for a full image.")
                    return 0
                print(f"\n[!] CRC MISMATCH: chip 0x{chip_crc:08x} != readback "
                      f"0x{local_crc:08x} -- reads are NOT faithful (a protocol/"
                      "addressing problem). Do NOT trust a dump.", file=sys.stderr)
                return 2
            except fr_isp.FrError as e:
                print(f"\n[*] readout looks structured but the chip CRC op did not answer "
                      f"({e}); the reads are plausible. Run dump.py, then take a SECOND "
                      "dump and match SHA-256 to confirm.", file=sys.stderr)
                return 0

    except ProtocolNotVerified as e:
        print("\n[--] Protocol not yet captured:\n", file=sys.stderr)
        print(str(e), file=sys.stderr)
        return 3
    except fr_isp.FrError as e:
        print(f"\n[!] link error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
