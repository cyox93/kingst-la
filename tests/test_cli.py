import pytest

from la5016 import cli
from la5016 import device as d

from fake import FakeTransport, transfers


def _opener(transport):
    def open_device(conn=None):
        dev = d.Device(transport)
        dev.identify()
        return dev
    return open_device


def _capture(packets):
    return FakeTransport(capture=transfers(packets), before_trigger=2)


SQUARE = [(1, 3), (0, 3), (1, 3), (0, 3), (1, 3)]  # CH0 toggling every 3 samples


def test_capture_csv_to_stdout(capsysbinary):
    t = _capture(SQUARE)
    rc = cli.main(["-c", "samplerate=1m", "--samples", "9", "-C", "CH0", "-O", "csv:header=false:label=off"],
                  open_device=_opener(t))
    out = capsysbinary.readouterr().out.decode()
    assert rc == 0
    assert out.split() == ["1", "1", "1", "0", "0", "0", "1", "1", "1"]
    assert t.closed


def test_capture_vcd_file(tmp_path):
    t = _capture(SQUARE)
    path = tmp_path / "cap.vcd"
    rc = cli.main(["--samples", "15", "-C", "CH0=CLK", "-o", str(path)], open_device=_opener(t))
    text = path.read_text()
    assert rc == 0
    assert "$var wire 1 ! CLK $end" in text
    assert "$timescale 1 ns $end" in text   # 500 MHz default samplerate


def test_wait_trigger_drops_pretrigger(capsysbinary):
    t = _capture(SQUARE)   # trigger after packet 2 -> sample 6
    rc = cli.main(["-c", "samplerate=1m", "--samples", "15", "-C", "CH0", "-t", "CH0=r", "-w",
                   "-O", "csv:header=false:label=off"], open_device=_opener(t))
    assert rc == 0
    assert capsysbinary.readouterr().out.decode().split() == ["1"] * 3 + ["0"] * 3 + ["1"] * 3


def test_time_converts_to_samples(capsysbinary):
    t = _capture(SQUARE * 2)
    rc = cli.main(["-c", "samplerate=20k", "--time", "1", "-C", "CH0", "-O", "csv:header=false:label=off"],
                  open_device=_opener(t))
    assert rc == 0
    assert len(capsysbinary.readouterr().out.decode().split()) == 20


@pytest.mark.parametrize("argv", [
    ["--samples", "10", "--time", "1"],
    ["--samples", "10", "-t", "CH0=e"],
    ["--samples", "10", "-w"],
    ["--samples", "10", "-c", "voltage_threshold=1.5-1.5"],
    ["--samples", "10", "-O", "srzip"],
    ["--samples", "10", "-O", "vcd:bogus=1"],
    ["-d", "fx2lafw", "--scan"],
])
def test_usage_errors_exit_1(argv, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert cli.main(argv, open_device=_opener(_capture(SQUARE))) == 1
    assert not (tmp_path / "x.txt").exists()


def test_device_error_removes_output_file(tmp_path):
    def broken(conn=None):
        raise d.DeviceError("no device")
    path = tmp_path / "cap.csv"
    assert cli.main(["--samples", "10", "-o", str(path)], open_device=broken) == 1
    assert not path.exists()


def test_version(capsys):
    assert cli.main(["-V"]) == 0
    assert capsys.readouterr().out.startswith("la5016 ")


# --- protocol decoders (-P), with a tiny decoder written here ---------------

TOGGLES = '''
import sigrokdecode as srd

class Decoder(srd.Decoder):
    api_version = 3
    id = 'toggles'
    name = 'Toggles'
    longname = 'Toggle counter'
    desc = 'Counts edges.'
    license = 'gplv3+'
    inputs = ['logic']
    outputs = []
    tags = ['Util']
    channels = ({'id': 'd', 'name': 'D', 'desc': 'data'},)
    annotations = (('edge', 'Edge'), ('level', 'Level'))
    binary = (('raw', 'Raw'),)
    options = ({'id': 'bad', 'desc': 'Wrong meta type', 'default': 'no', 'values': ('no', 'yes')},)

    def reset(self):
        pass

    def start(self):
        self.out_ann = self.register(srd.OUTPUT_ANN)
        self.out_bin = self.register(srd.OUTPUT_BINARY)
        self.out_count = self.register(srd.OUTPUT_META, meta=(int, 'Edges', 'Edges so far'))
        self.out_rate = self.register(srd.OUTPUT_META, meta=(float, 'Rate', 'Edges per sample'))

    def decode(self):
        n = 0
        while True:
            pins = self.wait({0: 'e'})
            n += 1
            self.put(0, self.samplenum, self.out_count, n)
            self.put(0, self.samplenum, self.out_rate, n if self.options['bad'] == 'yes' else n / self.samplenum)
            self.put(self.samplenum, self.samplenum, self.out_ann, [0, ['edge %d' % pins[0]]])
            self.put(self.samplenum, self.samplenum, self.out_ann, [1, ['L%d' % pins[0]]])
            self.put(self.samplenum, self.samplenum, self.out_bin, [0, b'%d' % pins[0]])
'''


@pytest.fixture
def toggles(tmp_path, monkeypatch):
    from la5016.srd import loader
    d = tmp_path / "decoders" / "toggles"
    d.mkdir(parents=True)
    (d / "__init__.py").write_text("from .pd import Decoder\n")
    (d / "pd.py").write_text(TOGGLES)
    monkeypatch.setenv("LA5016_DECODERS_DIR", str(tmp_path / "decoders"))
    loader._loaded.clear()
    yield
    loader._loaded.clear()


def test_decoder_annotations_replace_logic_output(toggles, capsysbinary):
    t = _capture(SQUARE)
    rc = cli.main(["--samples", "15", "-P", "toggles:d=CH0", "-A", "toggles=edge"], open_device=_opener(t))
    assert rc == 0
    assert capsysbinary.readouterr().out.decode().splitlines() == [
        "toggles-1: edge 0", "toggles-1: edge 1", "toggles-1: edge 0", "toggles-1: edge 1"]


def test_decoder_samplenum_and_binary_file(toggles, tmp_path, capsysbinary):
    t = _capture(SQUARE)
    assert cli.main(["--samples", "15", "-P", "toggles:d=CH0", "--protocol-decoder-samplenum",
                     "-A", "toggles=level"], open_device=_opener(t)) == 0
    assert capsysbinary.readouterr().out.decode().splitlines()[0] == "3-3 toggles-1: L0"
    path = tmp_path / "raw.bin"
    assert cli.main(["--samples", "15", "-P", "toggles:d=CH0", "-B", "toggles=raw", "-o", str(path)],
                    open_device=_opener(_capture(SQUARE))) == 0
    assert path.read_bytes() == b"0101"


@pytest.mark.parametrize("argv", [
    ["--samples", "15", "-P", "nosuch"],
    ["--samples", "15", "-P", "toggles:d=CH9", "-C", "CH0"],
    ["--samples", "15", "-P", "toggles:speed=3"],
    ["--samples", "15", "-P", "toggles", "-A", "toggles=nope"],
    ["--samples", "15", "-A", "toggles"],
])
def test_decoder_usage_errors(toggles, argv):
    assert cli.main(argv, open_device=_opener(_capture(SQUARE))) == 1


def test_decoder_meta_output(toggles, capsysbinary):
    assert cli.main(["--samples", "15", "-P", "toggles:d=CH0", "-M", "toggles", "-A", "toggles=edge"],
                    open_device=_opener(_capture(SQUARE))) == 0
    assert capsysbinary.readouterr().out.decode().splitlines() == [   # -M replaces annotations
        "toggles-1: Edges: 1", "toggles-1: Rate: 0.33333333333333331",
        "toggles-1: Edges: 2", "toggles-1: Rate: 0.33333333333333331",
        "toggles-1: Edges: 3", "toggles-1: Rate: 0.33333333333333331",
        "toggles-1: Edges: 4", "toggles-1: Rate: 0.33333333333333331"]
    assert cli.main(["--samples", "15", "-P", "toggles:d=CH0", "-M", "toggles,", "--protocol-decoder-samplenum"],
                    open_device=_opener(_capture(SQUARE))) == 0
    assert capsysbinary.readouterr().out.decode().splitlines()[:2] == [
        "0-3 toggles-1: Edges: 1", "0-3 toggles-1: Rate: 0.33333333333333331"]


def test_decoder_binary_wins_over_meta(toggles, tmp_path):
    path = tmp_path / "raw.bin"
    assert cli.main(["--samples", "15", "-P", "toggles:d=CH0", "-M", "toggles", "-B", "toggles", "-o", str(path)],
                    open_device=_opener(_capture(SQUARE))) == 0
    assert path.read_bytes() == b"0101"


@pytest.mark.parametrize("argv, msg", [
    (["-P", "toggles:d=CH0", "-M", "uart"], "Protocol decoder 'uart' not found."),
    (["-M", "toggles"], "-A/-B/-M need protocol decoders (-P)"),
    (["-P", "toggles:d=CH0:bad=yes", "-M", "toggles"], "registered as 'float'"),
])
def test_decoder_meta_errors(toggles, caplog, argv, msg):
    assert cli.main(["--samples", "15"] + argv, open_device=_opener(_capture(SQUARE))) == 1
    assert msg in caplog.text


def test_show_decoder_without_device(toggles, capsys):
    assert cli.main(["--show", "-P", "toggles"]) == 0
    assert capsys.readouterr().out.startswith("ID: toggles\nName: Toggles\n")


# --- streaming (continuous=on) ----------------------------------------------

def _stream_words(samples, channels):
    import struct
    out = b""
    for g in range(0, len(samples), 16):
        for ch in channels:
            w = 0
            for i, s in enumerate(samples[g:g + 16]):
                w |= ((s >> ch) & 1) << i
            out += struct.pack("<H", w)
    return out


STALE = b"\xff\xff\x00\xff"   # one-channel streams drop their first two words


def test_stream_to_csv(capsysbinary):
    t = FakeTransport()
    samples = [i % 2 for i in range(48)]
    raw = STALE + _stream_words(samples, [0])
    t.stream_data = [raw[:3], raw[3:5], raw[5:]]
    rc = cli.main(["-c", "continuous=on:samplerate=1m", "--samples", "40", "-C", "CH0",
                   "-O", "csv:header=false:label=off"], open_device=_opener(t))
    assert rc == 0
    assert capsysbinary.readouterr().out.decode().split() == [str(s) for s in samples[:40]]
    assert t.writes(d.REG_RUN)[-1] == bytes([d.RUNMODE_HALT])


def test_stream_break_off_keeps_output_and_fails(tmp_path):
    t = FakeTransport()
    t.stream_data = [STALE + _stream_words([1] * 32, [0])]
    t.stream_error = "stream stopped: no data"
    path = tmp_path / "s.csv"
    rc = cli.main(["-c", "continuous=on:samplerate=1m", "-C", "CH0", "-O", "csv:header=false:label=off",
                   "-o", str(path)], open_device=_opener(t))
    assert rc == 1
    assert path.read_text().split() == ["1"] * 32


@pytest.mark.parametrize("argv", [
    ["-c", "continuous=on", "-t", "CH0=r", "--samples", "10"],
    ["-c", "continuous=on:samplerate=200m", "--samples", "10"],
])
def test_stream_usage_errors(argv):
    assert cli.main(argv, open_device=_opener(FakeTransport())) == 1


def test_stream_default_samplerate_is_100m(capsysbinary):
    t = FakeTransport()
    t.stream_data = [STALE + _stream_words([0] * 16, [0])]
    assert cli.main(["-c", "continuous=on", "--samples", "16", "-C", "CH0", "-O", "vcd"],
                    open_device=_opener(t)) == 0
    assert b"at 100 MHz" in capsysbinary.readouterr().out


# --- PWM (-g PWMx -c ... --set) ----------------------------------------------

def test_pwm_set_and_off(capsys):
    t = FakeTransport()
    assert cli.main(["-g", "PWM1", "-c", "output_frequency=1k:duty_cycle=25%", "--set"],
                    open_device=_opener(t)) == 0
    assert capsys.readouterr().out == "PWM1: on, 1 kHz, duty 25 %\n"
    assert t.writes(d.REG_PWM1) == [__import__("struct").pack("<II", 200_000, 50_000)]
    assert cli.main(["-g", "PWM1", "-c", "enabled=off", "--set"], open_device=_opener(t)) == 0
    assert capsys.readouterr().out == "PWM1: off\n"


@pytest.mark.parametrize("argv", [
    ["-g", "PWM1", "-c", "output_frequency=1k"],                    # no --set
    ["--set", "-c", "samplerate=1m"],                               # no -g
    ["-g", "PWM3", "-c", "enabled=on", "--set"],
    ["-g", "PWM1", "-c", "speed=3", "--set"],
    ["-g", "PWM1", "-c", "output_frequency=30m", "--set"],
    ["-g", "PWM1", "-c", "duty_cycle=0", "--set"],
    ["-g", "PWM1", "--set"],
])
def test_pwm_usage_errors(argv):
    assert cli.main(argv, open_device=_opener(FakeTransport())) == 1
