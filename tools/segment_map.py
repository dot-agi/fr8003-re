#!/usr/bin/env python3
"""Entropy/segment map of a raw flash image.

Classifies each block as erased, blank, code+data, or high-entropy
(packed/encrypted), and reports where real content ends. Read-only; it never
writes to or touches the device — it only reads the local image file.

    python3 tools/segment_map.py            # default image/fr8003-flash.bin
    python3 tools/segment_map.py -b 0x200   # finer 512-byte blocks
"""
import argparse
import math
import statistics
from collections import Counter


def entropy(b: bytes) -> float:
    if not b:
        return 0.0
    n = len(b)
    c = Counter(b)
    return -sum((v / n) * math.log2(v / n) for v in c.values())


def classify(b: bytes, e: float) -> str:
    if b.count(0xFF) == len(b):
        return "erased(0xFF)"
    if b.count(0x00) == len(b):
        return "blank(0x00)"
    if e > 7.2:
        return "hi-entropy(packed/enc)"
    if e > 4.5:
        return "code+data"
    if e > 1.5:
        return "struct/config"
    return "sparse"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-i", "--image", default="image/fr8003-flash.bin")
    ap.add_argument("-b", "--block", type=lambda s: int(s, 0), default=0x1000,
                    help="block size in bytes (default 0x1000)")
    a = ap.parse_args()
    d = open(a.image, "rb").read()
    n = len(d)
    print(f"image {n} bytes (0x{n:x}), overall entropy {entropy(d):.3f} bits/byte\n")

    segs = []
    for off in range(0, n, a.block):
        b = d[off:off + a.block]
        e = entropy(b)
        k = classify(b, e)
        if segs and segs[-1][2] == k:
            segs[-1][1] = off + len(b)
            segs[-1][3].append(e)
        else:
            segs.append([off, off + len(b), k, [e]])
    for s, en, k, es in segs:
        print(f"  0x{s:06x}-0x{en:06x}  {en - s:7d} B  {k:22s} e~{statistics.mean(es):.2f}")

    last = n
    while last > 0 and d[last - 1] == 0xFF:
        last -= 1
    print(f"\ncontent extent: last non-0xFF byte at 0x{last:x} ({100 * last / n:.1f}% used)")


if __name__ == "__main__":
    main()
