#!/usr/bin/env python3
"""Freqchip FR800x / FR8003A UART boot-ROM download-protocol client.

This is the shared transport + protocol layer used by ``probe.py``, ``dump.py``
and ``restore.py`` to talk to the Akko 5075B's wireless-radio SoC (a Freqchip
FR8003A) over its 4-wire serial header (``CON3``).

------------------------------------------------------------------------------
WHAT IS CONFIRMED  (primary sources cited inline)
------------------------------------------------------------------------------
Chip / memory (FR801xH datasheet "specification V1.1" and the vendor Keil flash
algorithm ``FR8010H.FLM`` shipped in zoobab/FR801xH and qdfreqchip/FR801x-SDK):

  * Memory map (datasheet Section 21 "Memory Map"):
        0x00000000  ROM        - 128 KiB mask ROM (BLE stack + boot code)
        0x01000000  QSPI FLASH - the stacked serial flash, execute-in-place
        0x20000000  DATA RAM
        0x40000000  BLE / peripheral registers
  * The flash is memory-mapped (XIP). The vendor ``flash_read()`` routine in
    FR8010H.FLM is literally ``memcpy(dst, 0x01000000 + addr, len)`` -- reading
    flash is a plain load from the XIP window and changes no chip state.
    THIS is why a full dump is inherently non-destructive: see ``read_flash``.
    (CAUTION -- addressing base differs by family: the FR8003A is FR800x, whose
    XIP window is 0x10000000, and its ROMBOOT serial READ/WRITE/ERASE opcodes
    take 0-BASED flash offsets, NOT this XIP address -- see the RECOVERED
    section and ``FLASH_XIP_BASE`` below.)
    NOTE ON SCOPE: reading this window backs up the MAIN QSPI FLASH only.  It is
    a complete *main-flash* image, NOT a complete chip backup -- it does not
    include the EFUSE (chip unique id / trims), the flash's own OTP / security
    registers, or the 128 KiB mask ROM.  Those are captured separately, best
    effort, into the manifest (see the status/efuse/otp reads below), and the
    mask ROM is dumpable for RE only (you cannot re-flash mask ROM).
  * FlashDevice descriptor in FR8010H.FLM: base 0x01000000, size 0x00080000
    (512 KiB), page 256 B, sector 4 KiB.  Matches the FR8003A's 512 KiB flash.
  * SPI-NOR opcodes (named data symbols in FR8010H.FLM -- ``read_cmd``,
    ``write_cmd``, ``sector_erase_cmd``, ``block_erase_cmd``, ``read_id_cmd``,
    ``read_status_cmd`` ...) are the standard set encoded in ``SpiOpcode`` below.
  * UART: datasheet Section 5 -- two UARTs, 4800..921600 baud, 8N1, no flow ctl.
  * Boot ROM auto-listens on UART at power-on: Tuya's "Flash and Authorize
    FR801xH Series Modules" guide states -- "Once the chip is powered on, its
    boot program will attempt to communicate with external tools through the
    serial port. After a successful handshake is established, you can proceed
    with firmware flashing."  => power-cycle the radio to (re)enter ROMBOOT;
    there is no BOOT strap pin and no need to desolder the battery.
  * In Tuya's *documented workflow* the UART path supports a full chip erase
    ("USB to UART converter: supports flashing and full chip erase"), unlike the
    SWD path which is write-only on these parts.  (Scope this to Tuya's workflow,
    not an absolute silicon guarantee -- the vendor Keil algo actually only does
    sector erase, and its ``EraseChip`` entry is a no-op.)  The UART ROM is
    nonetheless the most robust un-brick path (freqchip forum topic 1835: after a
    botched update bricked SWD, the vendor's fix was a UART-tool full erase).

------------------------------------------------------------------------------
THE ROMBOOT WIRE PROTOCOL  (RECOVERED 2026-07-02 -- software-only RE)
------------------------------------------------------------------------------
The handshake, baud negotiation, frame layout and the read/write/erase/call
opcodes were recovered by reverse-engineering the vendor download tools -- no
logic-analyzer capture was needed.  THREE independent sources agree:

  1. Vendor 2020 tool ``FR8010H_Download_Tool.exe`` / ``fr801x_flash_en.exe`` --
     these are *PyInstaller-frozen Python 2.7 + PyQt* apps (``_MEIPASS`` cookie,
     ``python27.dll``).  Extracted with pyinstxtractor-ng and decompiled with
     uncompyle6; the whole protocol is in a clean ``uart.py`` module.  This is
     the ONLY source that implements the flash-READ (dump) path.
  2. Vendor .NET SDK ``FrDownloadSdk.dll`` v1.4.5.0 -- C# (JetBrains) reversal
     published at ``github.com/kaidegit/FreqDownloader`` (OriginalToolReverse/
     {OPCODE,FrDownload}.cs).  Authoritative for the per-family handshake tokens
     and the 800X flash base.  (This newer SDK dropped flash-READ; it verifies
     via an on-chip CRC32 command instead.)
  3. ``kaidegit/FreqDownloader`` -- a maintained Python port + ``protocol.md``.

Frame (host->chip), no checksum, no delimiters::

    opcode(1) | addr(4, little-endian) | datalen(2, little-endian) | data(datalen)

Reply (chip->host) is an ack byte (opcode-specific, see ``_ACK_CODE``) usually
followed by a 6-byte echo of the request's addr+len, and -- for reads -- then
the data.  The exact per-opcode receive framing lives in ``fr_transact``.

FR8003A specifics -- the Akko radio is the FR800x ("800X") family, NOT FR801xH:
  * handshake token is ``FR8000OK`` (FrDownload.cs ``case "800X"``), not FR801HOK;
  * its flash XIP window is ``0x10000000`` (used only by the CRC op / labels),
    NOT the 0x01000000 shown in the FR801xH memory map above;
  * the ROMBOOT READ/WRITE/BLOCK_ERASE opcodes take *0-BASED flash offsets*
    regardless of the XIP base (the 2020 tool dumps offsets 0..0x80000; the C#
    tool erases/writes from begin=0).

SAFETY MODEL (these bytes are RE-derived and hardware-UNVERIFIED):
  * The READ/dump path is non-destructive -- a wrong handshake guess simply
    fails to answer, nothing is written -- so it ships ENABLED
    (``WireConfig.read_protocol_recovered = True``) and can be attempted now.
  * The ERASE/PROGRAM path can brick on wrong bytes (freqchip forum topic 1835),
    and additionally needs the 800X write-unprotect patch (not bundled), so it
    stays behind a SECOND gate (``WireConfig.write_hardware_verified = False``):
    ``restore.py``'s erase/write refuse until a dump has round-tripped on the
    real chip.  DUMP FIRST; only then consider enabling restore.
The ``bootlog`` / ``autobaud`` / ``raw`` bring-up helpers remain available.
"""

from __future__ import annotations

import dataclasses
import struct
import sys
import time
from typing import Callable, List, Optional

# pyserial is imported lazily (see ``_serial``) so that ``--help`` and all the
# on-disk validation in restore.py / dump.py work in an environment without it;
# it is only actually required once you open a serial port to the hardware.


# --------------------------------------------------------------------------- #
# Confirmed constants (FR8010H.FLM + FR801xH datasheet)                        #
# --------------------------------------------------------------------------- #

# FR800x ("800X") flash XIP window (C# FrDownload.cs: 800X -> 0x10000000).  NB:
# the ROMBOOT flash READ/WRITE/BLOCK_ERASE opcodes take 0-BASED flash offsets;
# this XIP base is used only by the CRC-over-memory op and manifest labels.
FLASH_XIP_BASE = 0x10000000
FLASH_SIZE = 0x00080000  # 512 KiB (FlashDevice descriptor / FR8003A datasheet)
FLASH_PAGE = 0x100  # 256 B program page
FLASH_SECTOR = 0x1000  # 4 KiB erase sector
FLASH_BLOCK64 = 0x10000  # 64 KiB erase block

ROM_BASE = 0x00000000
ROM_SIZE = 0x00020000  # 128 KiB mask ROM ("ROMBOOT") -- BLE stack + boot code
SRAM_BASE = 0x20000000
SRAM_SIZE = 0x0000C000  # 48 KiB data RAM per the FR801xH map (0x20000000..); FR8003A
# public specs cite up to 64K / 56K+4K cache -- verify the exact partition. Unused by
# the flash dump.


class SpiOpcode:
    """Standard SPI-NOR opcodes, confirmed against the named symbols in
    FR8010H.FLM (``read_cmd``, ``write_cmd``, ``sector_erase_cmd`` ...).

    These describe the flash chip itself.  Whether the ROMBOOT lets us issue
    raw SPI opcodes (STIG pass-through) or only high-level memory ops is part of
    the unverified wire protocol -- but the opcodes are fixed regardless.
    """

    READ = 0x03
    PAGE_PROGRAM = 0x02
    SECTOR_ERASE_4K = 0x20
    BLOCK_ERASE_64K = 0xD8
    CHIP_ERASE = 0xC7  # standard SPI-NOR opcode; NOT an FR8010H.FLM-named symbol (its
    # EraseChip entry is a no-op). Unused by the dump; here for completeness only.
    WRITE_ENABLE = 0x06
    WRITE_DISABLE = 0x04
    VOLATILE_WRITE_ENABLE = 0x50
    WRITE_STATUS = 0x01
    READ_STATUS1 = 0x05
    READ_STATUS2 = 0x35
    JEDEC_ID = 0x9F
    DEEP_SLEEP = 0xB9
    WAKEUP = 0xAB


# Datasheet Section 5: UART supports 4800..921600.  The vendor tool defaults to
# a low rate for the initial handshake and (per esptool-class ROMs) may switch
# up afterwards.  Ordered most-likely-first for autobaud probing.
BAUD_CANDIDATES: List[int] = [115200, 921600, 1000000, 500000, 230400, 57600, 9600, 4800]


# --------------------------------------------------------------------------- #
# ROMBOOT wire protocol (RECOVERED -- see module docstring for the 3 sources)  #
# --------------------------------------------------------------------------- #

# Command opcodes.  Identical (decimal) across the 2020 Python tool and the .NET
# SDK (OPCODE.cs).  ACK code for each is in _ACK_CODE below.
OP_GET_TYPE = 0x00      # -> ack 0x01 + 4 type bytes (byte0: 1=flash, 2=eeprom)
OP_WRITE = 0x02         # program flash at a 0-based offset (payload = data)
OP_WRITE_RAM = 0x04     # write SRAM at an absolute address (payload = data)
OP_READ_ENABLE = 0x06   # unlock flash read; payload = b"MAGIC" (2020 tool only)
OP_READ = 0x08          # read flash at a 0-based offset (reply carries datalen)
OP_READ_RAM = 0x0A      # read 4 bytes of SRAM at an absolute address
OP_BLOCK_ERASE = 0x0C   # erase one 4 KiB sector at a 0-based offset
OP_CHIP_ERASE = 0x0E
OP_DISCONN = 0x10       # addr=3 reboot-to-app, 2 RAM-boot, 1 plain disconnect
OP_CHANGE_BAUD = 0x12   # addr = baud index (BAUD_INDEX)
OP_ERROR = 0x14
OP_EXE_CODE = 0x15      # call/jump to addr (thumb: addr|1); ack 0x17
OP_CRC = 0x21           # CRC32 over XIP memory (addr = absolute xip_base+offset)

_ACK_CODE = {
    OP_GET_TYPE: 0x01, OP_WRITE: 0x03, OP_WRITE_RAM: 0x05, OP_READ_ENABLE: 0x07,
    OP_READ: 0x09, OP_READ_RAM: 0x0B, OP_BLOCK_ERASE: 0x0D, OP_CHIP_ERASE: 0x0F,
    OP_DISCONN: 0x11, OP_CHANGE_BAUD: 0x13, OP_EXE_CODE: 0x17, OP_CRC: 0x22,
}

# Per-opcode reply framing as (echo6, trailing) where ``echo6`` is whether the
# ack byte is followed by a 6-byte echo of the request addr+len, and
# ``trailing`` is how many *further* bytes follow (int, or "datalen" for READ).
# Taken verbatim from the 2020 uart.py match/read expressions (the authoritative
# source for the READ path this dumper uses).
_REPLY_SPEC = {
    OP_GET_TYPE: (False, 4),
    OP_WRITE: (True, 0),
    OP_WRITE_RAM: (True, 0),
    OP_READ_ENABLE: (False, 6),
    OP_READ: (True, "datalen"),
    OP_READ_RAM: (True, 4),
    OP_BLOCK_ERASE: (True, 0),
    OP_CHIP_ERASE: (False, 5),
    OP_DISCONN: (False, 0),
    OP_CHANGE_BAUD: (False, 0),
    OP_EXE_CODE: (False, 6),
    OP_CRC: (False, 4),
}

# baud -> index carried in the CODE_CHANGE_BAUDRATE addr field.
BAUD_INDEX = {115200: 8, 230400: 9, 460800: 10, 921600: 11,
              1000000: 12, 1500000: 13, 3000000: 14, 6000000: 15}

# The Akko radio is the FR800x ("800X") family; token from FrDownload.cs.
CHIP_FAMILY = "800X"
CHIP_TOKEN = b"FR8000OK"


def _read_exact(t: "Transport", n: int) -> bytes:
    """Read exactly ``n`` bytes (looping across the transport timeout) or return
    fewer if the target went quiet."""
    buf = bytearray()
    while len(buf) < n:
        chunk = t.read(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return bytes(buf)


def fr_transact(t: "Transport", opcode: int, body: bytes = b"") -> bytes:
    """Perform one ROMBOOT command exchange; return the reply DATA payload.

    ``body`` is the frame body after the opcode: addr(4 LE) + datalen(2 LE) +
    optional data.  Returns the data bytes for READ/READ_RAM/GET_TYPE/CRC and
    b"" for commands that only ACK.  Raises FrError on a missing / mismatched
    ACK (or a short read) so nothing ever proceeds silently on a bad reply.
    No checksum, no delimiters (confirmed in all three reversed tools).
    """
    ack = _ACK_CODE.get(opcode)
    spec = _REPLY_SPEC.get(opcode)
    if ack is None or spec is None:
        raise FrError(f"fr_transact: unsupported opcode 0x{opcode:02x}")
    echo6, trailing = spec
    t.reset_buffers()
    t.write(bytes([opcode & 0xFF]) + body)

    a = _read_exact(t, 1)
    if not a or a[0] != ack:
        raise FrError(f"opcode 0x{opcode:02x}: no/bad ACK 0x{ack:02x} "
                      f"(got {a.hex() or 'nothing'})")
    if echo6:
        echo = _read_exact(t, 6)
        if len(echo) != 6 or (len(body) >= 6 and echo != body[:6]):
            raise FrError(f"opcode 0x{opcode:02x}: ACK echo mismatch "
                          f"(sent {body[:6].hex()} got {echo.hex()})")
    if trailing == "datalen":
        n = body[4] | (body[5] << 8)
        data = _read_exact(t, n)
        if len(data) != n:
            raise FrError(f"opcode 0x{opcode:02x}: short read {len(data)}/{n}")
        return data
    if trailing:
        return _read_exact(t, trailing)
    return b""


@dataclasses.dataclass
class WireConfig:
    """Wire-level parameters of the FR800x ROMBOOT serial download protocol.

    RECOVERED (2026-07-02) by reversing the vendor tools -- see the module
    docstring for the three sources and per-field confidence.  Two independent
    gates keep the destructive path off until it is proven on real silicon:

      * ``read_protocol_recovered`` -- the *non-destructive* read/dump path
        (handshake / GET_TYPE / flash READ) is recovered and safe to ATTEMPT
        (a wrong guess only fails to handshake; nothing is written).  Ships True.
      * ``write_hardware_verified`` -- the erase/program path has been proven by
        a dump -> erase -> write -> read-back round-trip on THIS chip.  Ships
        False so ``restore.py`` stays gated (it also needs the 800X
        write-unprotect patch, which is not bundled): we never erase on RE-only,
        hardware-unverified bytes.
    """

    read_protocol_recovered: bool = True
    write_hardware_verified: bool = False

    # -- handshake (HIGH; 3 sources agree; token from FrDownload.cs case "800X")
    handshake_baud: int = 115200
    boot_banner: bytes = b"freqchip"   # ROM emits this at power-on (match on b"f")
    sync_tx: bytes = CHIP_TOKEN        # host reply the ROM waits for (800X family)
    sync_ack: bytes = b"ok"            # ROM's confirmation before GET_TYPE

    # -- opcodes (HIGH; identical across the reversed tools) --
    op_read_id: int = OP_GET_TYPE
    op_read_memory: int = OP_READ
    op_write_memory: int = OP_WRITE
    op_erase: int = OP_BLOCK_ERASE
    op_call: int = OP_EXE_CODE
    op_read_enable: int = OP_READ_ENABLE
    op_change_baud: int = OP_CHANGE_BAUD
    op_crc: int = OP_CRC

    # -- flash addressing (HIGH for write/erase; MEDIUM-HIGH for read) --
    # flash device ops take 0-BASED offsets; only CRC-over-XIP uses ``xip_base``.
    flash_op_base: int = 0
    xip_base: int = FLASH_XIP_BASE     # 0x10000000 for 800X (CRC / labels only)
    comm_len: int = 256                # per-frame data chunk (COMM_LEN)

    # -- read unlock (MEDIUM-LOW; only the 2020 tool used it; best-effort) --
    read_enable_magic: bytes = b"MAGIC"

    # Single-exchange codec (see fr_transact).  Pluggable for capture-tuning.
    transact: Optional[Callable[["Transport", int, bytes], bytes]] = fr_transact


# The single global default.  Read/dump path enabled; erase/write gated.
WIRE = WireConfig()


class ProtocolNotVerified(RuntimeError):
    """Raised when a chip-driving op is attempted with an unverified WireConfig."""


class FrError(RuntimeError):
    """Any FR800x link / protocol failure."""


# --------------------------------------------------------------------------- #
# Serial transport                                                            #
# --------------------------------------------------------------------------- #

def _serial():
    """Import pyserial lazily, with a helpful error if it's missing."""
    try:
        import serial  # pyserial
    except ImportError as e:  # pragma: no cover - environment guard
        raise FrError(
            "pyserial is required for serial I/O; install it with:\n"
            "    python3 -m pip install pyserial"
        ) from e
    return serial


class Transport:
    """Thin, well-behaved wrapper over a pyserial port.

    Kept deliberately dumb: byte in, byte out, explicit timeouts.  All protocol
    knowledge lives in ``RomLink`` so the transport can be unit-reasoned and so
    the bring-up helpers can use it without any protocol assumptions.
    """

    def __init__(self, port: str, baud: int = 115200, timeout: float = 0.5):
        serial = _serial()
        self.port_name = port
        self._ser = serial.Serial()
        self._ser.port = port
        self._ser.baudrate = baud
        self._ser.bytesize = serial.EIGHTBITS
        self._ser.parity = serial.PARITY_NONE
        self._ser.stopbits = serial.STOPBITS_ONE
        self._ser.timeout = timeout
        # Do not let RTS/DTR toggle the target: on many USB-UART adapters those
        # lines are wired to a reset/boot transistor.  We drive reset by power,
        # not by handshake lines (see the planning doc).  Assign the inactive
        # states before open too, so pyserial applies them as the port opens
        # (some adapters pulse these lines during open).
        self._ser.rtscts = False
        self._ser.dsrdtr = False
        self._ser.rts = False
        self._ser.dtr = False

    def open(self) -> "Transport":
        self._ser.open()
        # Assert nothing on the modem-control lines.
        self._ser.setRTS(False)
        self._ser.setDTR(False)
        self.reset_buffers()
        return self

    def close(self) -> None:
        if self._ser.is_open:
            self._ser.close()

    def __enter__(self) -> "Transport":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def baud(self) -> int:
        return self._ser.baudrate

    @baud.setter
    def baud(self, value: int) -> None:
        self._ser.baudrate = value

    @property
    def timeout(self) -> float:
        return self._ser.timeout

    @timeout.setter
    def timeout(self, value: float) -> None:
        self._ser.timeout = value

    def reset_buffers(self) -> None:
        self._ser.reset_input_buffer()
        self._ser.reset_output_buffer()

    def write(self, data: bytes) -> int:
        n = self._ser.write(data)
        self._ser.flush()
        return n

    def read(self, n: int) -> bytes:
        return self._ser.read(n)

    def read_until_idle(self, idle: float = 0.05, cap: int = 4096) -> bytes:
        """Read whatever the target emits until it goes quiet for ``idle`` s.

        Used by the bring-up helpers to observe boot chatter / handshake replies
        without knowing the frame format.
        """
        saved = self._ser.timeout
        self._ser.timeout = idle
        out = bytearray()
        try:
            while len(out) < cap:
                chunk = self._ser.read(256)
                if not chunk:
                    break
                out += chunk
        finally:
            self._ser.timeout = saved
        return bytes(out)


# --------------------------------------------------------------------------- #
# ROM protocol link                                                           #
# --------------------------------------------------------------------------- #

class RomLink:
    """FR800x ROMBOOT download-protocol driver.

    The read path targets the flash XIP window (0x01000000) so a full read is a
    pure, non-destructive memory read.  The write/erase paths exist for
    ``restore.py`` and are guarded hard.
    """

    def __init__(self, transport: Transport, wire: WireConfig = WIRE, verbose: bool = True):
        self.t = transport
        self.wire = wire
        self.verbose = verbose
        self._read_enabled = False  # one-shot READ_ENABLE("MAGIC") latch

    # -- logging -------------------------------------------------------------
    def _log(self, msg: str) -> None:
        if self.verbose:
            sys.stderr.write(msg + "\n")

    def _require_read(self, what: str) -> None:
        """Gate the non-destructive path.  Recovered + safe -> normally passes."""
        if not self.wire.read_protocol_recovered:
            raise ProtocolNotVerified(
                f"cannot {what}: WireConfig.read_protocol_recovered is False.\n"
                "The FR800x ROMBOOT read protocol is recovered and ships enabled; "
                "if you disabled it, re-enable it or capture the handshake with "
                "`fr_isp.py bootlog/autobaud/raw`."
            )

    def _require_write(self, what: str) -> None:
        """Gate the DESTRUCTIVE path -- stays closed until proven on real silicon."""
        if not self.wire.write_hardware_verified:
            raise ProtocolNotVerified(
                f"cannot {what}: the FR800x ROMBOOT erase/program path is recovered "
                "but NOT yet hardware-verified (WireConfig.write_hardware_verified "
                "is False).\n"
                "The read/dump path IS enabled -- DUMP FIRST.  Wrong erase/program "
                "bytes can brick the radio (freqchip forum topic 1835), and the 800X "
                "write-unprotect patch (patch_unprotect_8000 @ 0x11004000) is not "
                "bundled.  Only after a full dump round-trips on THIS chip -- and you "
                "supply + validate that unprotect patch -- set write_hardware_verified "
                "= True to enable restore."
            )

    # -- bring-up helpers (safe, no protocol assumptions) --------------------

    def observe_boot(self, settle: float = 0.4) -> bytes:
        """Power-cycle window helper: return whatever the ROM emits at reset.

        READ-ONLY.  Some Freqchip ROMs print a banner; capturing it (and its
        baud) is the first clue for reversing the handshake.
        """
        self.t.reset_buffers()
        time.sleep(settle)
        return self.t.read_until_idle(idle=0.1)

    def autobaud_probe(self, probe: bytes, bauds: Optional[List[int]] = None) -> List[tuple]:
        """Send ``probe`` at each candidate baud, record any reply.

        Transmits the user-supplied ``probe`` bytes at each baud (not a passive
        read), issuing no erase/write opcode.  Returns a list of
        ``(baud, reply_bytes)`` for every baud that produced a non-empty response.
        Use this to find the handshake baud and the ROM's answer pattern.
        """
        results = []
        for b in bauds or BAUD_CANDIDATES:
            self.t.baud = b
            self.t.reset_buffers()
            self.t.write(probe)
            reply = self.t.read_until_idle(idle=0.08)
            if reply:
                results.append((b, reply))
                self._log(f"  baud {b:>7}: {len(reply)} bytes <- {reply.hex()}")
            else:
                self._log(f"  baud {b:>7}: (silence)")
        return results

    def raw(self, data: bytes, read_len: int = 256, idle: float = 0.08) -> bytes:
        """Transmit arbitrary bytes, return the reply.  For manual protocol RE.

        CAUTION: this bypasses the read/write gates -- you can hand-send a
        BLOCK_ERASE/WRITE/CHIP_ERASE frame here.  Only send read-class opcodes
        unless you accept the brick risk.
        """
        self.t.reset_buffers()
        self.t.write(data)
        if read_len:
            return self.t.read_until_idle(idle=idle, cap=max(read_len, 256))
        return b""

    # -- high-level protocol (guarded until WireConfig is verified) ----------

    def handshake(self, timeout: float = 10.0) -> bool:
        """Establish the ROMBOOT link (chip-initiated).

        The caller power-cycles the radio during this window.  The ROM emits its
        ``freqchip`` banner; we answer with the family token (``FR8000OK`` for
        the FR8003A/800X), wait for its ``ok``, then GET_TYPE to confirm boot.
        Non-destructive: a wrong token just never yields ``ok``.
        """
        self._require_read("handshake")
        self.t.baud = self.wire.handshake_baud
        # The ROM sends "freqchip" ONCE and waits only a short window for the token
        # before it boots its app. The default 0.5s read timeout answers far too
        # late, so drop it right down to reply within ~ms, and re-fire the token on
        # EVERY banner (so repeated power-cycles inside the window also get answered).
        saved_timeout = self.t.timeout
        self.t.timeout = 0.005
        self.t.reset_buffers()
        self._read_enabled = False
        banner0 = self.wire.boot_banner[:1]  # b"f"
        ack = self.wire.sync_ack             # b"ok"
        deadline = time.time() + timeout
        buf = b""
        try:
            while time.time() < deadline:
                chunk = self.t.read(64)
                if chunk:
                    buf = (buf + chunk)[-256:]
                if ack in buf:
                    self.t.timeout = saved_timeout  # restore for the GET_TYPE read
                    stype = self._get_type()
                    self._log("handshake: ROMBOOT ready (%s), storage=%s" % (
                        CHIP_FAMILY, {1: "flash", 2: "eeprom"}.get(
                            stype[0] if stype else None, "?")))
                    return True
                if banner0 in buf:
                    self.t.write(self.wire.sync_tx)
                    self._log(f"handshake: saw banner, sent {self.wire.sync_tx!r}")
                    buf = b""  # consume; re-fire only on a fresh banner
            raise FrError(
                "handshake: no ROMBOOT ok within %.1fs (banner WAS seen). Power-cycle "
                "the radio DURING this window, keep the CON3 wires steady, and confirm "
                "Pi-TX -> CON3 RX." % timeout)
        finally:
            self.t.timeout = saved_timeout

    def _get_type(self) -> bytes:
        """Send GET_TYPE, return the raw type bytes (byte0: 1=flash, 2=eeprom)."""
        return self._transact(self.wire.op_read_id, struct.pack("<IH", 0, 0))

    def _enable_read(self) -> None:
        """Unlock flash reads with the READ_ENABLE("MAGIC") one-shot.

        Best-effort: the 2020 ROM required it; the 800X ROM may not implement it.
        If it is not acknowledged we continue -- a genuinely gated read then shows
        up as an all-0x00/0xFF (BLOCKED) readout in ``assess_readout``.
        """
        if self._read_enabled:
            return
        magic = self.wire.read_enable_magic
        body = struct.pack("<IH", 0, len(magic)) + magic
        try:
            self._transact(self.wire.op_read_enable, body)
        except FrError as e:
            self._log(f"read-enable (MAGIC) not acknowledged ({e}); continuing")
        self._read_enabled = True

    def _transact(self, opcode: int, body: bytes = b"") -> bytes:
        """Perform one ROMBOOT command exchange via the (recovered) codec.

        Read-gated so nothing is transmitted unless the recovered read protocol
        is enabled.  Destructive callers additionally pass ``_require_write``.
        ``body`` is the frame body after the opcode (addr(4 LE)+len(2 LE)+data).
        """
        self._require_read("issue a ROMBOOT command")
        codec = self.wire.transact or fr_transact
        return codec(self.t, opcode, body)

    def read_chip_id(self) -> dict:
        """Return the ROM/chip identity via GET_TYPE (non-destructive).

        The reply is the storage-type record (byte0: 1=flash, 2=eeprom); the raw
        bytes are kept verbatim so the manifest is faithful regardless of decode.
        """
        resp = self._get_type()
        stype = resp[0] if resp else None
        return {
            "raw": resp.hex(),
            "len": len(resp),
            "chip_family": CHIP_FAMILY,
            "handshake_token": self.wire.sync_tx.decode("latin1"),
            "storage_type": {1: "flash", 2: "eeprom"}.get(stype, "unknown"),
        }

    def read_memory(self, offset: int, length: int) -> bytes:
        """Read ``length`` bytes of flash from 0-based ``offset`` (CODE_READ).

        The non-destructive primitive.  Chunks at COMM_LEN (256), the granularity
        the vendor tools use, and unlocks reads once with READ_ENABLE.  A short
        reply is a hard error (never silently truncated).
        """
        if length <= 0:
            return b""
        self._enable_read()
        out = bytearray()
        done = 0
        while done < length:
            n = min(self.wire.comm_len, length - done)
            body = struct.pack("<IH", self.wire.flash_op_base + offset + done, n)
            data = self._transact(self.wire.op_read_memory, body)
            if len(data) != n:
                raise FrError(f"read_memory: asked {n} bytes at flash 0x{offset + done:x}, "
                              f"got {len(data)} -- protocol mismatch or truncated reply")
            out += data
            done += n
        return bytes(out)

    def read_flash(self, offset: int, length: int,
                   progress: Optional[Callable[[int, int], None]] = None,
                   chunk: int = 4096) -> bytes:
        """Read ``length`` bytes of flash starting at flash ``offset``.

        Pure CODE_READ reads at 0-based flash offsets -> NON-DESTRUCTIVE.  Chunked
        with a progress callback for the 512 KiB full dump.
        """
        if offset < 0 or length < 0 or offset + length > FLASH_SIZE:
            raise ValueError(
                f"flash range out of bounds: offset=0x{offset:x} len=0x{length:x} "
                f"(flash is 0x{FLASH_SIZE:x} bytes)"
            )
        out = bytearray()
        done = 0
        while done < length:
            n = min(chunk, length - done)
            out += self.read_memory(offset + done, n)
            done += n
            if progress:
                progress(done, length)
        return bytes(out)

    def chip_crc32(self, offset: int, length: int) -> int:
        """Ask the ROM for the CRC32 of the XIP window [xip_base+offset, +length).

        NON-DESTRUCTIVE.  The ROM's CRC is standard (reflected, poly 0xEDB88320,
        init/xorout 0xFFFFFFFF) so ``zlib.crc32(readback) & 0xffffffff`` must
        equal it -- a cheap way to prove a dumped chunk read back faithfully and
        to confirm the 0-based READ addressing.  Best-effort: raises FrError if
        the ROM does not answer (e.g. older ROM without the CRC op).
        """
        self._require_read("compute chip CRC32")
        req = struct.pack("<II", length, 0)  # length(4) + addr(4, unused here)
        body = struct.pack("<IH", self.wire.xip_base + offset, len(req)) + req
        self.t.reset_buffers()
        self.t.write(bytes([self.wire.op_crc]) + body)
        ack = _ACK_CODE[self.wire.op_crc]
        # The ROM computes the CRC BEFORE replying and the pass scales with the
        # span, so poll on a length-derived deadline rather than a fixed idle (a
        # short idle would give up while the chip is still crunching).  Reply is
        # [ack, crc:4] or, on some builds, ASCII "CRC" then [ack, crc:4].
        deadline = time.time() + max(5.0, length / 20000.0)
        buf = bytearray()
        while time.time() < deadline:
            chunk = self.t.read(64)
            if not chunk:
                continue
            buf += chunk
            start = 3 if buf[:3] == b"CRC" else buf.find(bytes([ack]))
            if start >= 0 and len(buf) >= start + 5:
                return int.from_bytes(buf[start + 1:start + 5], "little")
        raise FrError(f"chip_crc32: no CRC ACK within deadline "
                      f"(got {bytes(buf).hex() or 'nothing'})")

    def set_baud(self, baud: int) -> None:
        """Renegotiate the link baud (CODE_CHANGE_BAUDRATE) -- non-destructive.

        Optional speed-up for the 512 KiB dump; the ROM stays in boot.  ACK
        arrives at the OLD baud, then both ends switch.
        """
        if baud not in BAUD_INDEX:
            raise ValueError(f"unsupported baud {baud}; choose from {sorted(BAUD_INDEX)}")
        # The 2020 tool matches a 1-byte CHANGE_BAUD ACK; the newer SDK waits for a
        # 7-byte frame.  Consume the 1-byte ACK, then settle at the OLD baud so any
        # trailing echo drains, switch, settle again and flush (mirrors the .NET
        # erase sequence: ack -> sleep 0.03 -> set baud -> sleep 0.12 -> flush).
        self._transact(self.wire.op_change_baud, struct.pack("<IH", BAUD_INDEX[baud], 0))
        time.sleep(0.03)
        self.t.baud = baud
        time.sleep(0.12)
        self.t.reset_buffers()

    def read_status_registers(self) -> Optional[dict]:
        """Optional provenance read of SPI-NOR SR1/SR2 (block-protect bits).

        Best-effort only: raises if no path is configured for this protocol, in
        which case probe/dump record it as absent.  Not part of the flash image.
        """
        self._require_read("read status registers")
        raise FrError("SPI-NOR status-register read is not exposed by the ROMBOOT protocol")

    def read_efuse_unique_id(self) -> Optional[bytes]:
        """Optional provenance read of the EFUSE / chip-unique-id.

        NOT in the XIP flash window and NOT restorable (never written back);
        used only for the manifest and restore's same-chip check.  Best-effort:
        raises if unconfigured.
        """
        self._require_read("read efuse / unique id")
        raise FrError("efuse/unique-id read is not exposed by the ROMBOOT protocol")

    def read_flash_otp(self, addr: int, length: int) -> Optional[bytes]:
        """Optional read of the flash OTP / security-register pages (SDK:
        flash_OTP_read).  Outside the XIP window; best-effort, raises if
        unconfigured."""
        self._require_read("read flash OTP")
        raise FrError("flash-OTP read is not exposed by the ROMBOOT protocol")

    # -- write path (restore only; extra guards live in restore.py) ----------

    def erase_sector(self, offset: int) -> None:
        """Erase the 4 KiB sector at 0-based flash ``offset`` (BLOCK_ERASE).

        DESTRUCTIVE -- gated behind ``write_hardware_verified`` (see
        ``_require_write``) and additionally needs the 800X write-unprotect patch
        loaded first (restore.py's job once enabled).
        """
        self._require_write("erase a flash sector")
        if offset % FLASH_SECTOR:
            raise ValueError("erase offset must be 4 KiB aligned")
        if offset < 0 or offset >= FLASH_SIZE:
            raise ValueError("erase offset out of bounds")
        self._transact(self.wire.op_erase,
                       struct.pack("<IH", self.wire.flash_op_base + offset, 0))

    def write_flash(self, offset: int, data: bytes,
                    progress: Optional[Callable[[int, int], None]] = None) -> None:
        """Program ``data`` at 0-based flash ``offset`` in <=COMM_LEN pages (WRITE).

        DESTRUCTIVE -- gated behind ``write_hardware_verified``.  The caller
        (restore.py) owns unprotect, erase, ordering and verify.
        """
        self._require_write("program flash")
        if offset < 0 or offset + len(data) > FLASH_SIZE:
            raise ValueError("write range out of bounds")
        done = 0
        while done < len(data):
            n = min(self.wire.comm_len, len(data) - done)
            self._transact(
                self.wire.op_write_memory,
                struct.pack("<IH", self.wire.flash_op_base + offset + done, n)
                + data[done:done + n],
            )
            done += n
            if progress:
                progress(done, len(data))


# --------------------------------------------------------------------------- #
# Readout health analysis (shared by probe.py and dump.py)                    #
# --------------------------------------------------------------------------- #

def shannon_entropy(data: bytes) -> float:
    """Bits/byte of Shannon entropy (0..8).  ~8.0 means indistinguishable from
    random -- the fingerprint of an AES-encrypted / scrambled flash readout.
    """
    if not data:
        return 0.0
    from math import log2
    counts = [0] * 256
    for b in data:
        counts[b] += 1
    n = len(data)
    return -sum((c / n) * log2(c / n) for c in counts if c)


def assess_readout(image_head: bytes, full_entropy: Optional[float] = None) -> dict:
    """Judge whether a flash readout looks plaintext, blocked, or encrypted.

    Heuristics, in order of confidence:
      * A readout that is entirely 0x00 or 0xFF => the ROM refused to read (or
        the flash is blank / read-protected): BLOCKED.
      * Whole-image entropy ~8.0 bits/byte with no 0xFF padding => the readout is
        ENCRYPTED/scrambled (Akko may have enabled code encryption).
      * Otherwise (structured, not blank, not random) => UNCERTAIN.  This
        heuristic canNOT prove the readout is faithful firmware; the authoritative
        check is the ROM CRC round-trip (chip_crc32 == zlib.crc32(readback)).

    Returns a dict: {verdict, entropy_head, notes:[...]}.  BLOCKED/ENCRYPTED are
    hard warnings (probe.py fails loud); UNCERTAIN defers to the CRC round-trip
    that probe.py/dump.py run to actually prove the read is faithful.
    """
    notes: List[str] = []
    head = image_head or b""
    ent_head = shannon_entropy(head)

    if head and all(b == 0x00 for b in head):
        return {"verdict": "BLOCKED", "entropy_head": ent_head,
                "notes": ["first bytes are all 0x00 -- ROM read refused or flash blank"]}
    if head and all(b == 0xFF for b in head):
        return {"verdict": "BLOCKED", "entropy_head": ent_head,
                "notes": ["first bytes are all 0xFF -- flash blank or read refused"]}

    # FR800x images begin with a jump table / image_info struct (see the OTA code
    # on the freqchip forum: struct image_info_t at flash offset 0 carries
    # image_length, image_storage_offset, crc, image_valid).  Real firmware has
    # a non-trivial, non-random header; encrypted readouts do not.
    looks_random = ent_head > 7.5 and (full_entropy is None or full_entropy > 7.5)
    if looks_random:
        notes.append(f"header entropy {ent_head:.2f} bits/byte (near-random) -- "
                     "readout may be ENCRYPTED (code encryption enabled?)")
        return {"verdict": "ENCRYPTED", "entropy_head": ent_head, "notes": notes}

    # Structured, not blank, not random -- but a heuristic can't prove the bytes
    # are faithful firmware.  Return UNCERTAIN and let the CRC round-trip decide;
    # never emit an over-confident "PLAINTEXT" that downstream marks restorable.
    notes.append(f"header entropy {ent_head:.2f} bits/byte -- structured (not blank/"
                 "random); confirm faithfulness via the CRC round-trip")
    return {"verdict": "UNCERTAIN", "entropy_head": ent_head, "notes": notes}


# --------------------------------------------------------------------------- #
# Bring-up CLI  (safe, read-mostly -- for capturing the handshake)            #
# --------------------------------------------------------------------------- #

def _add_common(p) -> None:
    p.add_argument("-p", "--port", required=True, help="serial device, e.g. /dev/tty.usbserial-XXXX")
    p.add_argument("-b", "--baud", type=int, default=115200, help="baud (default 115200)")


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        description="FR800x/FR8003A ROMBOOT client + protocol bring-up tools. "
                    "The sub-commands here are the SAFE, read-mostly helpers you "
                    "use to capture the (unverified) handshake; probe/dump/restore "
                    "are separate scripts."
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_boot = sub.add_parser("bootlog", help="print whatever the ROM emits at reset (power-cycle the radio)")
    _add_common(p_boot)

    p_auto = sub.add_parser("autobaud", help="send a probe byte at every candidate baud, log replies")
    _add_common(p_auto)
    p_auto.add_argument("--probe", default="55", help="hex byte(s) to send (default 55)")

    p_raw = sub.add_parser("raw", help="send arbitrary hex bytes, print the reply (manual RE)")
    _add_common(p_raw)
    p_raw.add_argument("data", help="hex bytes to transmit, e.g. 'aa5501'")

    args = ap.parse_args(argv)

    with Transport(args.port, baud=args.baud) as t:
        link = RomLink(t)
        if args.cmd == "bootlog":
            sys.stderr.write("Power-cycle the radio now; listening...\n")
            data = link.observe_boot()
            sys.stderr.write(f"captured {len(data)} bytes:\n")
            sys.stdout.write(data.hex() + "\n")
            if data:
                sys.stdout.write(repr(data) + "\n")
        elif args.cmd == "autobaud":
            probe = bytes.fromhex(args.probe)
            sys.stderr.write("Power-cycle the radio during this sweep.\n")
            hits = link.autobaud_probe(probe)
            sys.stderr.write(f"{len(hits)} baud(s) answered.\n")
        elif args.cmd == "raw":
            reply = link.raw(bytes.fromhex(args.data))
            sys.stdout.write(reply.hex() + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
