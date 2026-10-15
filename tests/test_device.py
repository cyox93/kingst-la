import struct

import pytest

from la5016 import device as d

from fake import FakeTransport, transfers


def test_decode_magic_primary_backup_and_partial():
    assert d.decode_magic(bytes.fromhex("05fa10ef05fa10ef")) == (0x05, 0x10)
    assert d.decode_magic(bytes.fromhex("0000000c0cf310ef")) == (0x0C, 0x10)
    assert d.decode_magic(bytes.fromhex("05fa00000000ffff")) == (0x05, 0)
    assert d.decode_magic(bytes(8)) == (0, 0)


@pytest.mark.parametrize("magic, magic2, stem", [
    (0x05, 0x10, "la5016a1"),   # observed on a 2021-05 unit
    (0x05, 0x00, "la5016a1"),
    (0x0C, 0x10, "la5016a2"),
    (0x0C, 0x33, "la5016a2"),  # (0x0c, 0x00) row ignores magic2
    (0x0C, 0x00, "la5016a2"),
    (0x41, 0x00, "la5016a1"),
    (0x02, 0x00, None),
])
def test_lookup_model(magic, magic2, stem):
    assert d.lookup_model(magic, magic2) == stem


def test_threshold_registers_match_sigrok_formula():
    # 1.4 V: R79 25 % duty (0xf2), R56 = (uint16_t)(302 * 1.4 + 121) = 543
    assert d.threshold_registers(1.4) == struct.pack("<HH", 543, 0xF2)
    assert d.threshold_registers(4.0) == struct.pack("<HH", 845, 0)
    assert d.threshold_registers(0.4) == struct.pack("<HH", 241, 0xF2)
    assert d.threshold_registers(-4.0) == struct.pack("<HH", 10, 0x2D7)  # clamped


def test_trigger_registers():
    regs = d.trigger_registers(0x000F, {0: "r", 1: "1", 2: "0"})
    channels, enabled, level, high = struct.unpack("<IIII", regs)
    assert (channels, enabled, level, high) == (0x0F, 0b111, 0b110, 0b010)
    _, _, level, high = struct.unpack("<IIII", d.trigger_registers(1, {0: "f"}))
    assert (level, high) == (0, 1)
    with pytest.raises(d.DeviceError):
        d.trigger_registers(3, {0: "r", 1: "f"})


def test_sample_registers():
    regs = d.sample_registers(100_000_000, 1_000_000, 50, False)
    assert len(regs) == 16
    assert int.from_bytes(regs[0:5], "little") == 1_000_000
    assert int.from_bytes(regs[5:10], "little") == 0
    assert int.from_bytes(regs[10:13], "little") == 1        # 0x100 >> 8
    assert struct.unpack_from("<HB", regs, 13) == (8, 0)
    regs = d.sample_registers(500_000_000, None, 25, True)
    assert int.from_bytes(regs[0:5], "little") == d.NUM_SAMPLES_MAX
    assert int.from_bytes(regs[5:10], "little") == d.NUM_SAMPLES_MAX // 4
    assert int.from_bytes(regs[10:13], "little") == (2 * 1024 ** 3 // 8 * 25 // 100) >> 8
    assert struct.unpack_from("<H", regs, 13)[0] == 1
    with pytest.raises(d.DeviceError):
        d.sample_registers(600_000_000, None, 50, False)
    with pytest.raises(d.DeviceError):
        d.sample_registers(12_207, None, 50, False)   # divider would not fit 16 bits


def test_init_reuses_loaded_fpga():
    t = FakeTransport(fpga_loaded=True)
    dev = d.Device(t)
    dev.init_hardware(load_bitstream=lambda name: pytest.fail("no upload expected"))
    assert dev.identity.stem == "la5016a1"
    assert dev.identity.manufactured == "2021-05"
    assert ("out", d.CMD_FPGA_ENABLE, 1, 0, b"") in t.log
    assert t.log[-1] == ("out", d.CMD_BULK_RESET, 0, 0, b"")


def test_init_uploads_bitstream_padded():
    t = FakeTransport(fpga_loaded=False)
    names = []
    dev = d.Device(t)
    dev.init_hardware(load_bitstream=lambda name: names.append(name) or b"\xab" * 5000)
    assert names == ["kingst-la5016a1-fpga.bitstream"]
    assert ("out", d.CMD_FPGA_INIT, 0, 0, struct.pack("<I", 5000)) in t.log
    assert len(t.bitstream) == 8192 and t.bitstream[5000:] == bytes(3192)


def test_unknown_model():
    t = FakeTransport(magic=bytes.fromhex("02fd00ff02fd00ff"))
    with pytest.raises(d.DeviceError, match="unsupported model"):
        d.Device(t).identify()


def test_capture_sequence_and_download():
    data = transfers([(1, 10), (0, 10), (1, 10), (0, 10), (1, 10)] * 2)
    t = FakeTransport(capture=data, before_trigger=3)
    dev = d.Device(t)
    cfg = d.CaptureConfig(samplerate=100_000_000, limit_samples=100, channel_mask=1, triggers={0: "r"})
    cap = dev.capture(cfg)
    assert cap.samplerate == 100_000_000
    assert cap.trigger_packets == 3
    assert cap.nbytes == len(data)
    assert b"".join(cap.chunks) == data
    order = [e[2] for e in t.log if e[0] == "out" and e[1] == d.CMD_FPGA_SPI]
    assert order[:5] == [d.REG_THRESHOLD, d.REG_CAPT_MODE, d.REG_TRIGGER, d.REG_SAMPLING, d.REG_RUN]
    read_pos, nbytes = struct.unpack("<II", t.writes(d.REG_BULK)[0])
    assert nbytes == len(data) and read_pos == 0x1000 - len(data)
    trig = [e for e in t.log if e[0] == "out" and e[2] == d.REG_TRIGGER][0]
    assert trig[3] == 16


def test_read_pos_wraps():
    t = FakeTransport(capture=transfers([(0, 1)] * 5 * 300))  # 4800 bytes > write_pos 0x1000
    dev = d.Device(t)
    n = dev.start_download(dev.capture_info())
    read_pos, _ = struct.unpack("<II", t.writes(d.REG_BULK)[0])
    assert read_pos == (0x1000 - n) & 0xFFFFFFFF


def test_capture_without_trigger_has_no_trigger_packets():
    t = FakeTransport(capture=transfers([(0, 1)] * 5), before_trigger=7)
    cap = d.Device(t).capture(d.CaptureConfig(samplerate=1_000_000, limit_samples=5))
    assert cap.trigger_packets is None


def test_stop_halts_then_downloads():
    t = FakeTransport(capture=transfers([(0, 1)] * 5), polls_until_idle=10 ** 9)
    dev = d.Device(t)
    calls = []

    def stop():
        calls.append(1)
        if len(calls) == 2:
            t.running = False     # hardware goes idle after the halt
        return True

    cap = dev.capture(d.CaptureConfig(samplerate=1_000_000), should_stop=stop)
    assert t.writes(d.REG_RUN) == [bytes([d.RUNMODE_RUN]), bytes([d.RUNMODE_HALT])]
    assert cap.nbytes == 16


def test_close_halts_and_keeps_fpga():
    t = FakeTransport()
    d.Device(t).close()
    assert t.writes(d.REG_RUN) == [bytes([d.RUNMODE_HALT])]
    assert not [e for e in t.log if e[1] == d.CMD_FPGA_ENABLE]
    assert t.closed
