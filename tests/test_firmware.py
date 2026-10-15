"""Firmware extraction against synthetic Qt resource tables (no Kingst files)."""

import struct
import zlib

import pytest

from la5016 import firmware as fw


def _name(name):
    raw = name.encode("utf-16-be")
    return struct.pack(">HI", len(name), fw.qt_hash(name)) + raw


def _hex_record(addr, data, rtype=0):
    rec = struct.pack(">BHB", len(data), addr, rtype) + data
    rec += bytes([(-sum(rec)) & 0xFF])
    return b":" + rec.hex().upper().encode()


def build_rcc(files, entry_size=14, junk=b"\x00" * 37):
    """files: list of (dir, name, payload, compressed). Returns an executable-like blob."""
    dirs = sorted({d for d, *_ in files})
    names = b""
    name_off = {}
    for n in ["res"] + dirs + [f[1] for f in files]:
        if n not in name_off:
            name_off[n] = len(names)
            names += _name(n)
    data = b""
    data_off = []
    for _, _, payload, compressed in files:
        data_off.append(len(data))
        body = struct.pack(">I", len(payload)) + zlib.compress(payload) if compressed else payload
        data += struct.pack(">I", len(body)) + body

    pad = b"\x00" * (entry_size - 14)

    def dir_entry(n, count, first, root=False):
        return struct.pack(">IHII", 0 if root else name_off[n], fw.FLAG_DIRECTORY, count, first) + pad

    def file_entry(n, off, flags):
        return struct.pack(">IHHHI", name_off[n], flags, 0, 0, off) + pad

    # index 0 root -> 1 "res" -> dirs at 2.. -> files after
    entries = [dir_entry("", 1, 1, root=True), dir_entry("res", len(dirs), 2)]
    first_file = 2 + len(dirs)
    idx = first_file
    dir_entries = []
    for d in dirs:
        members = [i for i, f in enumerate(files) if f[0] == d]
        dir_entries.append(dir_entry(d, len(members), idx))
        idx += len(members)
    entries += dir_entries
    order = [i for d in dirs for i, f in enumerate(files) if f[0] == d]
    for i in order:
        entries.append(file_entry(files[i][1], data_off[i], fw.FLAG_COMPRESSED if files[i][3] else 0))
    return junk + b"".join(entries) + names + data + junk


HEX = b"\n".join([
    _hex_record(0x0000, b"\x02\x0d\x9c"),
    _hex_record(0x0010, b"\xaa\xbb"),
    _hex_record(0, b"", rtype=1),
]) + b"\n"

FILES = [
    ("fwusb", "fw01A2", HEX, False),
    ("fwusb", "fw01A1", b"\x00binary", True),
    ("fwfpga", "LA5016A1", b"\xff" * 64 + b"a1", False),
    ("fwfpga", "LA5016A2", b"\xff" * 300 + b"a2", True),
    ("fwfpga", "LA2016", b"\xff" * 10, False),
    ("lang", "en-US", b"qm", False),
]


@pytest.mark.parametrize("entry_size", [14, 22])
def test_read_resources(entry_size):
    res = fw.read_resources(build_rcc(FILES, entry_size))
    assert res["res/fwfpga/LA5016A2"] == b"\xff" * 300 + b"a2"
    assert res["res/fwusb/fw01A1"] == b"\x00binary"
    assert res["res/lang/en-US"] == b"qm"


def test_extract_names_and_hex_conversion():
    out = fw.extract(build_rcc(FILES))
    assert sorted(out) == [
        "kingst-la-01a1.fw",
        "kingst-la-01a2.fw",
        "kingst-la5016a1-fpga.bitstream",
        "kingst-la5016a2-fpga.bitstream",
    ]
    assert out["kingst-la-01a2.fw"] == b"\x02\x0d\x9c" + b"\x00" * 13 + b"\xaa\xbb"
    assert out["kingst-la-01a1.fw"] == b"\x00binary"


def test_intel_hex_bad_checksum():
    bad = HEX.replace(b"9C", b"9D", 1)
    with pytest.raises(fw.FirmwareError):
        fw.intel_hex_to_blob(bad)


def test_not_kingstvis():
    with pytest.raises(fw.FirmwareError, match="fwfpga"):
        fw.extract(b"\x00" * 1000)


def test_install_writes_files(tmp_path):
    app = tmp_path / "KingstVIS"
    app.write_bytes(build_rcc(FILES))
    dest = tmp_path / "out"
    result = fw.install(str(app), dest)
    assert {p.name for p, _, _ in result} == {p.name for p in dest.iterdir()}
    assert all(len(h) == 64 for _, _, h in result)


def test_load_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("LA5016_FIRMWARE_DIR", str(tmp_path))
    with pytest.raises(fw.FirmwareError, match="--extract-firmware"):
        fw.load("kingst-la-01a2.fw")


def test_names():
    assert fw.mcu_firmware_name(0x01A2) == "kingst-la-01a2.fw"
    assert fw.fpga_bitstream_name("LA5016A1") == "kingst-la5016a1-fpga.bitstream"
