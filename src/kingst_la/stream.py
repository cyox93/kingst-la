"""Streaming-mode sample layout: uncompressed, channel-interleaved 16-sample words.

Ported from libsigrok kingst-la2016 stream_data() (GPL-3.0-or-later): each
enabled channel, in index order, contributes one u16 LE word holding 16
consecutive samples of that channel (LSB first). After one word per enabled
channel the next 16 samples follow.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence

import numpy as np

from kingst_la.pipeline import Sink


class Deinterleaver:
    def __init__(self, channels: Sequence[int]):
        self.channels = sorted(channels)
        if not self.channels:
            raise ValueError("need at least one enabled channel")
        self.group = 2 * len(self.channels)   # bytes per 16 samples
        self._rest = b""
        # Spread the unpacked bits of channel j to bit position channels[j].
        self._place = np.zeros(16, np.int64)
        self._place[: len(self.channels)] = self.channels

    def feed(self, raw: bytes) -> np.ndarray:
        data = self._rest + raw
        n = len(data) // self.group * self.group
        self._rest = data[n:]
        if not n:
            return np.zeros(0, np.uint16)
        k = len(self.channels)
        words = np.frombuffer(data[:n], "<u2").reshape(-1, k)
        bits = np.unpackbits(words.view(np.uint8).reshape(-1, k, 2), axis=2, bitorder="little")  # (G, k, 16)
        if k <= 4:
            # Few channels: shift each channel's bits into place directly.
            out = bits[:, 0, :].astype(np.uint16) << self.channels[0]
            for j in range(1, k):
                out |= bits[:, j, :].astype(np.uint16) << self.channels[j]
            return out.reshape(-1)
        full = np.zeros((bits.shape[0], 16, 16), np.uint8)       # (G, sample, channel bit)
        full[:, :, self.channels] = bits.transpose(0, 2, 1)
        return np.packbits(full, axis=2, bitorder="little").view("<u2").reshape(-1)

    @property
    def leftover(self) -> int:
        return len(self._rest)


@dataclass
class StreamStats:
    samples: int = 0
    leftover_bytes: int = 0
    truncated: bool = False


def expand(raw_chunks: Iterable[bytes], sink: Sink, channels: Sequence[int],
           limit_samples: Optional[int] = None) -> StreamStats:
    """Feed deinterleaved samples to `sink` (feed only), stopping at `limit_samples`."""
    d = Deinterleaver(channels)
    st = StreamStats()
    for raw in raw_chunks:
        samples = d.feed(raw)
        if limit_samples is not None and st.samples + len(samples) >= limit_samples:
            samples = samples[: limit_samples - st.samples]
            st.truncated = True
        if len(samples):
            sink.feed(samples)
            st.samples += len(samples)
        if st.truncated:
            break
    st.leftover_bytes = d.leftover
    return st
