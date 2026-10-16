"""srzip (.sr) output and input."""

import io
import zipfile

import numpy as np
import pytest

from kingst_la import cli, input as inputs, options
from kingst_la.input.srzip import parse_keyfile, parse_size
from kingst_la.options import UsageError
from kingst_la.output import create, srzip
from kingst_la.pipeline import Channel, Meta

from test_cli import SQUARE, _capture, _opener, toggles  # noqa: F401  (fixture)

CHANNELS = [Channel(0, "CH0"), Channel(1, "CH1"), Channel(3, "SDA"), Channel(9, "CH9")]


class Collect:
    def __init__(self):
        self.meta = None
        self.parts = []
        self.ended = False

    def begin(self, meta):
        self.meta = meta

    def feed(self, a):
        self.parts.append(a.copy())

    def trigger(self):
        pass

    def end(self):
        self.ended = True

    @property
    def samples(self):
        return np.concatenate(self.parts) if self.parts else np.zeros(0, np.uint16)


def write_sr(path, samples, channels=CHANNELS, rate=1_000_000, pieces=1, total=16):
    with open(path, "wb") as f:
        out = create("srzip", f)
        out.begin(Meta(rate, channels, total))
        for part in np.array_split(samples, pieces):
            out.feed(part)
        out.trigger()
        out.end()


def make_sr(path, metadata, entries, version="2"):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("version", version)
        zf.writestr("metadata", metadata)
        for name, data in entries.items():
            zf.writestr(name, data)


def test_metadata_matches_libsigrok(tmp_path):
    path = tmp_path / "a.sr"
    write_sr(path, np.arange(40, dtype=np.uint16), channels=[CHANNELS[2], CHANNELS[0]], rate=12208)
    with zipfile.ZipFile(path) as zf:
        assert sorted(zf.namelist()) == ["logic-1-1", "metadata", "version"]
        assert zf.read("version") == b"2"
        assert zf.getinfo("logic-1-1").compress_type == zipfile.ZIP_DEFLATED
        meta = zf.read("metadata").decode()
        data = np.frombuffer(zf.read("logic-1-1"), "<u2")
    assert meta == (
        "[global]\n"
        "sigrok version=kingst-la %s\n"
        "\n"
        "[device 1]\n"
        "capturefile=logic-1\n"
        "total probes=16\n"
        "samplerate=12.208 kHz\n"
        "total analog=0\n"
        "probe1=CH0\n"
        "probe4=SDA\n"
        "unitsize=2\n" % __import__("kingst_la").__version__)
    # Bits of disabled channels are cleared.
    assert data.tolist() == (np.arange(40) & 0b1001).tolist()


def test_round_trip_in_chunks(tmp_path, monkeypatch):
    monkeypatch.setattr(srzip, "CHUNK_SIZE", 64)          # 32 samples per entry
    monkeypatch.setattr(inputs.srzip, "READ_SAMPLES", 7)  # odd read sizes
    x = np.random.default_rng(1).integers(0, 1 << 16, 1000).astype(np.uint16)
    path = tmp_path / "a.sr"
    write_sr(path, x, pieces=9)
    src = inputs.open_srzip(str(path))
    assert src.entries == ["logic-1-%d" % i for i in range(1, 33)]
    assert (src.samplerate, src.total_channels, src.unitsize) == (1_000_000, 16, 2)
    assert src.channels == CHANNELS
    sink = Collect()
    assert src.read(sink, src.channels) == 1000
    assert sink.ended and sink.meta == Meta(1_000_000, CHANNELS, 16)
    assert sink.samples.tolist() == (x & 0b1000001011).tolist()


def test_empty_capture_has_no_unitsize(tmp_path):
    path = tmp_path / "a.sr"
    write_sr(path, np.zeros(0, np.uint16))
    with zipfile.ZipFile(path) as zf:
        assert "unitsize" not in zf.read("metadata").decode()
        assert sorted(zf.namelist()) == ["metadata", "version"]
    src = inputs.open_srzip(str(path))
    assert src.entries == [] and src.read(Collect(), src.channels) == 0


def test_eight_channel_file_uses_unitsize_1(tmp_path):
    path = tmp_path / "a.sr"
    write_sr(path, np.array([1, 2, 3, 0x81], np.uint16), channels=[Channel(0, "D0"), Channel(7, "D7")], total=8)
    with zipfile.ZipFile(path) as zf:
        assert zf.read("logic-1-1") == bytes([1, 0, 1, 0x81])
        assert "total probes=8\n" in zf.read("metadata").decode()


def test_needs_a_seekable_file():
    class Pipe(io.RawIOBase):
        def writable(self):
            return True
    with pytest.raises(ValueError, match="needs a file"):
        create("srzip", Pipe())


def test_reads_foreign_files(tmp_path):
    # unitsize 1, single unchunked "logic-1" entry, version 1, escaped names, comment
    path = tmp_path / "fx2.sr"
    make_sr(path, "# saved elsewhere\n[global]\nsigrok version=0.5.2\n\n[device 1]\ndriver=fx2lafw\n"
                  "capturefile=logic-1\ntotal probes=8\nsamplerate=24 MHz\ntotal analog=0\n"
                  "probe1=D0\nprobe3=\\sa\\\\b\nunitsize=1\n",
            {"logic-1": bytes([0, 1, 4, 5, 0xFF])}, version="1")
    src = inputs.open_srzip(str(path))
    assert src.samplerate == 24_000_000
    assert src.channels == [Channel(0, "D0"), Channel(2, " a\\b")]
    sink = Collect()
    src.read(sink, src.channels)
    assert sink.samples.tolist() == [0, 1, 4, 5, 5]
    assert sink.meta.total_channels == 8


def test_reads_wide_files_and_drops_partial_sample(tmp_path):
    # 32 channels (unitsize 4): only the low 16 bits are used.
    path = tmp_path / "w.sr"
    data = np.array([0x0001_8001, 0xFFFF_8000], "<u4").tobytes()
    make_sr(path, "[device 1]\ncapturefile=logic-1\ntotal probes=32\nsamplerate=1 kHz\n"
                  "probe1=A\nprobe16=B\nunitsize=4\n",
            {"logic-1-1": data[:5], "logic-1-2": data[5:] + b"\x07"})
    src = inputs.open_srzip(str(path))
    sink = Collect()
    assert src.read(sink, src.channels) == 2
    assert sink.samples.tolist() == [0x8001, 0x8000]
    assert sink.meta.total_channels == 16


@pytest.mark.parametrize("metadata, entries, version, match", [
    ("[device 1]\ncapturefile=logic-1\ntotal probes=8\nsamplerate=1 MHz\nprobe1=A\n", {}, "3", "version"),
    ("[global]\nx=1\n", {}, "2", "device 1"),
    ("[device 1]\ntotal probes=8\nsamplerate=1 MHz\n", {}, "2", "no logic data"),
    ("[device 1]\ncapturefile=logic-1\ntotal probes=8\nprobe1=A\n", {}, "2", "samplerate"),
    ("[device 1]\ncapturefile=logic-1\ntotal probes=8\nsamplerate=fast\nprobe1=A\n", {}, "2", "samplerate"),
    ("[device 1]\ncapturefile=logic-1\ntotal probes=8\nsamplerate=1 MHz\nprobe9=A\n", {}, "2", "probe9"),
    ("[device 1]\ncapturefile=logic-1\ntotal probes=24\nsamplerate=1 MHz\nprobe17=Q\n", {}, "2", "beyond"),
    ("[device 1]\ncapturefile=logic-1\ntotal probes=8\nsamplerate=1 MHz\n", {}, "2", "no enabled"),
    ("[device 1]\ncapturefile=logic-1\ntotal probes=x\nsamplerate=1 MHz\n", {}, "2", "total probes"),
    ("no group=1\n", {}, "2", "malformed"),
])
def test_bad_session_files(tmp_path, metadata, entries, version, match):
    path = tmp_path / "bad.sr"
    make_sr(path, metadata, entries, version)
    with pytest.raises(inputs.InputError, match=match):
        inputs.open_srzip(str(path))


def test_not_a_session_file(tmp_path):
    (tmp_path / "x.sr").write_text("hello")
    with pytest.raises(inputs.InputError, match="not a sigrok session file"):
        inputs.open_srzip(str(tmp_path / "x.sr"))
    with zipfile.ZipFile(tmp_path / "y.sr", "w") as zf:
        zf.writestr("readme", "x")
    with pytest.raises(inputs.InputError, match="no version"):
        inputs.open_srzip(str(tmp_path / "y.sr"))
    with pytest.raises(inputs.InputError, match="not found"):
        inputs.open_srzip(str(tmp_path / "missing.sr"))


@pytest.mark.parametrize("text, hz", [("1 MHz", 1_000_000), ("12.208 kHz", 12208), ("500000", 500000),
                                      ("2.5 GHz", 2_500_000_000), ("20k", 20000), ("1 Hz", 1)])
def test_parse_size(text, hz):
    assert parse_size(text) == hz


def test_keyfile_escape_round_trip():
    names = [" lead", "a\\b", "tab\there", "nl\nx", "trail ", "x=y"]
    text = srzip.keyfile_text([("g", [("k%d" % i, n) for i, n in enumerate(names)])])
    assert parse_keyfile(text)["g"] == {"k%d" % i: n for i, n in enumerate(names)}


# ---- -C over file channels --------------------------------------------------

def test_select_channels():
    sel = options.select_channels
    assert sel(None, CHANNELS) == CHANNELS
    assert sel("SDA,CH0", CHANNELS) == [Channel(3, "SDA"), Channel(0, "CH0")]
    assert sel("CH3=DATA", CHANNELS) == [Channel(3, "DATA")]            # CHn names the bit
    assert sel("CH1-CH9", CHANNELS) == CHANNELS[1:]                       # file channels in range
    assert sel("D-1", [Channel(0, "D-1")]) == [Channel(0, "D-1")]         # a name with '-'


@pytest.mark.parametrize("text, match", [
    ("", "empty"), ("CH2", "no channel 'CH2'"), ("CH0,CH0", "more than once"),
    ("CH0=X,CH1=X", "more than once"), ("CH0-CH1=A", "range"), ("CH9-CH0", "after"), ("CH0=", "empty"),
])
def test_select_channels_errors(text, match):
    with pytest.raises(UsageError, match=match):
        options.select_channels(text, CHANNELS)


# ---- CLI ------------------------------------------------------------------

def test_capture_to_sr_then_read_back(tmp_path, capsysbinary):
    path = tmp_path / "cap.sr"
    assert cli.main(["-c", "samplerate=1m", "--samples", "9", "-C", "CH0=CLK", "-o", str(path)],
                    open_device=_opener(_capture(SQUARE))) == 0
    src = inputs.open_srzip(str(path))
    assert src.channels == [Channel(0, "CLK")] and src.samplerate == 1_000_000
    capsysbinary.readouterr()
    assert cli.main(["-i", str(path), "-O", "csv:header=false:label=off"]) == 0
    assert capsysbinary.readouterr().out.decode().split() == ["1", "1", "1", "0", "0", "0", "1", "1", "1"]
    assert cli.main(["-i", str(path), "-I", "srzip", "--samples", "4", "-C", "CLK=X", "-O", "bits"]) == 0
    assert capsysbinary.readouterr().out.decode().splitlines()[1:] == [
        "Acquisition with 1/16 channels at 1 MHz", "X:1110"]


def test_input_time_and_conversion(tmp_path):
    src_path, vcd = tmp_path / "a.sr", tmp_path / "a.vcd"
    write_sr(src_path, np.arange(2000, dtype=np.uint16) & 1, channels=[Channel(0, "CH0")])
    assert cli.main(["-i", str(src_path), "--time", "1", "-o", str(vcd)]) == 0   # 1 ms at 1 MHz
    text = vcd.read_text()
    assert "$var wire 1 ! CH0 $end" in text and text.rstrip().endswith("#1000")
    copy = tmp_path / "b.sr"
    assert cli.main(["-i", str(src_path), "--samples", "10", "-o", str(copy)]) == 0
    sink = Collect()
    inputs.open_srzip(str(copy)).read(sink, [Channel(0, "CH0")])
    assert sink.samples.tolist() == [0, 1] * 5


def test_input_runs_decoders(toggles, tmp_path, capsysbinary):  # noqa: F811
    path = tmp_path / "a.sr"
    write_sr(path, np.repeat(np.array([1, 0, 1, 0, 1], np.uint16), 3), channels=[Channel(0, "CH0")])
    assert cli.main(["-i", str(path), "-P", "toggles:d=CH0", "-A", "toggles=edge"]) == 0
    assert capsysbinary.readouterr().out.decode().splitlines() == [
        "toggles-1: edge 0", "toggles-1: edge 1", "toggles-1: edge 0", "toggles-1: edge 1"]


@pytest.mark.parametrize("extra, msg", [
    (["-c", "samplerate=1m"], "-c cannot be used with -i"),
    (["-t", "CH0=r"], "-t cannot"),
    (["-d", "kingst-la2016"], "-d cannot"),
    (["-I", "vcd"], "unsupported input format"),
    (["-I", "srzip:x=1"], "no options"),
    (["-C", "CH5"], "no channel 'CH5'"),
    (["-g", "PWM1", "--set"], "-g/--set"),
    (["--samples", "1", "--time", "1"], "mutually exclusive"),
    (["-O", "srzip"], "needs a file"),
])
def test_input_usage_errors(tmp_path, caplog, extra, msg):
    path = tmp_path / "a.sr"
    write_sr(path, np.zeros(4, np.uint16), channels=[Channel(0, "CH0")])
    assert cli.main(["-i", str(path)] + extra) == 1
    assert msg in caplog.text


def test_input_errors(tmp_path, caplog):
    assert cli.main(["-i", str(tmp_path / "none.sr")]) == 1
    assert "not found" in caplog.text
    path = tmp_path / "a.sr"
    write_sr(path, np.zeros(4, np.uint16), channels=[Channel(0, "CH0")])
    before = path.read_bytes()
    assert cli.main(["-i", str(path), "-o", str(path)]) == 1
    assert "overwrite the input" in caplog.text and path.read_bytes() == before
    assert cli.main(["-I", "srzip", "--samples", "1"]) == 1
    assert "-I needs an input file" in caplog.text
