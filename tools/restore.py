#!/usr/bin/env python3
"""Brick-safe reflash of the Akko 5075B radio (FR8003A) from a saved image.

Restoring flash is the dangerous direction.  The freqchip forum (topic 1835)
documents a botched UART update that bricked a chip so hard that even SWD/JFlash
could no longer erase or program it.  This script is built to make that
outcome as hard as possible:

  GUARDS (all on by default)
    * refuses to run unless the ROMBOOT wire protocol is verified (fr_isp.py);
      no guessed erase/write frames ever hit the chip.
    * refuses empty / short / oversized images; a full restore must be exactly
      the flash size (512 KiB) unless you pass an explicit --offset.
    * verifies the image SHA-256 against its sidecar (or --sha256) BEFORE
      touching the chip; mismatch aborts unless you pass --i-know-the-hash.
    * never does a blind chip-erase.  It erases only the sectors it is about to
      write, and only after you type the confirmation phrase (or pass --yes).
    * writes the flash HEADER sector (offset 0, the jump table the SDK
      bootloader validates) LAST.  If a write is interrupted, offset 0 is left
      invalid/blank rather than half-written -- and because ROMBOOT auto-listens
      on UART at power-on regardless of flash contents, you can always retry.
    * reads every written sector back and verifies it; a full-image read-back +
      SHA compare runs at the end.

The vendor update model is A/B-bank + CRC-checked-on-reboot; that path is for
in-application OTA.  This tool instead restores the *exact bytes* of a known-good
dump, which is the safest recovery.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import sys

import fr_isp
from fr_isp import (
    FLASH_SECTOR,
    FLASH_SIZE,
    ProtocolNotVerified,
    RomLink,
    Transport,
)

CONFIRM_PHRASE = "reflash the radio"


def _manifest_path(image_path: str) -> str:
    return os.path.splitext(image_path)[0] + ".manifest.json"


def _check_manifest_static(image_path: str, image: bytes, digest: str) -> list:
    """Static (no-hardware) consistency checks of the image against its manifest.

    Returns a list of problems (empty == OK / no manifest).  Catches the case
    codex flagged: a BLOCKED/ENCRYPTED or non-restorable dump whose ``.sha256``
    sidecar still matches would otherwise sail through.
    """
    path = _manifest_path(image_path)
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            man = json.load(f)
    except (OSError, ValueError) as e:
        return [f"could not read manifest {path}: {e}"]

    problems = []
    if man.get("sha256") and man["sha256"] != digest:
        problems.append(f"manifest sha256 {man['sha256']} != image {digest}")
    if man.get("bytes_read") is not None and man["bytes_read"] != len(image):
        problems.append(f"manifest bytes_read {man['bytes_read']} != image {len(image)}")
    verdict = man.get("readout_verdict")
    if verdict and verdict != "PLAINTEXT":
        problems.append(f"manifest records the dump as {verdict} (not usable firmware)")
    if man.get("restorable") is False:
        problems.append("manifest marks this image as not restorable")
    return problems


def _erase_write_verify(link: RomLink, off: int, chunk: bytes) -> bool:
    """Erase, program, and read-back-verify one sector.  True on match."""
    link.erase_sector(off)
    link.write_flash(off, chunk)
    return link.read_flash(off, len(chunk)) == chunk


def _manifest_ok(image_path: str, ident: dict, link: RomLink, force: bool) -> bool:
    """Cross-check the target chip against the image's manifest, if present.

    Returns True to proceed.  A missing manifest is allowed (with a note); a
    real mismatch of chip id or EFUSE unique id is refused unless ``force``.
    """
    path = _manifest_path(image_path)
    if not os.path.exists(path):
        print("[*] no image manifest found -- skipping same-chip check.")
        return True
    with open(path) as f:
        man = json.load(f)

    ok = True
    if man.get("chip_id") and ident and man["chip_id"] != ident:
        print(f"[!] chip_id differs: image={man['chip_id']} target={ident}", file=sys.stderr)
        ok = False
    want_uid = man.get("efuse_unique_id")
    if want_uid:
        try:
            got = link.read_efuse_unique_id()
            if got is not None and got.hex() != want_uid:
                print(f"[!] efuse/uid differs: image={want_uid} target={got.hex()}",
                      file=sys.stderr)
                ok = False
        except (ProtocolNotVerified, fr_isp.FrError):
            pass
    if ok:
        print("[+] target matches image manifest (same chip).")
    return ok or force


def _load_expected_sha(image_path: str, override: str | None) -> str | None:
    if override:
        return override.strip().split()[0].lower()
    sidecar = image_path + ".sha256"
    if os.path.exists(sidecar):
        with open(sidecar) as f:
            return f.read().strip().split()[0].lower()
    return None


def _sectors(offset: int, length: int):
    """Yield sector-aligned (start, size) spans covering [offset, offset+length)."""
    start = offset
    end = offset + length
    if start % FLASH_SECTOR or end % FLASH_SECTOR:
        raise ValueError("restore range must be 4 KiB sector aligned")
    for s in range(start, end, FLASH_SECTOR):
        yield s, min(FLASH_SECTOR, end - s)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-p", "--port", required=True, help="serial device wired to CON3")
    ap.add_argument("-b", "--baud", type=int, default=115200, help="handshake baud (default 115200)")
    ap.add_argument("-i", "--image", required=True, help="image to write (e.g. fr8003-dump.bin)")
    ap.add_argument("--offset", type=lambda s: int(s, 0), default=0,
                    help="flash offset to write at (default 0 = full restore)")
    ap.add_argument("--sha256", help="expected SHA-256 of the image (else read <image>.sha256)")
    ap.add_argument("--i-know-the-hash", action="store_true",
                    help="proceed even if the SHA-256 sidecar or manifest integrity "
                         "checks fail (you vouch for the image; discouraged)")
    ap.add_argument("--force-cross-chip", action="store_true",
                    help="proceed even if the target chip id/uid differs from the image manifest")
    ap.add_argument("--unsafe-partial", action="store_true",
                    help="allow a partial write (--offset != 0); the header-last "
                         "brick-safety invariant only holds for a full restore, so this "
                         "is refused by default")
    ap.add_argument("--no-backup", action="store_true",
                    help="skip the safety pre-restore dump of the current flash (discouraged)")
    ap.add_argument("--yes", action="store_true",
                    help="skip the interactive confirmation prompt")
    args = ap.parse_args(argv)

    # --- validate the image on disk BEFORE opening the port ------------------
    if not os.path.exists(args.image):
        ap.error(f"image not found: {args.image}")
    with open(args.image, "rb") as f:
        image = f.read()

    if len(image) == 0:
        print("[!] refusing to write an empty image", file=sys.stderr)
        return 1
    if args.offset < 0:
        print("[!] --offset must be >= 0", file=sys.stderr)
        return 1
    if args.offset + len(image) > FLASH_SIZE:
        print(f"[!] image overflows flash: offset 0x{args.offset:x} + 0x{len(image):x} "
              f"> 0x{FLASH_SIZE:x}", file=sys.stderr)
        return 1
    if args.offset == 0 and len(image) != FLASH_SIZE:
        print(f"[!] a full restore (offset 0) must be exactly 0x{FLASH_SIZE:x} bytes; "
              f"this image is 0x{len(image):x}.  Pass --offset for a partial write.",
              file=sys.stderr)
        return 1
    if (args.offset % FLASH_SECTOR) or (len(image) % FLASH_SECTOR):
        print(f"[!] offset and image length must be 4 KiB aligned "
              f"(sector = 0x{FLASH_SECTOR:x})", file=sys.stderr)
        return 1
    # BRICK-SAFETY: only a FULL restore (offset 0) enforces "erase the boot header
    # first, write it last". A partial write (offset != 0) leaves the existing valid
    # header pointing at a half-updated body -- an interruption can brick. Refuse by
    # default; --unsafe-partial is the deliberate opt-out.
    if args.offset != 0 and not args.unsafe_partial:
        print("[!] refusing a partial restore (--offset != 0): the header-last "
              "brick-safety invariant only holds for a full-image restore. Use "
              "--unsafe-partial only if you accept the brick risk.", file=sys.stderr)
        return 1
    # Refuse to flash an obviously-dead image (all 0x00 / all 0xFF): that would
    # brick the radio, and is the classic sign of a bad/locked source dump.
    if all(b == 0x00 for b in image) or all(b == 0xFF for b in image):
        print("[!] refusing to write: the image is entirely 0x00/0xFF (a dead/"
              "blocked dump).  Writing it would brick the radio.", file=sys.stderr)
        return 1
    # A full restore whose header sector (offset 0) is blank would not boot even
    # if the rest is fine -- classic sign of a truncated/locked dump.
    if args.offset == 0 and (all(b == 0xFF for b in image[:FLASH_SECTOR])
                             or all(b == 0x00 for b in image[:FLASH_SECTOR])):
        print("[!] refusing to write: the image's header sector (offset 0) is "
              "blank -- the radio would not boot.", file=sys.stderr)
        return 1

    digest = hashlib.sha256(image).hexdigest()
    expected = _load_expected_sha(args.image, args.sha256)
    print(f"[*] image {args.image}: {len(image)} bytes, sha256 {digest}")
    if expected is None:
        print("[!] no expected SHA-256 available (no sidecar, no --sha256).", file=sys.stderr)
        if not args.i_know_the_hash:
            print("[!] aborting; pass --i-know-the-hash to override.", file=sys.stderr)
            return 1
    elif expected != digest:
        print(f"[!] SHA-256 MISMATCH: expected {expected}", file=sys.stderr)
        if not args.i_know_the_hash:
            print("[!] aborting; the image does not match its checksum.", file=sys.stderr)
            return 1
    else:
        print("[+] SHA-256 matches sidecar -- image integrity OK")

    problems = _check_manifest_static(args.image, image, digest)
    if problems:
        for p in problems:
            print(f"[!] manifest check: {p}", file=sys.stderr)
        if not args.i_know_the_hash:
            print("[!] aborting; pass --i-know-the-hash to override.", file=sys.stderr)
            return 1

    # --- confirmation --------------------------------------------------------
    span = f"offset 0x{args.offset:x}..0x{args.offset + len(image):x}"
    print(f"\n[!] About to ERASE and WRITE {span} of the radio's flash.")
    print("[!] This is irreversible; a bad/interrupted write can brick the radio.")
    if not args.yes:
        try:
            reply = input(f'    Type "{CONFIRM_PHRASE}" to proceed: ')
        except EOFError:
            reply = ""
        if reply.strip() != CONFIRM_PHRASE:
            print("[--] not confirmed; aborting.  (Nothing was written.)")
            return 1

    # --- drive the chip ------------------------------------------------------
    print(f"[*] opening {args.port} @ {args.baud} 8N1")
    try:
        with Transport(args.port, baud=args.baud) as t:
            link = RomLink(t)
            print("[*] handshaking with ROMBOOT (power-cycle the radio if it stalls)...")
            link.handshake()
            ident = link.read_chip_id()
            print("[+] chip:", ", ".join(f"{k}={v}" for k, v in ident.items()))

            if not _manifest_ok(args.image, ident, link, args.force_cross_chip):
                print("[!] target chip does not match the image's manifest; aborting.\n"
                      "[!] pass --force-cross-chip to override (you are writing one "
                      "chip's firmware onto a different chip).", file=sys.stderr)
                return 1

            # SAFETY NET: back up the region we're about to overwrite BEFORE any
            # erase, so a failed restore is still recoverable to the prior state.
            if not args.no_backup:
                backup = link.read_flash(args.offset, len(image))
                stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                bpath = f"{args.image}.prerestore-{stamp}.bin"
                with open(bpath, "wb") as f:
                    f.write(backup)
                print(f"[+] pre-restore backup of current flash -> {bpath} "
                      f"(sha256 {hashlib.sha256(backup).hexdigest()})")

            # Plan which sectors differ from what's already on the chip (reads are
            # non-destructive) so we erase only what must change.
            spans = list(_sectors(args.offset, len(image)))
            plan = []
            for sec_off, sec_len in spans:
                chunk = image[sec_off - args.offset: sec_off - args.offset + sec_len]
                current = link.read_flash(sec_off, sec_len)
                plan.append((sec_off, sec_len, chunk, current != chunk))
            diffs = [p for p in plan if p[3]]

            def _fail(off: int) -> None:
                sys.stderr.write("\n")
                print(f"[!] verify FAILED at sector 0x{off:05x}; STOPPING.\n"
                      "[!] Do not power-cycle blindly; re-run restore to finish "
                      "(the boot header is invalidated first and written last, so "
                      "retry is safe).", file=sys.stderr)

            if not diffs:
                print("[+] flash already matches the image; nothing to write.")
            else:
                header = next((p for p in plan if p[0] == 0), None)
                if header is not None:
                    # BRICK-SAFETY: invalidate the boot header (offset 0) FIRST so
                    # that at no point does a valid header sit over a half-updated
                    # body.  An interruption then leaves offset 0 erased -- the
                    # radio won't run, but ROMBOOT still answers on UART, so a
                    # re-run finishes the job.
                    hdr_off, hdr_len, hdr_chunk, _ = header
                    print("[*] invalidating boot header sector (offset 0) first...")
                    link.erase_sector(0)
                    if any(b != 0xFF for b in link.read_flash(0, hdr_len)):
                        _fail(0)
                        return 1
                    body = [p for p in diffs if p[0] != 0]
                    for i, (off, _ln, chunk, _d) in enumerate(body, 1):
                        sys.stderr.write(f"\r    body sector {i}/{len(body)} @ 0x{off:05x}   ")
                        sys.stderr.flush()
                        if not _erase_write_verify(link, off, chunk):
                            _fail(off)
                            return 1
                    sys.stderr.write("\n")
                    print("[*] writing boot header sector last...")
                    link.write_flash(0, hdr_chunk)  # already erased above
                    if link.read_flash(0, hdr_len) != hdr_chunk:
                        _fail(0)
                        return 1
                    print(f"[+] {len(body) + 1} sector(s) written (incl. header), "
                          f"{len(plan) - len(body) - 1} already matched.")
                else:
                    for i, (off, _ln, chunk, _d) in enumerate(diffs, 1):
                        sys.stderr.write(f"\r    sector {i}/{len(diffs)} @ 0x{off:05x}   ")
                        sys.stderr.flush()
                        if not _erase_write_verify(link, off, chunk):
                            _fail(off)
                            return 1
                    sys.stderr.write("\n")
                    print(f"[+] {len(diffs)} sector(s) written, "
                          f"{len(plan) - len(diffs)} already matched.")

            print("[*] full-image read-back verification...")
            full = link.read_flash(args.offset, len(image))
            if hashlib.sha256(full).hexdigest() != digest:
                print("[!] FINAL VERIFY FAILED: read-back does not match the image.",
                      file=sys.stderr)
                return 1
            print("[+] restore complete and verified.  Power-cycle the radio to run it.")
            return 0

    except ProtocolNotVerified as e:
        print("\n[--] Protocol not yet captured -- cannot restore over UART yet:\n", file=sys.stderr)
        print(str(e), file=sys.stderr)
        return 3
    except fr_isp.FrError as e:
        print(f"\n[!] link error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
