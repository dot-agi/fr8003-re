#!/usr/bin/env python3
"""Decode the FR801x jump_table header at the start of the FR8003A flash image.

The FreqChip FR801x boot ROM reads this structure (`struct jump_table_t` in the
SDK's platform/include/jump_table.h) to find the app entry point, RTOS/BLE
config, and the device address. Offset 0 of the flash IS this header; the app
code follows it. Addressing is XIP: flash file offset N maps to CPU address
0x10000000 + N. Read-only.
"""
import argparse
import struct

XIP_BASE = 0x10000000
CHECKWORD = 0x51525251  # JUMP_TABLE_CHECKWORD
IMAGE_TYPES = {
    0x00000000: "CONTROLLER",
    0x11111111: "HOST",
    0x22222222: "FULL",
    0x33333333: "APP",
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-i", "--image", default="image/fr8003-dump.bin")
    a = ap.parse_args()
    d = open(a.image, "rb").read()

    def u32(o):
        return struct.unpack_from("<I", d, o)[0]

    def addr(o):  # XIP pointer -> (value, file offset, thumb bit)
        v = u32(o)
        return v, v - XIP_BASE, v & 1

    reserved, image_size, image_type = u32(0), u32(4), u32(8)
    entry, entry_off, entry_t = addr(12)
    mem, mem_off, _ = addr(16)

    print(f"reserved_data     0x{reserved:08x}")
    print(f"image_size        0x{image_size:08x}  ({image_size} bytes)")
    print(f"image_type        0x{image_type:08x}  {IMAGE_TYPES.get(image_type, '??')}")
    print(f"entry             0x{entry:08x}  -> file 0x{entry_off:05x}  thumb={entry_t}")
    print(f"memory_init_app   0x{mem:08x}  -> file 0x{mem_off:05x}")

    off = d.find(struct.pack("<I", CHECKWORD))
    if off < 0:
        print("checkword 0x51525251 NOT found -- image is not a jump_table APP image")
        return 1
    # bd_addr (6 bytes) sits just before the 4-aligned checkword (3 pad bytes).
    mac = d[off - 9:off - 3]
    print(f"checkword         0x{CHECKWORD:08x}  at header offset 0x{off:02x}  "
          f"(header is {off + 4} bytes)")
    print(f"bd_addr (MAC)     {':'.join(f'{b:02x}' for b in reversed(mac))}  "
          f"(placeholder if 11:22:33:44:55:66 -- real address lives in NVDS)")
    print(f"XIP mapping       file 0x00000 == CPU 0x{XIP_BASE:08x}; app code begins ~0x02000")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
