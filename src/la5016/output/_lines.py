"""Common line buffering of the bits and hex formats.

Follows the shared structure of libsigrok src/output/bits.c and
src/output/hex.c: header on the first logic packet, `width` samples per
line, trigger marker after the next full-line flush, partial lines on END.
"""

from __future__ import annotations

from typing import BinaryIO, List, Mapping, Optional

import numpy as np

from ..pipeline import Meta
from ._common import (DEVICE_CHANNELS, PACKAGE_VERSION, as_samples,
                      parse_options, samplerate_string)


class LineOutput:
    NAME = ""
    DEFAULT_WIDTH = 64

    def __init__(self, out: BinaryIO, options: Optional[Mapping[str, object]] = None):
        opts = parse_options(self.NAME, {"width": ("uint32", self.DEFAULT_WIDTH)}, options)
        self.out = out
        width = int(opts["width"])  # type: ignore[arg-type]
        # C stores it in an int: values >= 2^31 never match spl_cnt, like 0.
        self.spl = width if width < 2**31 else 0
        self.meta: Optional[Meta] = None
        self.header_done = False
        self.trigger_pos = -1
        self.pending: List[np.ndarray] = []
        self.pending_len = 0

    # -- Sink ---------------------------------------------------------------

    def begin(self, meta: Meta) -> None:
        self.meta = meta
        self.prefixes = [f"{ch.name}:".encode() for ch in meta.channels]

    def feed(self, samples: np.ndarray) -> None:
        samples = as_samples(samples)
        parts = []
        if not self.header_done:
            parts.append(self._header())
            self.header_done = True
        if samples.size:
            self.pending.append(samples)
            self.pending_len += samples.size
        if self.spl and self.pending_len >= self.spl:
            data = np.concatenate(self.pending)
            nfull = data.size // self.spl
            full = data[: nfull * self.spl].reshape(nfull, self.spl)
            rest = data[nfull * self.spl:]
            if self.trigger_pos > -1:
                parts.append(self._format_lines(full[:1]))
                parts.append(b"T:%s^ %d\n" % (b" " * self._marker_offset(self.trigger_pos),
                                              self.trigger_pos))
                self.trigger_pos = -1
                parts.append(self._format_lines(full[1:]))
            else:
                parts.append(self._format_lines(full))
            self._after_flush(full)
            self.pending = [rest] if rest.size else []
            self.pending_len = rest.size
        data_out = b"".join(parts)
        if data_out:
            self.out.write(data_out)

    def trigger(self) -> None:
        self.trigger_pos = self.pending_len

    def end(self) -> None:
        if self.pending_len:
            data = np.concatenate(self.pending)
            self.out.write(self._format_end(data))
        self.pending = []
        self.pending_len = 0

    # -- helpers ------------------------------------------------------------

    def _header(self) -> bytes:
        assert self.meta is not None, "begin() not called"
        s = f"{PACKAGE_VERSION}\n"
        s += f"Acquisition with {len(self.meta.channels)}/{DEVICE_CHANNELS} channels"
        if self.meta.samplerate:
            s += f" at {samplerate_string(self.meta.samplerate)}"
        return (s + "\n").encode()

    def _format_lines(self, full: np.ndarray) -> bytes:
        raise NotImplementedError

    def _marker_offset(self, trig: int) -> int:
        raise NotImplementedError

    def _after_flush(self, full: np.ndarray) -> None:
        pass

    def _format_end(self, data: np.ndarray) -> bytes:
        raise NotImplementedError
