"""Hex output: two hex digits per 8 samples and channel.

Ported from libsigrok src/output/hex.c (receive(), gen_header()), including
its quirks: with a width that is not a multiple of 8 the trailing samples
of each line are not printed, and the final partial byte on END is taken
from an 8-bit shift register that spans line boundaries (so it can print
more than two digits).
"""

from __future__ import annotations

from typing import BinaryIO, Mapping, Optional

import numpy as np

from ._common import assemble, channel_bits, const_block
from ._lines import LineOutput

_HEX = np.frombuffer(b"0123456789abcdef", dtype=np.uint8)


class HexOutput(LineOutput):
    NAME = "hex"
    DEFAULT_WIDTH = 192  # DEFAULT_SAMPLES_PER_LINE

    def __init__(self, out: BinaryIO, options: Optional[Mapping[str, object]] = None):
        super().__init__(out, options)
        # Up to 8 samples preceding the pending line, and how many samples
        # were shifted into sample_buf since its last output.
        self.tail = np.zeros(0, dtype=np.uint16)
        self.since = 0

    def _hex_bytes(self, rows: np.ndarray, index: int) -> np.ndarray:
        n, k = rows.shape
        nb = k // 8
        packed = np.packbits(channel_bits(rows[:, : nb * 8], index), axis=1)
        chars = np.empty((n, nb, 3), dtype=np.uint8)
        chars[:, :, 0] = _HEX[packed >> 4]
        chars[:, :, 1] = _HEX[packed & 15]
        chars[:, :, 2] = ord(" ")
        return chars.reshape(n, nb * 3)

    def _format_lines(self, full: np.ndarray) -> bytes:
        n = full.shape[0]
        if n == 0:
            return b""
        blocks = []
        assert self.meta is not None
        for prefix, ch in zip(self.prefixes, self.meta.channels):
            blocks += [const_block(n, prefix), (self._hex_bytes(full, ch.index), None),
                       const_block(n, b"\n")]
        return assemble(blocks)

    def _after_flush(self, full: np.ndarray) -> None:
        if self.spl >= 8:
            self.since = self.spl % 8
        else:
            self.since += full.size
        self.tail = np.concatenate([self.tail, full.reshape(-1)[-8:]])[-8:]

    def _marker_offset(self, trig: int) -> int:
        # One character per nibble plus one separator per byte.
        return trig // 4 + trig // 8

    def _format_end(self, data: np.ndarray) -> bytes:
        k = data.size
        rem = k & 7
        hist = None
        if rem:
            m = rem if k >= 8 else self.since + k
            hist = np.concatenate([self.tail, data])[-min(m, 8):]
        lines = []
        assert self.meta is not None
        for prefix, ch in zip(self.prefixes, self.meta.channels):
            line = prefix + self._hex_bytes(data.reshape(1, -1), ch.index).tobytes()
            if hist is not None:
                buf = 0
                for bit in channel_bits(hist, ch.index):
                    buf = (buf << 1) | int(bit)
                line += b"%.2x " % (buf << (8 - rem))
            lines.append(line + b"\n")
        return b"".join(lines)
