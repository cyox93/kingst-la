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
