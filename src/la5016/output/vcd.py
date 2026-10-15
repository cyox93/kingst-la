"""Value Change Dump output.

Ported from libsigrok src/output/vcd.c (logic-only "immediate write" path
of receive(), gen_header(), get_timescale_freq(), vcd_identifier()).
Like libsigrok, a timestamp is emitted whenever the raw 16-bit sample
changes, also when only a disabled channel changed (the line then carries
no values), and value changes are preceded by two spaces after "#<ts>".
"""

from __future__ import annotations

from typing import BinaryIO, Mapping, Optional

import numpy as np

from ..pipeline import Meta
from ._common import (DEVICE_CHANNELS, PACKAGE_VERSION, as_samples, assemble,
                      channel_bits, const_block, ctime_now, parse_options,
                      period_string, samplerate_string, uint_block)

# Scope name is PACKAGE_NAME in libsigrok; kept for compatible files.
SCOPE_NAME = "libsigrok"


def vcd_identifier(idx: int) -> str:
    if idx < 94:
        return chr(ord("!") + idx)
    idx -= 94
    letters = "abcdefghijklmnopqrstuvwxyz"
    if idx < 26 * 26:
        return letters[idx // 26 % 26] + letters[idx % 26]
    idx -= 26 * 26
    if idx < 26 ** 3:
        return letters[idx // 676 % 26] + letters[idx // 26 % 26] + letters[idx % 26]
    raise ValueError("too many VCD signals")


def get_timescale_freq(samplerate: int) -> int:
    timescale = 1
    while timescale < samplerate:
        timescale *= 10
    for _ in range(2):
        if timescale // samplerate * samplerate == timescale:
            break
        timescale *= 10
    return timescale


class VcdOutput:
    NAME = "vcd"

    def __init__(self, out: BinaryIO, options: Optional[Mapping[str, object]] = None):
        parse_options(self.NAME, {}, options)
        self.out = out
        self.meta: Optional[Meta] = None
        self.header_done = False
        self.snum = 0
        self.last = 0  # last_logic: raw sample, zero-initialized

    def begin(self, meta: Meta) -> None:
        if meta.samplerate <= 0:
            raise ValueError("VCD output needs a known samplerate")
        self.meta = meta
        self.samplerate = meta.samplerate
        self.period = get_timescale_freq(meta.samplerate)
        self.ids = [vcd_identifier(i).encode() for i in range(len(meta.channels))]

    def _chk_header(self) -> bytes:
        if self.header_done:
            return b""
        self.header_done = True
        assert self.meta is not None, "begin() not called"
        m = self.meta
        lines = [
            f"$date {ctime_now()} $end",
            f"$version {PACKAGE_VERSION} $end",
            "$comment",
            f"  Acquisition with {len(m.channels)}/{DEVICE_CHANNELS} channels"
            f" at {samplerate_string(m.samplerate)}",
            "$end",
            f"$timescale {period_string(1, self.period)} $end",
            f"$scope module {SCOPE_NAME} $end",
        ]
        for ch, ident in zip(m.channels, self.ids):
            lines.append(f"$var wire 1 {ident.decode()} {ch.name} $end")
        lines += ["$upscope $end", "$enddefinitions $end", ""]
        return "\n".join(lines).encode()

    def _timestamps(self, snums: np.ndarray) -> np.ndarray:
        # snum_to_ts() and "%.0f" (round half to even, as np.rint).
        ts = snums.astype(np.float64) / self.samplerate * self.period
        return np.rint(ts).astype(np.uint64)

    def feed(self, samples: np.ndarray) -> None:
        samples = as_samples(samples)
        parts = [self._chk_header()]
        n = samples.size
        if n:
            prevs = np.empty_like(samples)
            prevs[0] = self.last
            prevs[1:] = samples[:-1]
            changed = samples != prevs
            first = self.snum == 0
            if first:
                changed[0] = True
            idx = np.flatnonzero(changed)
            cur = samples[idx]
            diff = cur ^ prevs[idx]
            k = idx.size
            blocks = [const_block(k, b"\n#"), uint_block(self._timestamps(self.snum + idx)),
                      const_block(k, b" ")]
            assert self.meta is not None
            for ch, ident in zip(self.meta.channels, self.ids):
                show = channel_bits(diff, ch.index).astype(bool)
                if first:
                    show[0] = True
                body = np.empty((k, 2 + len(ident)), dtype=np.uint8)
                body[:, 0] = ord(" ")
                body[:, 1] = channel_bits(cur, ch.index) + ord("0")
                body[:, 2:] = np.frombuffer(ident, dtype=np.uint8)
                blocks.append((body, show[:, None]))
            parts.append(assemble(blocks))
            self.last = int(samples[-1])
            self.snum += n
        data = b"".join(parts)
        if data:
            self.out.write(data)

    def trigger(self) -> None:
        pass  # vcd.c has no SR_DF_TRIGGER handling

    def end(self) -> None:
        # Final timestamp as length indicator.
        ts = float(self.snum) / self.samplerate * self.period
        self.out.write(self._chk_header() + b"\n#%.0f\n" % ts)
