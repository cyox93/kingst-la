"""Hardware tests: need a connected LA5016 and extracted firmware.

Run with `pytest -m hw`. Feed a known periodic signal into CH0 and set
KINGST_LA_TEST_FREQ (Hz) and, if its amplitude is small, KINGST_LA_TEST_THRESHOLD (V).
"""

import os

import numpy as np
import pytest

from kingst_la import rle
from kingst_la.device import CaptureConfig, open_device

pytestmark = pytest.mark.hw

FREQ = float(os.environ.get("KINGST_LA_TEST_FREQ", "0"))
THRESHOLD = float(os.environ.get("KINGST_LA_TEST_THRESHOLD", "1.4"))


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


@pytest.mark.skipif(not FREQ, reason="set KINGST_LA_TEST_FREQ to the CH0 signal frequency")
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


@pytest.mark.parametrize("rate, mask", [(10_000_000, 0xF), (100_000_000, 0x1)])  # 40 and 100 Mbps; 200 Mbps breaks off now and then
def test_stream_runs_in_real_time(dev, rate, mask):
    import time
    from kingst_la import stream as lstream
    st = dev.stream(CaptureConfig(samplerate=rate, channel_mask=mask, threshold=THRESHOLD))
    sink = Collect()
    t = time.time()
    try:
        stats = lstream.expand(st, sink, st.channels, limit_samples=rate)   # one second
    finally:
        st.stop()
    dt = time.time() - t
    assert stats.samples == rate
    assert 0.9 < dt < 1.5     # data arrives at the sampling rate, not faster


@pytest.mark.parametrize("requested, actual", [(40_000_000, 50_000_000), (400_000_000, 200_000_000)])
def test_divider_rounding_matches_hardware(dev, requested, actual):
    import time
    n = actual // 2
    t = time.time()
    cap = dev.capture(CaptureConfig(samplerate=requested, limit_samples=n))
    dt = time.time() - t
    for _ in cap.chunks:
        pass
    assert cap.samplerate == actual
    assert dt == pytest.approx(0.5, rel=0.25)


# PWM loopback: wire a PWM output to CH0 and set KINGST_LA_TEST_PWM to 1 or 2.
PWM = int(os.environ.get("KINGST_LA_TEST_PWM", "0"))
needs_pwm = pytest.mark.skipif(PWM not in (1, 2), reason="wire PWM1 or PWM2 to CH0, set KINGST_LA_TEST_PWM")


@needs_pwm
@pytest.mark.parametrize("freq, duty, rate", [(1_000, 25, 1_000_000), (123_400, 10, 100_000_000),
                                              (1_000_000, 50, 100_000_000), (20_000_000, 50, 500_000_000)])
def test_pwm_loopback(dev, freq, duty, rate):
    actual_f, actual_d = dev.set_pwm(PWM - 1, True, freq, duty)
    try:
        _, _, _, x = _capture(dev, samplerate=rate, limit_samples=200_000, channel_mask=1)
    finally:
        dev.set_pwm(PWM - 1, False)
    ch0 = (x & 1).astype(np.int8)
    rises = np.flatnonzero(np.diff(ch0) == 1)
    period = np.diff(rises).mean()
    assert rate / period == pytest.approx(actual_f, rel=1e-3)
    high = ch0[rises[0] + 1:rises[-1] + 1].mean()
    assert high == pytest.approx(actual_d / 100, abs=max(0.01, 1.5 / period))   # one-sample resolution


@needs_pwm
def test_pwm_streaming_loopback(dev):
    from kingst_la import stream as lstream
    dev.set_pwm(PWM - 1, True, 1_000, 50)
    try:
        st = dev.stream(CaptureConfig(samplerate=10_000_000, channel_mask=0xF, threshold=THRESHOLD))
        sink = Collect()
        try:
            lstream.expand(st, sink, st.channels, limit_samples=10_000_000)
        finally:
            st.stop()
    finally:
        dev.set_pwm(PWM - 1, False)
    ch0 = (np.concatenate(sink.parts) & 1).astype(np.int8)
    periods = np.diff(np.flatnonzero(np.diff(ch0) == 1))
    assert len(periods) >= 998 and set(periods.tolist()) == {10_000}   # no lost or repeated data


@needs_pwm
@pytest.mark.parametrize("rate", [10_000_000, 100_000_000])
def test_pwm_one_channel_stream_starts_in_phase(dev, rate):
    # A one-channel stream may begin with two words from before a time gap; they must be dropped.
    from kingst_la import stream as lstream
    dev.set_pwm(PWM - 1, True, rate // 10, 30)
    try:
        for _ in range(10):
            st = dev.stream(CaptureConfig(samplerate=rate, channel_mask=1, threshold=THRESHOLD))
            sink = Collect()
            try:
                lstream.expand(st, sink, st.channels, limit_samples=4096)
            finally:
                st.stop()
            ch0 = np.concatenate(sink.parts) & 1
            period = ch0[2000:2010]
            assert (ch0 == np.resize(np.roll(period, 2000 % 10), len(ch0))).all()
    finally:
        dev.set_pwm(PWM - 1, False)
