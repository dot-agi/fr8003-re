# 02 — Boot header (jump_table)

## Mechanism

The FR8003A has no relocated Cortex-M vector table in flash (a scan for one finds
nothing — flash offset `0x2000` is already code: `fe e7` = `b .`, `00 b5` =
`push {lr}`). Instead the mask ROM boots, then reads a **jump_table** structure
at flash offset 0 to learn the app's entry point, its RTOS/BLE configuration, and
the device address. This is `struct jump_table_t` in the SDK
(`qd_FR801xH-SDK/components/modules/platform/include/jump_table.h`).

## Decoded fields (from `tools/parse_jump_table.py`)

| Offset | Field | Value | Meaning |
|---|---|---|---|
| `0x00` | `reserved_data` | `0x00000000` | — |
| `0x04` | `image_size` | `0x00025000` | declared size (151552 B) — see note |
| `0x08` | `image_type` | `0x33333333` | **`IMAGE_TYPE_APP`** ✓ |
| `0x0c` | `entry` | `0x10002d05` | app entry, thumb → flash `0x2d04` |
| `0x10` | `memory_init_app` | `0x1001027d` | ke_mem/prf/task init → flash `0x1027c` |
| `0x14` | `stack_top_address` | `0x11004a8c` | top of stack — a valid **SRAM** pointer ✓ |
| `0x18` | `firmware_version` | `0x00000000` | unset ✓ |
| `0x24` | `param_get` | `0x100102b9` | ROM param-read fn pointer ✓ |
| `0x28` | `param_set` | `0x10010301` | ROM param-write fn pointer ✓ |
| `0x2c` | `system_option` | `0x024f040f` | RTOS/BLE feature bitmask ✓ |
| `0x57` | `bd_addr` | `11:22:33:44:55:66` | **placeholder** MAC (real one in NVDS) |
| `0x60` | `checkword` | `0x51525251` | **`JUMP_TABLE_CHECKWORD`** ✓ (ASCII "QRRQ") |

The two verified markers — `image_type == IMAGE_TYPE_APP` and
`checkword == JUMP_TABLE_CHECKWORD` — together prove this is a genuine, intact
FR801x app image.

## Notes / caveats

- **Header size = 100 bytes** (checkword at `0x60`). The reference `jump_table_t`
  in the mirrored SDK puts its checkword at `0x58`, making that struct `0x5c` = 92
  bytes; this build's header is 8 bytes longer, and the extra bytes fall in the
  timing/buffer-param region *past* `system_option`. Every field up to and
  including `system_option` (`0x2c`) reads at the reference offsets and is confirmed
  in the table above; only the later timing/buffer fields are shifted and unread.
- **`image_size = 0x25000`** defines the app's reserved partition (OTA "bank A" =
  `[0, 0x25000)`); the BLE OTA updater derives its write base as
  `image_size + 0x2000 = 0x27000` (bank B) — see [06](06-ble-ota-protocol.md). So
  it is the partition size, not the code byte count (the code+data is only ~104 KiB).
- `system_option = 0x024f040f` selects which features the ROM enables (RTOS, sleep,
  cache, QSPI mode, console UART). Bit-decoding it against `jump_table.h`'s
  `SYSTEM_OPTION_*` is a focused follow-up; RTOS-enabled is already consistent with
  the `os_*` strings.

Regenerate: `python3 tools/parse_jump_table.py`.
