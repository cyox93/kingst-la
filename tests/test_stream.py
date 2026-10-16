import random
import struct

import numpy as np
import pytest

from kingst_la import stream


def interleave(samples, channels):
    """Reference encoder following stream_data(): per 16 samples, one word per enabled channel."""
    out = b""
    for g in range(0, len(samples), 16):
        block = samples[g:g + 16]
        for ch in sorted(channels):
            word = 0
            for i, s in enumerate(block):
                word |= ((int(s) >> ch) & 1) << i
            out += struct.pack("<H", word)
    return out


def mask(samples, channels):
    m = sum(1 << c for c in channels)
    return np.array([s & m for s in samples], np.uint16)


class Rec:
    def __init__(self):
        self.parts = []

    def feed(self, a):
        self.parts.append(a)

    def all(self):
        return np.concatenate(self.parts) if self.parts else np.zeros(0, np.uint16)


@pytest.mark.parametrize("channels", [[0], [3], [0, 2, 9], list(range(16)), [15, 1]])
@pytest.mark.parametrize("split", [1, 3, 7, 1000])
def test_roundtrip(channels, split):
    rng = random.Random(len(channels) * 100 + split)
    samples = [rng.randrange(1 << 16) for _ in range(16 * 9)]
    raw = interleave(samples, channels)
    rec = Rec()
    chunks = [raw[i:i + split] for i in range(0, len(raw), split)]
    st = stream.expand(chunks, rec, channels)
    assert np.array_equal(rec.all(), mask(samples, channels))
    assert st.samples == len(samples) and st.leftover_bytes == 0


def test_single_channel_bit_order():
    # word 0x0003 on CH0: samples 0 and 1 high, the rest low
    d = stream.Deinterleaver([0])
    assert d.feed(b"\x03\x00").tolist() == [1, 1] + [0] * 14


def test_limit_truncates():
    raw = interleave([0xFFFF] * 64, [0, 1])
    rec = Rec()
    st = stream.expand([raw], rec, [0, 1], limit_samples=20)
    assert len(rec.all()) == 20 and st.truncated


def test_leftover_reported():
    rec = Rec()
    st = stream.expand([b"\x01\x00\x02"], rec, [0, 1])
    assert st.samples == 0 and st.leftover_bytes == 3


def test_throughput():
    import time
    raw = np.random.default_rng(1).integers(0, 1 << 16, 8 << 20, dtype=np.uint16).tobytes()  # 16 MiB
    d = stream.Deinterleaver(list(range(16)))
    t = time.perf_counter()
    n = len(d.feed(raw))
    dt = time.perf_counter() - t
    assert n == len(raw) // 2
    assert len(raw) / dt > 25e6, "%.1f MB/s" % (len(raw) / dt / 1e6)   # USB stream maximum
