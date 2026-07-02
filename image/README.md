# image/

The factory firmware binary (`fr8003-dump.bin`) is **not included** in this public
repo -- it is Akko's proprietary firmware and is not ours to redistribute. What is
here reproduces and verifies an identical copy:

- `fr8003-dump.bin.sha256` -- SHA-256 of the exact image the docs describe.
- `fr8003-dump.manifest.json` -- provenance (chip id, CRC, size, readout verdict).

Dump your own board and check it matches:

```bash
python3 ../tools/dump.py -p /dev/tty.usbserial-XXXX -o fr8003-dump.bin
shasum -a 256 -c fr8003-dump.bin.sha256   # must print OK
```
