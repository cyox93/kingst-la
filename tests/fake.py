"""A scripted stand-in for usb.Transport that models the LA5016 registers."""

from __future__ import annotations

import struct

from la5016 import device as d


def transfers(packets, seq0=0):
    """Pack (pins, repeat) pairs into 16-byte transfers (5 packets + sequence byte)."""
    assert len(packets) % 5 == 0
    out = b""
    for t in range(len(packets) // 5):
        for pins, rep in packets[5 * t:5 * t + 5]:
            out += struct.pack("<HB", pins, rep)
        out += bytes([(seq0 + t) & 0xFF])
    return out


class FakeTransport:
    def __init__(self, *, magic=b"\x05\xfa\x10\xef\x05\xfa\x10\xef", fpga_loaded=True,
                 capture=b"", before_trigger=0, polls_until_idle=2):
        self.log = []
        self.magic = magic
        self.fpga_loaded = fpga_loaded
        self.capture = capture
        self.before_trigger = before_trigger
        self.polls_until_idle = polls_until_idle
        self.running = False
        self.polls = 0
        self.closed = False
        self.bitstream = b""

    # Transport API

    def ctrl_in(self, request, value, index, length):
        self.log.append(("in", request, value, index, length))
        if request == d.CMD_EEPROM and value == 0x20:
            return b"\x21\x05\xde\xfa"
        if request == d.CMD_EEPROM and value == 0x08:
            return self.magic
        if request == d.CMD_FPGA_INIT:
            return b"\x00"
        if request == d.CMD_FPGA_SPI and value == d.REG_RUN:
            if not self.fpga_loaded:
                return b"\x00\x00" if False else b"\xff\xff"
            if self.running:
                self.polls += 1
                if self.polls >= self.polls_until_idle:
                    self.running = False
                return struct.pack("<H", 0x85E2 if self.running else 0x85E9)
            return struct.pack("<H", 0x85E9)
        if request == d.CMD_FPGA_SPI and value == d.REG_PWM_EN:
            return b"\x00"
        if request == d.CMD_FPGA_SPI and value == d.REG_SAMPLING:
            n_packets = len(self.capture) // 16 * 5
            return struct.pack("<III", n_packets, self.before_trigger, 0x1000) + bytes(4)
        raise AssertionError("unexpected ctrl_in %r" % ((request, value, index, length),))

    def ctrl_out(self, request, value, index, data=b""):
        self.log.append(("out", request, value, index, bytes(data)))
        if request == d.CMD_FPGA_SPI and value == d.REG_RUN:
            self.running = data == bytes([d.RUNMODE_RUN])
            self.polls = 0

    def bulk_write(self, ep, data):
        self.log.append(("bulk_write", ep, len(data)))
        self.bitstream += data
        self.fpga_loaded = True

    def bulk_read_stream(self, ep, nbytes, chunk):
        self.log.append(("bulk_read", ep, nbytes))
        data = self.capture[:nbytes]
        for off in range(0, len(data), 7):  # odd chunking on purpose
            yield data[off:off + 7]

    def close(self):
        self.closed = True

    # helpers

    def writes(self, reg):
        return [e[4] for e in self.log if e[0] == "out" and e[1] == d.CMD_FPGA_SPI and e[2] == reg]
