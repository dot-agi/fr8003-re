# image/

Neither binary is included in this public repo — the **flash** image is Akko's
proprietary firmware, and the **mask ROM** is FreqChip's silicon ROM; neither is
ours to redistribute. What is here reproduces and verifies identical copies:

- `fr8003-flash.bin.sha256` / `fr8003-flash.manifest.json` — the 512 KiB QSPI flash
  (Akko's application); provenance in the manifest.
- `fr8003-maskrom.bin.sha256` / `fr8003-maskrom.manifest.json` — the 128 KiB mask
  ROM (FreqChip's radio stack + boot); see [`../docs/09`](../docs/09-mask-rom.md).

Dump your own board and check they match:

```bash
python3 ../tools/dump.py     -p /dev/tty.usbserial-XXXX -o fr8003-flash.bin
python3 ../tools/dump_rom.py -p /dev/tty.usbserial-XXXX -o fr8003-maskrom.bin
shasum -a 256 -c fr8003-flash.bin.sha256      # must print OK
shasum -a 256 -c fr8003-maskrom.bin.sha256   # must print OK
```
