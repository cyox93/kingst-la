"""Hardware tests: need a connected LA5016 and extracted firmware.

Run with `pytest -m hw`. Feed a known periodic signal into CH0 and set
LA5016_TEST_FREQ (Hz) and, if its amplitude is small, LA5016_TEST_THRESHOLD (V).
"""

import os

import numpy as np
import pytest

from la5016 import rle
from la5016.device import CaptureConfig, open_device

pytestmark = pytest.mark.hw

FREQ = float(os.environ.get("LA5016_TEST_FREQ", "0"))
THRESHOLD = float(os.environ.get("LA5016_TEST_THRESHOLD", "1.4"))


class Collect:
    def __init__(self):
        self.parts = []
        self.trigger_at = None

    def feed(self, a):
        self.parts.append(a)

    def trigger(self):
        self.trigger_at = sum(len(p) for p in self.parts)


@pytest.fixture(scope="module")
def dev():
    d = open_device()
    yield d
    d.close()


def _capture(dev, **kw):
    cfg = CaptureConfig(threshold=THRESHOLD, **kw)
    cap = dev.capture(cfg)
    sink = Collect()
    stats = rle.expand(cap.chunks, sink, trigger_packets=cap.trigger_packets, limit_samples=cfg.limit_samples)
    return cap, stats, sink, np.concatenate(sink.parts)


def test_identify(dev):
    assert dev.identity.stem in ("la5016a1", "la5016a2")


@pytest.mark.parametrize("rate", [1_000_000, 200_000_000, 500_000_000])
def test_capture_is_consistent(dev, rate):
    cap, stats, _, x = _capture(dev, samplerate=rate, limit_samples=2_000_000)
    assert stats.samples == len(x) == 2_000_000
    assert stats.seq_breaks == 0 and stats.leftover_bytes == 0


@pytest.mark.skipif(not FREQ, reason="set LA5016_TEST_FREQ to the CH0 signal frequency")
def test_ch0_frequency(dev):
    rate = 1_000_000
    n = int(max(20, 50 * rate / FREQ)) if FREQ < rate / 20 else 1_000_000
    _, _, _, x = _capture(dev, samplerate=rate, limit_samples=n)
    ch0 = (x & 1).astype(float)
    spec = np.abs(np.fft.rfft(ch0 - ch0.mean()))
    peak = (np.argmax(spec[1:]) + 1) * rate / len(ch0)
    assert peak == pytest.approx(FREQ, rel=0.01)


@pytest.mark.skipif(not FREQ, reason="needs a periodic CH0 signal")
@pytest.mark.parametrize("cond, before, after", [("r", 0, 1), ("f", 1, 0)])
def test_edge_trigger(dev, cond, before, after):
    _, _, sink, x = _capture(dev, samplerate=1_000_000, limit_samples=200_000,
                             channel_mask=1, triggers={0: cond})
    k = sink.trigger_at
    assert k is not None
    assert (x[k - 1] & 1, x[k] & 1) == (before, after)
