"""Kingst LA5016 device protocol: identification, FPGA setup, capture, download.

Ported from libsigrok src/hardware/kingst-la2016 (protocol.c, api.c),
Copyright (C) 2020 Florian Schmidt, 2021 Gerhard Sittig and others, GPL-3.0-or-later.
Only the LA5016 entries and the SDRAM capture mode are carried over.
"""

from __future__ import annotations

import logging
import struct
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterator, Optional

from la5016 import firmware
from la5016.usb import EP_BITSTREAM, EP_CAPTURE, Transport

log = logging.getLogger(__name__)

CMD_FPGA_ENABLE = 0x10
CMD_FPGA_SPI = 0x20
CMD_BULK_START = 0x30
CMD_BULK_RESET = 0x38
CMD_FPGA_INIT = 0x50
CMD_EEPROM = 0xA2

REG_RUN = 0x00
REG_PWM_EN = 0x02
REG_CAPT_MODE = 0x03
REG_BULK = 0x08
REG_SAMPLING = 0x10
REG_TRIGGER = 0x20
REG_THRESHOLD = 0x68

CAPTMODE_TO_RAM = 0x00
RUNMODE_HALT = 0x00
RUNMODE_RUN = 0x03

RUNSTATE_IDLE = 1 << 0
RUNSTATE_TRGD = 1 << 2
RUNSTATE_POST = 1 << 3

EP2_PADDING = 4096
BULK_CHUNK = 512 * 1024
POLL_INTERVAL = 0.05

NUM_SAMPLES_MAX = 10 * 1000 * 1000 * 1000
BASECLOCK = 800_000_000
MAX_SAMPLERATE = 500_000_000
MEMORY_BITS = 2  # Gbit

# 16-channel memory layout: 5 x (u16 pins, u8 repeat) + u8 sequence per 16 bytes.
TRANSFER_SIZE = 16
PACKETS_PER_TRANSFER = 5

# (magic, magic2, fpga stem); magic2 0 matches any value.
MODELS = (
    (0x05, 0x00, "la5016a1"),
    (0x0C, 0x10, "la5016a2"),
    (0x0C, 0x00, "la5016a2"),
    (0x41, 0x00, "la5016a1"),
)


class DeviceError(Exception):
    pass


@dataclass
class Identity:
    magic: int
    magic2: int
    stem: str
    manufactured: Optional[str]  # "YYYY-MM", None if unreadable or inconsistent

    @property
    def bitstream(self) -> str:
        return firmware.fpga_bitstream_name(self.stem)


@dataclass
class CaptureInfo:
    n_rep_packets: int
    n_rep_packets_before_trigger: int
    write_pos: int


@dataclass
class CaptureConfig:
    samplerate: int = MAX_SAMPLERATE
    limit_samples: Optional[int] = None       # None: until memory is full or stopped
    time_ms: Optional[int] = None
    captureratio: int = 50
    threshold: float = 1.4
    channel_mask: int = 0xFFFF
    triggers: Dict[int, str] = field(default_factory=dict)  # bit -> '0' '1' 'r' 'f'


@dataclass
class Capture:
    samplerate: int                     # effective rate
    trigger_packets: Optional[int]      # RLE packets before the trigger, None without trigger
    nbytes: int
    chunks: Iterator[bytes]


def lookup_model(magic: int, magic2: int) -> Optional[str]:
    for m, m2, stem in MODELS:
        if m == magic and (not m2 or m2 == magic2):
            return stem
    return None


def decode_magic(buf: bytes) -> tuple:
    """EEPROM bytes at 0x08: magic, ~magic, magic2, ~magic2, then a backup copy."""
    if buf[0] ^ buf[1] == 0xFF and buf[2] ^ buf[3] == 0xFF:
        return buf[0], buf[2]
    if buf[4] ^ buf[5] == 0xFF and buf[6] ^ buf[7] == 0xFF:
        return buf[4], buf[6]
    if buf[0] ^ buf[1] == 0xFF:
        return buf[0], 0
    if buf[4] ^ buf[5] == 0xFF:
        return buf[4], 0
    return 0, 0


def threshold_registers(voltage: float) -> bytes:
    """Duty cycles of the two PWMs feeding the threshold DAC (R56, R79)."""
    voltage = max(-4.0, min(4.0, voltage))
    if voltage >= 2.9:
        r79, r56 = 0, int(302 * voltage - 363)
    elif voltage > -0.4:
        r79, r56 = 0x00F2, int(302 * voltage + 121)
    else:
        r79, r56 = 0x02D7, int(302 * voltage + 1090)
    r56 = max(10, min(1100, r56))
    return struct.pack("<HH", r56, r79)


def trigger_registers(channel_mask: int, triggers: Dict[int, str]) -> bytes:
    enabled = level = high_or_falling = 0
    for ch, cond in triggers.items():
        bit = 1 << ch
        if cond in "rf" and enabled & ~level:
            raise DeviceError("device only supports one edge trigger")
        if cond in "01":
            level |= bit
        else:
            level &= ~bit
        if cond in "1f":
            high_or_falling |= bit
        else:
            high_or_falling &= ~bit
        enabled |= bit
    return struct.pack("<IIII", channel_mask, enabled, level, high_or_falling)


def sample_registers(samplerate: int, limit_samples: Optional[int], captureratio: int,
                     trigger_involved: bool) -> bytes:
    if samplerate > MAX_SAMPLERATE:
        raise DeviceError("samplerate too high: %d" % samplerate)
    divider = BASECLOCK // samplerate
    if not 1 <= divider <= 0xFFFF:
        raise DeviceError("samplerate too low: %d" % samplerate)
    limit = min(limit_samples or NUM_SAMPLES_MAX, NUM_SAMPLES_MAX)
    if trigger_involved:
        pre_samples = limit * captureratio // 100
        pre_memory = MEMORY_BITS * 1024 ** 3 // 8 * captureratio // 100
    else:
        pre_samples = pre_memory = 0
    pre_memory = max(pre_memory, 0x100)
    return (limit.to_bytes(5, "little") + pre_samples.to_bytes(5, "little")
            + (pre_memory >> 8).to_bytes(3, "little") + struct.pack("<HB", divider, 0))


def effective_samplerate(samplerate: int) -> int:
    return min(BASECLOCK // (BASECLOCK // samplerate), MAX_SAMPLERATE)


class Device:
    def __init__(self, transport: Transport):
        self.t = transport
        self.identity: Optional[Identity] = None

    # --- registers -------------------------------------------------------

    def _spi_read(self, reg: int, length: int) -> bytes:
        return self.t.ctrl_in(CMD_FPGA_SPI, reg, 0, length)

    def _spi_write(self, reg: int, data: bytes, index: int = 0) -> None:
        self.t.ctrl_out(CMD_FPGA_SPI, reg, index, data)

    def run_state(self) -> int:
        return struct.unpack("<H", self._spi_read(REG_RUN, 2))[0]

    def is_idle(self) -> bool:
        return bool(self.run_state() & RUNSTATE_IDLE)

    def set_run_mode(self, mode: int) -> None:
        self._spi_write(REG_RUN, bytes([mode]))

    # --- identification and FPGA ----------------------------------------

    def identify(self) -> Identity:
        manufactured = None
        try:
            yy, mm, iyy, imm = self.t.ctrl_in(CMD_EEPROM, 0x20, 0, 4)
            if yy ^ iyy == 0xFF and mm ^ imm == 0xFF:
                manufactured = "20%02x-%02x" % (yy, mm)
            else:
                log.warning("manufacture date fails checksum test")
        except Exception as e:  # non-fatal, as in sigrok
            log.warning("cannot read manufacture date: %s", e)
        buf = self.t.ctrl_in(CMD_EEPROM, 0x08, 0, 8)
        log.debug("EEPROM magic bytes %s", buf.hex())
        magic, magic2 = decode_magic(buf)
        stem = lookup_model(magic, magic2)
        if stem is None:
            raise DeviceError("unsupported model: EEPROM magic %#04x/%#04x (bytes %s)"
                              % (magic, magic2, buf.hex()))
        self.identity = Identity(magic, magic2, stem, manufactured)
        log.info("model LA5016 (%s), magic %#04x/%#04x", stem, magic, magic2)
        return self.identity

    def fpga_ready(self) -> bool:
        """sigrok check_fpga_bitstream(): can the loaded bitstream be reused?"""
        try:
            if self.t.ctrl_in(CMD_FPGA_INIT, 0, 0, 1)[0] != 0:
                return False
            state = self.run_state()
            if state and ((state & 0x3) != 0x1 or (state & ~0xF) != 0x85E0):
                return False
            return (self._spi_read(REG_PWM_EN, 1)[0] & 0x3) == 0
        except Exception as e:
            log.debug("FPGA check failed: %s", e)
            return False

    def upload_bitstream(self, data: bytes) -> None:
        log.info("uploading FPGA bitstream (%d bytes)", len(data))
        self.t.ctrl_out(CMD_FPGA_INIT, 0, 0, struct.pack("<I", len(data)))
        padded = -(-len(data) // EP2_PADDING) * EP2_PADDING
        data = data + bytes(padded - len(data))
        for off in range(0, padded, EP2_PADDING):
            self.t.bulk_write(EP_BITSTREAM, data[off:off + EP2_PADDING])

    def enable_bitstream(self) -> None:
        resp = self.t.ctrl_in(CMD_FPGA_INIT, 0, 0, 1)[0]
        if resp != 0:
            raise DeviceError("unexpected FPGA init response %#04x" % resp)
        time.sleep(0.030)
        self.t.ctrl_out(CMD_FPGA_ENABLE, 0x01, 0)
        time.sleep(0.040)

    def init_hardware(self, load_bitstream: Callable[[str], bytes] = firmware.load) -> None:
        ident = self.identity or self.identify()
        if not self.fpga_ready():
            self.upload_bitstream(load_bitstream(ident.bitstream))
        else:
            log.info("reusing loaded FPGA bitstream")
        self.enable_bitstream()
        state = self.run_state()
        if state & 0xFFF0 != 0x85E0:
            log.warning("unexpected run state %#06x, want 0x85eX", state)
        self.t.ctrl_out(CMD_BULK_RESET, 0, 0)

    # --- capture ---------------------------------------------------------

    def configure(self, cfg: CaptureConfig) -> bool:
        """Write threshold, capture mode, trigger and sampling setup. Returns trigger_involved."""
        self._spi_write(REG_THRESHOLD, threshold_registers(cfg.threshold))
        self._spi_write(REG_CAPT_MODE, bytes([CAPTMODE_TO_RAM]))
        if not cfg.channel_mask:
            raise DeviceError("need at least one enabled channel")
        self._spi_write(REG_TRIGGER, trigger_registers(cfg.channel_mask, cfg.triggers), index=16)
        involved = bool(cfg.triggers)
        self._spi_write(REG_SAMPLING, sample_registers(cfg.samplerate, cfg.limit_samples,
                                                       cfg.captureratio, involved))
        return involved

    def capture_info(self) -> CaptureInfo:
        n_rep, n_before, write_pos = struct.unpack_from("<III", self._spi_read(REG_SAMPLING, 16))
        log.debug("capture info: packets %d, before trigger %d, write_pos %#x", n_rep, n_before, write_pos)
        if n_rep % PACKETS_PER_TRANSFER:
            log.warning("packet count %d is not a multiple of %d", n_rep, PACKETS_PER_TRANSFER)
        return CaptureInfo(n_rep, n_before, write_pos)

    def start_download(self, info: CaptureInfo) -> int:
        nbytes = info.n_rep_packets // PACKETS_PER_TRANSFER * TRANSFER_SIZE
        read_pos = (info.write_pos - nbytes) & 0xFFFFFFFF
        self.t.ctrl_out(CMD_BULK_RESET, 0, 0)
        self._spi_write(REG_BULK, struct.pack("<II", read_pos, nbytes))
        self.t.ctrl_out(CMD_BULK_START, 0, 0)
        return nbytes

    def capture(self, cfg: CaptureConfig, should_stop: Callable[[], bool] = lambda: False,
                on_state: Callable[[int], None] = lambda s: None) -> Capture:
        """Run one SDRAM capture to completion and return the download stream.

        `should_stop` is polled while the hardware samples; once it returns True
        (or `cfg.time_ms` elapses) the capture is halted and what was sampled so
        far is downloaded, like sigrok's msec limit.
        """
        involved = self.configure(cfg)
        self.set_run_mode(RUNMODE_RUN)
        start = time.monotonic()
        halted = False
        while True:
            state = self.run_state()
            on_state(state)
            if state & RUNSTATE_IDLE:
                break
            timed_out = cfg.time_ms is not None and (time.monotonic() - start) * 1000 >= cfg.time_ms
            if not halted and (timed_out or should_stop()):
                log.info("stopping acquisition")
                self.set_run_mode(RUNMODE_HALT)
                halted = True
            time.sleep(POLL_INTERVAL)
        info = self.capture_info()
        nbytes = self.start_download(info)
        log.info("downloading %d bytes", nbytes)
        return Capture(
            samplerate=effective_samplerate(cfg.samplerate),
            trigger_packets=info.n_rep_packets_before_trigger if involved else None,
            nbytes=nbytes,
            chunks=self.t.bulk_read_stream(EP_CAPTURE, nbytes, BULK_CHUNK),
        )

    def close(self) -> None:
        try:
            self.set_run_mode(RUNMODE_HALT)
        except Exception as e:
            log.debug("halt on close failed: %s", e)
        self.t.close()


def open_device(conn=None, load_firmware: Callable[[str], bytes] = firmware.load) -> Device:
    """Find the LA5016, load FX2 firmware if needed, identify it and set up the FPGA."""
    from la5016 import usb as lusb

    cand = lusb.select(conn)
    if not cand.has_firmware:
        image = load_firmware(firmware.mcu_firmware_name(cand.pid))
        log.info("uploading FX2 firmware to %s", cand.conn)
        lusb.upload_fx2_firmware(cand, image)
        cand = lusb.wait_renumerated(cand.bus, cand.port_path, time.monotonic())
    dev = Device(lusb.PyUsbTransport(cand))
    try:
        dev.identify()
        dev.init_hardware(load_firmware)
    except BaseException:
        dev.t.close()
        raise
    return dev
