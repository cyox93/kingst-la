import struct

import pytest

from kingst_la import device as d

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


def test_stream_setup_order_and_read_size():
    t = FakeTransport()
    dev = d.Device(t)
    st = dev.stream(d.CaptureConfig(samplerate=10_000_000, channel_mask=0b1011, threshold=1.4))
    assert st.samplerate == 10_000_000 and st.channels == [0, 1, 3]
    assert t.writes(d.REG_CAPT_MODE) == [bytes([d.CAPTMODE_STREAM])]
    _, enabled, level, high = struct.unpack("<IIII", t.writes(d.REG_TRIGGER)[0])
    assert (enabled, level, high) == (0, 0, 0)
    kinds = [(e[0], e[1], e[2]) for e in t.log if e[0] in ("out", "bulk_reader")]
    i_reset = kinds.index(("out", d.CMD_BULK_RESET, 0))
    i_reader = [k[0] for k in kinds].index("bulk_reader")
    i_run = kinds.index(("out", d.CMD_FPGA_SPI, d.REG_RUN))
    i_start = kinds.index(("out", d.CMD_BULK_START, 0))
    assert i_reset < i_reader < i_run < i_start
    reader = [e for e in t.log if e[0] == "bulk_reader"][0]
    # 3 ch x 10 MHz = 3.75 MB/s -> about 50 ms per read, a multiple of 512
    assert reader[2] % 512 == 0 and 150_000 < reader[2] < 200_000
    st.stop()
    assert t.writes(d.REG_RUN)[-1] == bytes([d.RUNMODE_HALT])


@pytest.mark.parametrize("mask, chunks, expected", [
    (0b1, [b"\x01", b"\x02\x03", b"\x04\x05\x06"], [b"\x05\x06"]),   # first 4 bytes dropped
    (0b1, [b"\x01\x02\x03\x04", b"\x05\x06"], [b"\x05\x06"]),
    (0b11, [b"\x01\x02", b"\x03\x04"], [b"\x01\x02", b"\x03\x04"]),   # kept with 2+ channels
])
def test_stream_drops_stale_words_of_one_channel(mask, chunks, expected):
    t = FakeTransport()
    t.stream_data = chunks
    st = d.Device(t).stream(d.CaptureConfig(samplerate=1_000_000, channel_mask=mask))
    assert list(st) == expected
    st.stop()


def test_stream_rejects_trigger_and_fast_rates():
    with pytest.raises(d.DeviceError):
        d.Device(FakeTransport()).stream(d.CaptureConfig(samplerate=200_000_000, channel_mask=1))
    with pytest.raises(d.DeviceError):
        d.Device(FakeTransport()).stream(d.CaptureConfig(samplerate=1_000_000, triggers={0: "r"}))


def test_pwm_registers_follow_sigrok_rounding():
    assert d.pwm_registers(1000, 50) == (200_000, 100_000)
    assert d.pwm_registers(3_000_000, 33.3) == (66, 22)        # int(66.67), round(21.98)
    assert d.pwm_actual(66, 22) == pytest.approx((3_030_303.03, 33.333), rel=1e-4)
    for bad in ((0, 50), (20_000_001, 50), (1000, 0), (1000, 100.1)):
        with pytest.raises(d.DeviceError):
            d.pwm_registers(*bad)


def test_set_pwm_sequence_and_state_keeps_other_output():
    t = FakeTransport()
    dev = d.Device(t)
    assert dev.set_pwm(1, True, 100_000, 50) == (100_000, 50)
    t.log.clear()
    assert dev.set_pwm(0, True, 1000, 25) == (1000, 25)
    en = t.writes(d.REG_PWM_EN)
    assert en == [b"\x02", b"\x03"]                     # PWM1 off (PWM2 stays on), then both on
    assert t.writes(d.REG_PWM1) == [struct.pack("<II", 200_000, 50_000)]
    t.log.clear()
    assert dev.set_pwm(1, False) is None
    assert t.writes(d.REG_PWM_EN) == [b"\x01"] and not t.writes(d.REG_PWM2)
    assert dev.pwm_state() == [(True, 1000, 25), (False, 100_000, 50)]


def test_set_pwm_reuses_last_frequency():
    t = FakeTransport()
    dev = d.Device(t)
    dev.set_pwm(0, True, 2000, 50)
    assert dev.set_pwm(0, True, duty=10) == (2000, 10)


def test_bitstream_upload_resets_pwm_state():
    t = FakeTransport(fpga_loaded=False)
    dev = d.Device(t)
    dev.set_pwm(0, True, 5000, 50)
    dev.init_hardware(load_bitstream=lambda name: b"\x00" * 10)
    assert dev.pwm_state()[0][0] is False
