#!/usr/bin/env python3
"""Non-destructive full 512 KiB flash dump of the Akko 5075B radio (FR8003A).

Reads the entire QSPI flash through the ROM's read path -- which the vendor
algorithm implements as a plain memory read from the 0x10000000 XIP window (see
fr_isp.py), so this operation is READ-ONLY: it never erases, programs, or
otherwise changes the chip.  Writes three artefacts next to each other:

    fr8003-dump.bin        the raw 512 KiB image
    fr8003-dump.bin.sha256 its SHA-256
    fr8003-dump.manifest.json   chip id, size, readout verdict, timestamp, hash

Run ``probe.py`` first to confirm the readout is plaintext.  If the readout
looks encrypted/blocked, this still writes the image but flags it loudly in the
manifest and on stderr so you never mistake a locked readout for a good dump.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
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


def _progress(done: int, total: int) -> None:
    pct = 100 * done // total
    bar = "#" * (pct // 4)
    sys.stderr.write(f"\r    [{bar:<25}] {pct:3d}%  ({done}/{total} bytes)")
    sys.stderr.flush()
    if done >= total:
        sys.stderr.write("\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-p", "--port", required=True,
                    help="serial device wired to CON3, e.g. /dev/tty.usbserial-XXXX")
    ap.add_argument("-b", "--baud", type=int, default=115200, help="handshake baud (default 115200)")
    ap.add_argument("-o", "--out", default="fr8003-dump.bin", help="output image path")
    ap.add_argument("--size", type=lambda s: int(s, 0), default=FLASH_SIZE,
                    help=f"bytes to read (default full flash 0x{FLASH_SIZE:x})")
    args = ap.parse_args(argv)

    if args.size <= 0 or args.size > FLASH_SIZE:
        ap.error(f"--size must be 1..0x{FLASH_SIZE:x}")

    print(f"[*] opening {args.port} @ {args.baud} 8N1")
    try:
        with Transport(args.port, baud=args.baud) as t:
            link = RomLink(t)

            print("[*] handshaking with ROMBOOT (power-cycle the radio now if it stalls)...")
            link.handshake()
            ident = link.read_chip_id()
            print("[+] chip:", ", ".join(f"{k}={v}" for k, v in ident.items()))
            # Best-effort provenance (not part of the main-flash image); None
            # until the ROM protocol exposes these reads.
            status = _try(link.read_status_registers)
            euid = _try(link.read_efuse_unique_id)

            print(f"[*] reading 0x{args.size:x} bytes of flash (read-only, no erase)...")
            image = link.read_flash(0, args.size, progress=_progress)

            # Authoritative faithfulness check: ask the ROM for the CRC32 of the
            # same span (the CONFIRMED CODE_CRC op) and compare it to our readback.
            # A match PROVES the dump equals what's on the chip -- independent of the
            # entropy heuristic and of the medium-confidence READ opcode.
            crc_verified = None
            try:
                chip_crc = link.chip_crc32(0, args.size)
                local_crc = zlib.crc32(image) & 0xFFFFFFFF
                crc_verified = chip_crc == local_crc
                print(f"[{'+' if crc_verified else '!'}] chip CRC32 0x{chip_crc:08x} vs "
                      f"readback 0x{local_crc:08x} -- "
                      f"{'MATCH (dump is faithful)' if crc_verified else 'MISMATCH'}")
            except fr_isp.FrError as e:
                print(f"[*] chip CRC op did not answer ({e}); confirm faithfulness by "
                      "taking a second dump and matching SHA-256.")

    except ProtocolNotVerified as e:
        print("\n[--] Protocol not yet captured -- cannot dump over UART yet:\n", file=sys.stderr)
        print(str(e), file=sys.stderr)
        return 3
    except fr_isp.FrError as e:
        print(f"\n[!] link error: {e}", file=sys.stderr)
        return 1

    if len(image) != args.size:
        print(f"[!] short read: got {len(image)} of {args.size} bytes", file=sys.stderr)
        return 1

    digest = hashlib.sha256(image).hexdigest()
    verdict = assess_readout(image[:0x1000], full_entropy=shannon_entropy(image))

    out = os.path.abspath(args.out)
    with open(out, "wb") as f:
        f.write(image)
    with open(out + ".sha256", "w") as f:
        f.write(f"{digest}  {os.path.basename(out)}\n")

    manifest = {
        "tool": "fr8003 dump.py",
        "chip": "Freqchip FR8003A",
        "chip_id": ident,
        "scope": "main QSPI flash only",
        "excludes": ["EFUSE / chip unique id", "flash OTP / security registers",
                     "128 KiB mask ROM", "peripheral state"],
        # A valid restore source is a full image PROVEN faithful by the CRC
        # round-trip -- not merely one the entropy heuristic liked.
        "restorable": len(image) == FLASH_SIZE and crc_verified is True,
        "crc_verified": crc_verified,
        "flash_size": FLASH_SIZE,
        "bytes_read": len(image),
        "sha256": digest,
        "flash_status_registers": status,
        "efuse_unique_id": euid.hex() if euid else None,
        "readout_verdict": verdict["verdict"],
        "readout_verdict_is_inferred": True,
        "readout_notes": verdict["notes"],
        "image_entropy_bits_per_byte": round(shannon_entropy(image), 4),
        "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "image_file": os.path.basename(out),
        "read_only": True,
        "source_window": f"0x{fr_isp.FLASH_XIP_BASE:08x} (XIP, non-destructive)",
    }
    manifest_path = os.path.splitext(out)[0] + ".manifest.json"
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")

    print(f"[+] wrote {out} ({len(image)} bytes)")
    print(f"[+] sha256 {digest}")
    print(f"[+] manifest {manifest_path}")

    if verdict["verdict"] in ("BLOCKED", "ENCRYPTED"):
        print(f"\n[!] WARNING: readout verdict is {verdict['verdict']} -- the flash "
              "reads back blocked/encrypted, so this image is probably NOT usable "
              "firmware (a locked part). Read .planning/fr8003-dump.md.", file=sys.stderr)
        return 2
    if crc_verified is False:
        print("\n[!] CRC MISMATCH: the read-back does not match the chip's own CRC -- "
              "the dump is NOT faithful (a protocol/addressing problem). Do not trust "
              "it; do not restore from it.", file=sys.stderr)
        return 2
    if crc_verified is None:
        print("\n[*] Dump written, but the chip CRC op did not confirm it. Take a "
              "SECOND dump and check the two SHA-256s match before trusting it.",
              file=sys.stderr)
        return 0
    print("\n[+] Dump CRC-verified faithful to the chip -- keep it as your backup.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
