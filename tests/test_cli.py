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
    ["--samples", "10", "-o", "x.txt"],
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

    def reset(self):
        pass

    def start(self):
        self.out_ann = self.register(srd.OUTPUT_ANN)
        self.out_bin = self.register(srd.OUTPUT_BINARY)

    def decode(self):
        while True:
            pins = self.wait({0: 'e'})
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


def test_show_decoder_without_device(toggles, capsys):
    assert cli.main(["--show", "-P", "toggles"]) == 0
    assert capsys.readouterr().out.startswith("ID: toggles\nName: Toggles\n")
