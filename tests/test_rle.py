from __future__ import annotations

import struct
import time

import numpy as np
import pytest

from kingst_la.rle import ExpandStats, expand


class RecSink:
    """Records feed/trigger events; begin/end must never be called."""

    def __init__(self):
        self.events = []

    def begin(self, meta):
        raise AssertionError("begin called")

    def end(self):
        raise AssertionError("end called")

    def feed(self, samples):
        assert samples.dtype == np.uint16
        assert len(samples) > 0
        self.events.append(("feed", samples.copy()))

    def trigger(self):
        self.events.append(("trigger",))

    def samples(self):
        arrs = [e[1] for e in self.events if e[0] == "feed"]
        return np.concatenate(arrs) if arrs else np.zeros(0, np.uint16)

    def trigger_index(self):
        """Sample index at which trigger occurred, or None."""
        n = 0
        idx = None
        for e in self.events:
            if e[0] == "feed":
                n += len(e[1])
            else:
                assert idx is None, "trigger emitted twice"
                idx = n
        return idx


def transfer(packets, seq=0):
    """Build one 16-byte transfer from up to 5 (pins, reps) packets (padded with reps=0)."""
    packets = list(packets) + [(0, 0)] * (5 - len(packets))
    assert len(packets) == 5
    b = b"".join(struct.pack("<HB", p, r) for p, r in packets)
    return b + bytes([seq & 0xFF])


def transfers(packet_list, seq0=0):
    out = b""
    for i in range(0, len(packet_list), 5):
        out += transfer(packet_list[i:i + 5], seq0 + i // 5)
    return out


def ref_expand(packet_list):
    return np.array([p for p, r in packet_list for _ in range(r)], dtype=np.uint16)


PKTS = [(0x1234, 3), (0xFFFF, 0), (0x0001, 255), (0xABCD, 1), (0x8000, 2),
        (0x0002, 4), (0x0003, 0), (0x0004, 7), (0x0005, 1), (0x0006, 2)]


def run(raw, **kw):
    sink = RecSink()
    chunks = raw if isinstance(raw, list) else [raw]
    stats = expand(iter(chunks), sink, **kw)
    return sink, stats


def test_basic_expand_and_stats():
    raw = transfers(PKTS)
    sink, st = run(raw)
    assert np.array_equal(sink.samples(), ref_expand(PKTS))
    assert isinstance(st, ExpandStats)
    assert st.samples == 3 + 255 + 1 + 2 + 4 + 7 + 1 + 2
    assert st.packets == 10
    assert st.transfers == 2
    assert st.seq_breaks == 0
    assert st.leftover_bytes == 0
    assert st.truncated is False
    assert st.trigger_sample is None
    assert sink.trigger_index() is None


def test_little_endian_pins():
    sink, _ = run(bytes([0x34, 0x12, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]))
    assert sink.samples().tolist() == [0x1234]


def test_all_zero_repeats_feeds_nothing():
    sink, st = run(transfers([(1, 0)] * 5))
    assert sink.events == []
    assert st.samples == 0 and st.packets == 5


def test_empty_input():
    sink, st = run([])
    assert sink.events == [] and st.samples == 0 and st.transfers == 0


@pytest.mark.parametrize("off", range(1, 16))
def test_split_at_every_offset(off):
    raw = transfers(PKTS)
    ref = ref_expand(PKTS)
    # split inside the first and second transfer, plus an empty chunk
    chunks = [raw[:off], b"", raw[off:16 + off], raw[16 + off:]]
    sink, st = run(chunks)
    assert np.array_equal(sink.samples(), ref)
    assert st.leftover_bytes == 0
    # byte-by-byte
    sink, _ = run([raw[i:i + 1] for i in range(len(raw))])
    assert np.array_equal(sink.samples(), ref)


def test_max_chunk_splitting():
    pk = [(i, 255) for i in range(23)]
    raw = transfers(pk)
    sink, st = run(raw, max_chunk=7)
    assert np.array_equal(sink.samples(), ref_expand(pk))
    assert all(len(e[1]) <= 7 for e in sink.events)
    assert st.samples == 23 * 255


def test_max_chunk_large_default_bounded():
    pk = [(7, 255)] * 5000
    sink, _ = run(transfers(pk), max_chunk=1000)
    assert all(len(e[1]) <= 1000 for e in sink.events)
    assert np.array_equal(sink.samples(), ref_expand(pk))


def trigger_pos(k):
    """Expected trigger sample index after k packets of PKTS."""
    return sum(r for _, r in PKTS[:k])


@pytest.mark.parametrize("k", [0, 1, 2, 3, 4, 5, 6, 7, 10])
@pytest.mark.parametrize("max_chunk", [1 << 22, 7, 1])
def test_trigger_positions(k, max_chunk):
    sink, st = run(transfers(PKTS), trigger_packets=k, max_chunk=max_chunk)
    assert np.array_equal(sink.samples(), ref_expand(PKTS))
    assert sink.trigger_index() == trigger_pos(k)
    assert st.trigger_sample == trigger_pos(k)


def test_trigger_at_zero_is_first_event():
    sink, _ = run(transfers(PKTS), trigger_packets=0)
    assert sink.events[0] == ("trigger",)


def test_trigger_mid_transfer_splits_feed():
    sink, _ = run(transfers(PKTS), trigger_packets=3)  # inside first transfer
    kinds = [e[0] for e in sink.events]
    assert kinds.count("trigger") == 1
    assert sink.trigger_index() == 3 + 255


def test_trigger_beyond_data_not_emitted():
    sink, st = run(transfers(PKTS), trigger_packets=11)
    assert sink.trigger_index() is None
    assert st.trigger_sample is None


def test_trigger_zero_without_data_not_emitted():
    sink, st = run([], trigger_packets=0)
    assert sink.events == [] and st.trigger_sample is None


def test_limit_truncates_mid_packet():
    sink, st = run([transfers(PKTS)], limit_samples=100)
    assert np.array_equal(sink.samples(), ref_expand(PKTS)[:100])
    assert st.samples == 100 and st.truncated is True


def test_limit_stops_consuming_input():
    raw = transfers(PKTS)

    def gen():
        yield raw
        raise AssertionError("iterator consumed past limit")

    sink = RecSink()
    st = expand(gen(), sink, limit_samples=10)
    assert st.samples == 10 and st.truncated


def test_limit_larger_than_data_not_truncated():
    _, st = run(transfers(PKTS), limit_samples=10_000)
    assert st.truncated is False and st.samples == trigger_pos(10)


def test_limit_with_trigger():
    # trigger at sample 258 (< limit) is emitted
    sink, st = run(transfers(PKTS), trigger_packets=3, limit_samples=270)
    assert sink.trigger_index() == 258 and st.samples == 270
    # trigger beyond limit is not emitted
    sink, st = run(transfers(PKTS), trigger_packets=5, limit_samples=200)
    assert sink.trigger_index() is None and st.trigger_sample is None
    # trigger exactly at the limit is not emitted (no samples would follow it)
    sink, st = run(transfers(PKTS), trigger_packets=3, limit_samples=258)
    assert sink.trigger_index() is None and st.samples == 258 and st.truncated


def test_seq_breaks():
    raw = transfer([(1, 1)], 254) + transfer([(1, 1)], 255) + transfer([(1, 1)], 0)
    _, st = run(raw)
    assert st.seq_breaks == 0
    raw = transfer([(1, 1)], 5) + transfer([(1, 1)], 7) + transfer([(1, 1)], 8) + transfer([(1, 1)], 3)
    _, st = run(raw)
    assert st.seq_breaks == 2 and st.transfers == 4
    # break across a chunk boundary
    _, st = run([transfer([(1, 1)], 1), transfer([(1, 1)], 9)])
    assert st.seq_breaks == 1


def test_leftover_bytes():
    raw = transfers(PKTS) + b"\x01\x02\x03"
    sink, st = run([raw[:20], raw[20:]])
    assert st.leftover_bytes == 3
    assert np.array_equal(sink.samples(), ref_expand(PKTS))


def test_performance_1mib():
    rng = np.random.default_rng(1)
    raw = rng.integers(0, 256, size=1 << 20, dtype=np.uint8).tobytes()
    chunks = [raw[i:i + 65536 + 3] for i in range(0, len(raw), 65536 + 3)]
    sink = RecSink()
    sink.feed = lambda s: None  # avoid copying cost
    t = time.perf_counter()
    st = expand(iter(chunks), sink)
    assert time.perf_counter() - t < 0.5
    assert st.transfers == (1 << 20) // 16
