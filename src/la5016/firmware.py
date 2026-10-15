"""Extract the Kingst FX2 firmware and FPGA bitstreams from a KingstVIS executable.

Kingst does not distribute these files separately, so every user extracts them
from their own KingstVIS copy. The files land in a per-user data directory and
must never be committed or redistributed.

sigrok's sigrok-fwextract-kingst-la2016 locates the Qt resource tables through
ELF symbols. The macOS build is a stripped Mach-O, so this module finds them by
content instead, which works for any executable format:

1. Name table: entries are (u16 length BE, u32 qt_hash BE, UTF-16BE name).
   The anchor is the "fwfpga" entry; the hash makes false hits unlikely.
2. Struct table: fixed-size entries (14 bytes in rcc format 1, 22 in 2/3),
   starting with a root directory (name 0, flags DIR, first child 1).
   Each root candidate is walked and accepted when every name resolves in the
   name table and the tree contains fwfpga/.
3. Data table: entries are (u32 size BE, payload). The file with data offset 0
   is the first entry, so the table base is where that size field sits; it is
   found by searching for the size implied by the next offset and validated
   against every file.

Compressed entries (flag 0x01) hold a u32 BE uncompressed length plus zlib data.
FX2 firmware stored as Intel HEX is converted to a flat image, as sigrok does.
"""

from __future__ import annotations

import hashlib
import os
import re
import struct
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

DEFAULT_KINGSTVIS = "/Applications/KingstVIS.app"

FLAG_COMPRESSED = 0x01
FLAG_DIRECTORY = 0x02
FLAG_COMPRESSED_ZSTD = 0x04


class FirmwareError(Exception):
    pass


def firmware_dir() -> Path:
    """Per-user directory that holds the extracted files."""
    env = os.environ.get("LA5016_FIRMWARE_DIR")
    if env:
        return Path(env)
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "la5016" / "firmware"
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(home / "AppData" / "Local")
        return Path(base) / "la5016" / "firmware"
    base = os.environ.get("XDG_DATA_HOME") or str(home / ".local" / "share")
    return Path(base) / "la5016" / "firmware"


def mcu_firmware_name(pid: int) -> str:
    return "kingst-la-%04x.fw" % pid


def fpga_bitstream_name(stem: str) -> str:
    return "kingst-%s-fpga.bitstream" % stem.lower()


def load(name: str) -> bytes:
    path = firmware_dir() / name
    try:
        return path.read_bytes()
    except FileNotFoundError:
        raise FirmwareError(
            "missing firmware file %s; run 'la5016 --extract-firmware' first" % path
        ) from None


# --- Qt rcc tables ---------------------------------------------------------


def qt_hash(name: str) -> int:
    h = 0
    for unit in _utf16_units(name):
        h = ((h << 4) + unit) & 0xFFFFFFFF
        h ^= (h & 0xF0000000) >> 23
        h &= 0x0FFFFFFF
    return h


def _utf16_units(name: str) -> Tuple[int, ...]:
    raw = name.encode("utf-16-be")
    return struct.unpack(">%dH" % (len(raw) // 2), raw)


def _name_at(blob: bytes, pos: int) -> Optional[str]:
    if pos < 0 or pos + 6 > len(blob):
        return None
    length, h = struct.unpack_from(">HI", blob, pos)
    end = pos + 6 + 2 * length
    if length == 0 or end > len(blob):
        return None
    try:
        name = blob[pos + 6:end].decode("utf-16-be")
    except UnicodeDecodeError:
        return None
    return name if qt_hash(name) == h else None


def _name_entries(blob: bytes, name: str) -> List[int]:
    """Offsets of hash-valid name table entries spelling `name`."""
    needle = name.encode("utf-16-be")
    out = []
    i = blob.find(needle)
    while i != -1:
        if _name_at(blob, i - 6) == name:
            out.append(i - 6)
        i = blob.find(needle, i + 1)
    return out


@dataclass
class _Node:
    name: str
    flags: int
    data_offset: int = 0
    children: Optional[List["_Node"]] = None


def _walk(blob: bytes, struct_base: int, name_base: int, entry_size: int,
          index: int, depth: int = 0) -> Optional[_Node]:
    if depth > 16:
        return None
    pos = struct_base + index * entry_size
    if pos + 14 > len(blob):
        return None
    name_off, flags = struct.unpack_from(">IH", blob, pos)
    if index == 0:
        name = ""
    else:
        name = _name_at(blob, name_base + name_off)
        if name is None:
            return None
    if flags & FLAG_DIRECTORY:
        count, first = struct.unpack_from(">II", blob, pos + 6)
        if count > 4096 or first == 0:
            return None
        children = []
        for child in range(first, first + count):
            node = _walk(blob, struct_base, name_base, entry_size, child, depth + 1)
            if node is None:
                return None
            children.append(node)
        return _Node(name, flags, children=children)
    _country, _lang, data_off = struct.unpack_from(">HHI", blob, pos + 6)
    return _Node(name, flags, data_offset=data_off)


def _files(node: _Node, prefix: str = "") -> Iterator[Tuple[str, _Node]]:
    for child in node.children or []:
        path = prefix + child.name
        if child.children is not None:
            yield from _files(child, path + "/")
        else:
            yield path, child


# Root directory entry: name offset 0, flags DIR, any child count, first child 1.
_ROOT_ENTRY = re.compile(b"\\x00\\x00\\x00\\x00\\x00\\x02(?s:.{4})\\x00\\x00\\x00\\x01")


def _find_tree(blob: bytes) -> Tuple[_Node, int]:
    """Return the resource tree that contains fwfpga/, plus its name table base."""
    anchors = _name_entries(blob, "fwfpga")
    if not anchors:
        raise FirmwareError("no Qt resource named 'fwfpga' found; is this KingstVIS?")
    for entry_size in (14, 22):
        for m in _ROOT_ENTRY.finditer(blob):
            struct_base = m.start()
            # The fwfpga directory entry tells where the name table starts.
            for anchor in anchors:
                for i in range(1, 64):
                    pos = struct_base + i * entry_size
                    if pos + 6 > len(blob):
                        break
                    name_off, flags = struct.unpack_from(">IH", blob, pos)
                    if not flags & FLAG_DIRECTORY or name_off > anchor:
                        continue
                    name_base = anchor - name_off
                    tree = _walk(blob, struct_base, name_base, entry_size, 0)
                    if tree is not None and any(p.split("/")[-2:-1] == ["fwfpga"] for p, _ in _files(tree)):
                        return tree, name_base
    raise FirmwareError("could not locate the Qt resource struct table")


def _find_data_base(blob: bytes, files: List[_Node]) -> int:
    by_off = sorted({f.data_offset for f in files})
    if len(by_off) < 2 or by_off[0] != 0:
        raise FirmwareError("unexpected Qt resource data layout")
    first_size = by_off[1] - 4
    needle = struct.pack(">I", first_size)
    i = blob.find(needle)
    while i != -1:
        if all(_entry_ok(blob, i, f) for f in files):
            return i
        i = blob.find(needle, i + 1)
    raise FirmwareError("could not locate the Qt resource data table")


def _entry_ok(blob: bytes, base: int, f: _Node) -> bool:
    pos = base + f.data_offset
    if pos + 4 > len(blob):
        return False
    size = struct.unpack_from(">I", blob, pos)[0]
    if pos + 4 + size > len(blob):
        return False
    if f.flags & FLAG_COMPRESSED:
        return size > 6 and blob[pos + 8] == 0x78
    return True


def _payload(blob: bytes, base: int, f: _Node) -> bytes:
    pos = base + f.data_offset
    size = struct.unpack_from(">I", blob, pos)[0]
    data = blob[pos + 4:pos + 4 + size]
    if f.flags & FLAG_COMPRESSED_ZSTD:
        raise FirmwareError("zstd-compressed resources are not supported")
    if f.flags & FLAG_COMPRESSED:
        expect = struct.unpack_from(">I", data)[0]
        data = zlib.decompress(data[4:])
        if len(data) != expect:
            raise FirmwareError("resource size mismatch after decompression")
    return data


def read_resources(blob: bytes) -> Dict[str, bytes]:
    """All files of the resource tree that holds the Kingst firmware, by path."""
    tree, _ = _find_tree(blob)
    files = dict(_files(tree))
    base = _find_data_base(blob, list(files.values()))
    return {path: _payload(blob, base, node) for path, node in files.items()}


# --- Intel HEX -------------------------------------------------------------


def intel_hex_to_blob(text: bytes) -> bytes:
    """Flat image from address 0 to the last data byte, like sigrok's extractor."""
    parts = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line[:1] != b":":
            raise FirmwareError("invalid Intel HEX line %r" % line[:20])
        rec = bytes.fromhex(line[1:].decode("ascii"))
        count, addr, rtype = struct.unpack_from(">BHB", rec)
        if len(rec) != 5 + count or (sum(rec) & 0xFF) != 0:
            raise FirmwareError("bad Intel HEX record %r" % line[:20])
        if rtype == 0:
            parts.append((addr, rec[4:4 + count]))
        elif rtype == 1:
            break
    if not parts:
        raise FirmwareError("empty Intel HEX file")
    parts.sort()
    end = max(a + len(d) for a, d in parts)
    img = bytearray(end)
    for addr, data in parts:
        img[addr:addr + len(data)] = data
    return bytes(img)


def _maybe_hex(data: bytes) -> bytes:
    if data[:1] == b":" and max(data) < 127:
        return intel_hex_to_blob(data)
    return data


# --- Extraction ------------------------------------------------------------

_RULES = (
    (re.compile(r"(?:^|/)fwusb/fw(.+)$"), lambda m: "kingst-la-%s.fw" % m.group(1).lower(), _maybe_hex),
    (re.compile(r"(?:^|/)fwfpga/(LA5016.*)$"), lambda m: fpga_bitstream_name(m.group(1)), lambda d: d),
)


def extract(blob: bytes) -> Dict[str, bytes]:
    """Map sigrok-style file names to their contents."""
    out = {}
    for path, data in read_resources(blob).items():
        for pattern, rename, convert in _RULES:
            m = pattern.search(path)
            if m:
                out[rename(m)] = convert(data)
    if not any(n.endswith(".fw") for n in out) or not any(n.endswith(".bitstream") for n in out):
        raise FirmwareError("KingstVIS resources lack FX2 firmware or LA5016 bitstreams")
    return out


def executable_path(app: str) -> Path:
    p = Path(app)
    if p.is_dir():
        mac = p / "Contents" / "MacOS" / "KingstVIS"
        if mac.exists():
            return mac
        for name in ("KingstVIS.exe", "KingstVIS"):
            if (p / name).exists():
                return p / name
        raise FirmwareError("no KingstVIS executable inside %s" % p)
    return p


def install(app: str = DEFAULT_KINGSTVIS, dest: Optional[Path] = None) -> List[Tuple[Path, int, str]]:
    """Extract into `dest` (default: firmware_dir()). Returns (path, size, sha256)."""
    exe = executable_path(app)
    try:
        blob = exe.read_bytes()
    except OSError as e:
        raise FirmwareError("cannot read %s: %s" % (exe, e)) from None
    files = extract(blob)
    dest = dest or firmware_dir()
    dest.mkdir(parents=True, exist_ok=True)
    result = []
    for name in sorted(files):
        path = dest / name
        path.write_bytes(files[name])
        result.append((path, len(files[name]), hashlib.sha256(files[name]).hexdigest()))
    return result
