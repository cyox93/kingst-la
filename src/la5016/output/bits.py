"""Bits output: one '0'/'1' digit per sample and channel.

Ported from libsigrok src/output/bits.c (receive(), gen_header()).
"""

from __future__ import annotations

import numpy as np

from ._common import assemble, channel_bits, const_block
from ._lines import LineOutput


class BitsOutput(LineOutput):
    NAME = "bits"
    DEFAULT_WIDTH = 64  # DEFAULT_SAMPLES_PER_LINE

    def _bit_lines(self, rows: np.ndarray, trailing_space: bool) -> bytes:
        n, k = rows.shape
        if n == 0:
            return b""
        # A space follows every 8th bit, except the one that ends a full line.
        width = k + (k // 8 if trailing_space else (k - 1) // 8)
        cols = np.arange(k)
        pos = cols + cols // 8
        blocks = []
        assert self.meta is not None
        for prefix, ch in zip(self.prefixes, self.meta.channels):
            body = np.full((n, width), ord(" "), dtype=np.uint8)
            body[:, pos] = channel_bits(rows, ch.index) + ord("0")
            blocks += [const_block(n, prefix), (body, None), const_block(n, b"\n")]
        return assemble(blocks)

    def _format_lines(self, full: np.ndarray) -> bytes:
        return self._bit_lines(full, trailing_space=False)

    def _marker_offset(self, trig: int) -> int:
        # One character per bit plus one separator per byte.
        return trig + trig // 8

    def _format_end(self, data: np.ndarray) -> bytes:
        return self._bit_lines(data.reshape(1, -1), trailing_space=True)
