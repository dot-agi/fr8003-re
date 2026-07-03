# 03 — Firmware features (high-level map)

What the firmware does, from the surviving strings (mostly debug/log format
strings and GATT identifiers — the build is otherwise stripped) plus the linked-in
SDK modules. The three protocols summarised here are reverse-engineered in detail in
[05](05-hid-uart-protocol.md), [06](06-ble-ota-protocol.md), and
[07](07-24ghz-link.md).

## Bluetooth LE

- Advertises as **`Akko 5075-1`** and **`HS-Bluetooth`**.
- Full GAP/GATT: pairing (`GAPC_PAIRING_REQ`, `peer auth:%x`), bonding, the Device
  Information Service (`dis read:%d`), notification enable (`ntf_enable`).
- BLE stack + RTOS are the FreqChip SDK's (`ip\ble\hl_api\gatt\gatt_api.c`,
  `modules\os\os_msg_q.c`, `os_timer.c`).

## 2.4 GHz dongle link + host interface

- Proprietary 2.4 GHz to the bundled dongle: **`HS_Dongle`**, **`HS_KB_DG`**
  ("HS keyboard dongle"). "HS" is the vendor's prefix across all three modes.
- **`HID_UART`** — HID reports shuttle to/from the WB32 over the CON3 UART. This is
  the seam keeberry speaks; **fully reversed** in [05](05-hid-uart-protocol.md).
- The on-air 2.4 GHz PHY lives in the **mask ROM**, not this flash — see
  [07](07-24ghz-link.md).

## OTA — the radio updates itself over BLE

A complete BLE OTA service (not a stub): `ota_start`/`ota_stop`,
`app_otas_recv_data`, `os_timer_ota_cb`, `ota_addr_check`, `OTA_ADDR_ERROR`,
`OTA_CHECK_FAIL`, `OTA_TIMOUT`, `crc32 check success/fail`, `REBOOT`. The received
image is written to flash, CRC32-verified, and the chip reboots into it — a
**wireless path to reflash the radio**, in addition to the CON3 ROMBOOT path used
to dump it. This **confirms** (not just "revisits") the earlier "no wireless DFU"
doubt was misplaced — that was about the WB32 and a WCH profile, not this FreqChip
OTA. Full protocol in [06](06-ble-ota-protocol.md).

## Also linked

- **BLE Mesh** provisioning code — `app_mesh_start:%d`, `M_PROV_STARTED`,
  `M_PROV_SUCCEED`, `M_PROV_FAILED`. Likely an SDK default the product does not
  use; if dead, it is reclaimable space (to be confirmed).
- Identity/markers: `FR8000`, `Freqchip`, `FREQ`, `FREQUUU`, `CHIP`, and `QRRQ`
  (the checkword bytes rendered as ASCII).

## The three protocols

| # | Protocol | Why it matters for custom wireless | Detail |
|---|---|---|---|
| 1 | HID-over-UART | the WB32 ↔ radio contract our firmware must speak | [05](05-hid-uart-protocol.md) |
| 2 | BLE OTA | a wireless delivery channel for custom radio firmware | [06](06-ble-ota-protocol.md) |
| 3 | 2.4 GHz link | what a from-scratch dongle stack would reimplement | [07](07-24ghz-link.md) |
