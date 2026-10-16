"""Real sigrok decoders on synthetic signals. Needs `la5016 --install-decoders`.

Run with `pytest -m decoders`.
"""

import io

import numpy as np
import pytest

from la5016.pipeline import Channel, Meta
from la5016.srd import annotate, loader, session

pytestmark = [
    pytest.mark.decoders,
    pytest.mark.skipif(not loader.available(), reason="run 'la5016 --install-decoders' first"),
]

CHANNELS = [Channel(i, "CH%d" % i) for i in range(16)]


def decode(pd, samples, rate, ann=None, chunk=4093, level=2):
    out = io.StringIO()
    printer = annotate.AnnotationPrinter(out, {}, loglevel=level)
    stacks = loader.build_stacks([pd], CHANNELS, printer)
    infos = {}
    todo = list(stacks)
    while todo:
        i = todo.pop()
        infos[i.info.id] = i.info
        todo.extend(i.next)
    printer.visible = annotate.parse_annotations(ann, infos)
    sink = session.DecoderSink(stacks)
    sink.begin(Meta(rate, CHANNELS))
    for i in range(0, len(samples), chunk):
        sink.feed(samples[i:i + chunk])
    sink.end()
    return out.getvalue().splitlines()


def uart_signal(data, baud, rate, bit=0):
    spb = rate // baud
    bits = [1] * 30
    for b in data:
        bits += [0] + [(b >> i) & 1 for i in range(8)] + [1, 1]
    bits += [1] * 30
    return (np.repeat(np.array(bits, np.uint16), spb) << bit).astype(np.uint16)


def i2c_signal(addr, payload, half=10):
    """Bit-banged I2C write on SCL=CH0, SDA=CH1, ACKs driven low."""
    seq = []

    def put(scl, sda, n=half):
        seq.extend([(scl | (sda << 1))] * n)

    put(1, 1, 3 * half)
    put(1, 0)            # start: SDA falls while SCL high
    put(0, 0)
    for byte in [addr << 1] + list(payload):
        for i in range(7, -1, -1):
            b = (byte >> i) & 1
            put(0, b)
            put(1, b)
            put(0, b)
        put(0, 0)        # ACK
        put(1, 0)
        put(0, 0)
    put(1, 0)
    put(1, 1, 3 * half)  # stop: SDA rises while SCL high
    return np.array(seq, np.uint16)


def spi_signal(mosi_bytes, half=5):
    """Mode 0 SPI on CLK=CH0, MOSI=CH1, CS#=CH2 (active low)."""
    seq = [0b100] * (4 * half)
    for byte in mosi_bytes:
        for i in range(7, -1, -1):
            b = (byte >> i) & 1
            seq += [(b << 1)] * half          # clock low, data set up, CS low
            seq += [1 | (b << 1)] * half      # rising edge samples
    seq += [0] * half + [0b100] * (4 * half)
    return np.array(seq, np.uint16)


@pytest.mark.parametrize("chunk", [1000, 4093, 10 ** 7])
def test_uart(chunk):
    x = uart_signal(b"Hello", 115200, 11_520_000)
    assert decode("uart:rx=CH0:baudrate=115200", x, 11_520_000, "uart=rx-data", chunk) == [
        "uart-1: 48", "uart-1: 65", "uart-1: 6C", "uart-1: 6C", "uart-1: 6F"]


def test_uart_on_renamed_channel_with_options():
    x = uart_signal(b"\x01\x80", 9600, 960_000, bit=5)
    chans = [Channel(5, "RX")]
    out = io.StringIO()
    printer = annotate.AnnotationPrinter(out, {}, loglevel=2)
    stacks = loader.build_stacks(["uart:rx=RX:baudrate=9600:format=bin"], chans, printer)
    printer.visible = annotate.parse_annotations("uart=rx-data", {"uart": stacks[0].info})
    sink = session.DecoderSink(stacks)
    sink.begin(Meta(960_000, chans))
    sink.feed(x)
    sink.end()
    assert out.getvalue().splitlines() == ["uart-1: 00000001", "uart-1: 10000000"]


def test_i2c():
    lines = decode("i2c:scl=CH0:sda=CH1", i2c_signal(0x50, b"\x12\xab"), 1_000_000)
    assert "i2c-1: Start" in lines
    assert "i2c-1: Address write: 50" in lines
    assert "i2c-1: Data write: 12" in lines
    assert "i2c-1: Data write: AB" in lines
    assert "i2c-1: Stop" in lines


def test_spi():
    lines = decode("spi:clk=CH0:mosi=CH1:cs=CH2", spi_signal(b"\x5a\xc3"), 1_000_000, "spi=mosi-data")
    assert lines == ["spi-1: 5A", "spi-1: C3"]


def test_timing_period():
    x = np.tile(np.repeat(np.array([0, 1], np.uint16), 500), 5)   # 1 kHz at 1 MSa/s
    lines = decode("timing:data=CH0:edge=rising", x, 1_000_000, "timing=time")
    assert lines and all(l == "timing-1: 1.000 ms (1.000 kHz)" for l in lines)


def test_stacked_eeprom24xx_over_i2c():
    lines = decode("i2c:scl=CH0:sda=CH1,eeprom24xx", i2c_signal(0x50, b"\x00\x41"), 1_000_000)
    assert any(l.startswith("eeprom24xx-1: ") for l in lines)
    assert any(l.startswith("i2c-1: ") for l in lines)   # sigrok-cli also prints lower levels


def test_uart_speed():
    import time
    x = uart_signal(bytes(range(256)) * 4, 115200, 11_520_000)    # ~11 M samples
    t = time.perf_counter()
    lines = decode("uart:rx=CH0:baudrate=115200", x, 11_520_000, "uart=rx-data", 1 << 22)
    assert len(lines) == 1024
    assert time.perf_counter() - t < 10


def meta(pd, samples, rate, ids):
    out = io.StringIO()
    printer = annotate.MetaPrinter(out, set(ids), samplenum=True)
    sink = session.DecoderSink(loader.build_stacks([pd], CHANNELS, printer))
    sink.begin(Meta(rate, CHANNELS))
    for i in range(0, len(samples), 4093):
        sink.feed(samples[i:i + 4093])
    sink.end()
    return out.getvalue().splitlines()


def test_meta_bitrate():
    # spi: 8 bits over samples 25..95 at 1 MHz -> int(8 / 71 us)
    assert meta("spi:clk=CH0:mosi=CH1:cs=CH2", spi_signal(b"\x5a\xc3"), 1_000_000, ["spi"]) == [
        "25-95 spi-1: Bitrate: 112676", "105-175 spi-1: Bitrate: 112676"]
    lines = meta("i2c:scl=CH0:sda=CH1", i2c_signal(0x50, b"\x12\xab"), 1_000_000, ["i2c"])
    assert len(lines) == 1 and lines[0].startswith("30-870 i2c-1: Bitrate: ")
