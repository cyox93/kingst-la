"""srzip input: read a sigrok session file (.sr) into the sample pipeline.

Follows libsigrok src/session_file.c (sr_session_load) for the metadata
and src/session_driver.c for the data: either a single "logic-1" entry
or chunks "logic-1-1", "logic-1-2", ... read in order until one is missing.
Samples become uint16 values with bit n for probe n+1, so only the first
16 logic channels of a file can be used.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, Iterator, List, Optional

import numpy as np

from ..pipeline import Channel, Meta, Sink

MAX_CHANNELS = 16
READ_SAMPLES = 1 << 20

_SIZE_RE = re.compile(r"\s*(\d+(?:\.\d*)?)\s*([kKmMgGtT]?)\s*(?:[hH][zZ])?\s*")
_SIZE_MULT = {"": 1, "k": 10**3, "m": 10**6, "g": 10**9, "t": 10**12}
_UNESCAPE = {"s": " ", "n": "\n", "t": "\t", "r": "\r", "\\": "\\"}


class InputError(ValueError):
    pass


def parse_keyfile(text: str) -> Dict[str, Dict[str, str]]:
    """Parse GKeyFile text: [group] headers, key=value lines, # comments."""
    groups: Dict[str, Dict[str, str]] = {}
    cur: Optional[Dict[str, str]] = None
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("[") and s.endswith("]"):
            cur = groups.setdefault(s[1:-1], {})
            continue
        key, sep, value = line.partition("=")
        if not sep or cur is None:
            raise InputError("malformed metadata line: %r" % line)
        cur[key.strip()] = _unescape(value.lstrip())
    return groups


def _unescape(value: str) -> str:
    return re.sub(r"\\(.)", lambda m: _UNESCAPE.get(m.group(1), m.group(0)), value)


def parse_size(text: str) -> int:
    """sr_parse_sizestring(): "1 MHz", "12.208 kHz", "500000" -> Hz."""
    m = _SIZE_RE.fullmatch(text)
    if not m:
        raise InputError("invalid samplerate %r in metadata" % text)
    return int(Decimal(m.group(1)) * _SIZE_MULT[m.group(2).lower()])


@dataclass
class SrzipFile:
    path: str
    samplerate: int
    total_channels: int      # logic channels in the file, enabled or not
    unitsize: int            # bytes per sample
    channels: List[Channel]  # enabled channels, by index
    entries: List[str]       # data entries in order

    def meta(self, channels: List[Channel]) -> Meta:
        return Meta(self.samplerate, channels, min(self.total_channels, MAX_CHANNELS))

    def read(self, sink: Sink, channels: List[Channel], limit: Optional[int] = None) -> int:
        """Feed the samples of `channels` to `sink` (begin/feed/end); return the count."""
        sink.begin(self.meta(channels))
        n = 0
        mask = np.uint16(sum(1 << c.index for c in channels))
        for block in self.blocks():
            if limit is not None and n + len(block) > limit:
                block = block[:limit - n]
            if len(block):
                sink.feed(block & mask)
                n += len(block)
            if limit is not None and n >= limit:
                break
        sink.end()
        return n

    def blocks(self) -> Iterator[np.ndarray]:
        """All samples as uint16 arrays; entry boundaries need not align with samples."""
        size = self.unitsize
        rest = b""
        with zipfile.ZipFile(self.path) as zf:
            for name in self.entries:
                with zf.open(name) as f:
                    while True:
                        data = f.read(READ_SAMPLES * size)
                        if not data:
                            break
                        data = rest + data
                        cut = len(data) // size * size
                        rest = data[cut:]
                        if cut:
                            yield _to_u16(data[:cut], size)
        # A trailing partial sample is dropped, as session_driver.c does.


def _to_u16(data: bytes, unitsize: int) -> np.ndarray:
    raw = np.frombuffer(data, dtype=np.uint8).reshape(-1, unitsize)
    out = raw[:, 0].astype(np.uint16)
    if unitsize > 1:
        out |= raw[:, 1].astype(np.uint16) << np.uint16(8)
    return out


def open_srzip(path: str) -> SrzipFile:
    try:
        zf = zipfile.ZipFile(path)
    except FileNotFoundError:
        raise InputError("input file '%s' not found" % path) from None
    except (zipfile.BadZipFile, IsADirectoryError, PermissionError) as e:
        raise InputError("'%s' is not a sigrok session file (%s)" % (path, e)) from None
    with zf:
        names = set(zf.namelist())
        if "version" not in names or "metadata" not in names:
            raise InputError("'%s' is not a sigrok session file: no version/metadata" % path)
        version = zf.read("version").decode("ascii", "replace").strip()
        if version not in ("1", "2"):
            raise InputError("cannot handle sigrok session file version %r" % version)
        groups = parse_keyfile(zf.read("metadata").decode("utf-8", "replace"))
        dev = groups.get("device 1")
        if dev is None:
            raise InputError("session file has no [device 1] section")
        capturefile = dev.get("capturefile")
        if not capturefile:
            raise InputError("session file has no logic data (analog-only files are not supported)")
        if "samplerate" not in dev:
            raise InputError("session file has no samplerate")
        samplerate = parse_size(dev["samplerate"])
        try:
            total = int(dev.get("total probes", "0"))
            unitsize = int(dev.get("unitsize", str(max(1, (total + 7) // 8))))
        except ValueError:
            raise InputError("invalid 'total probes' or 'unitsize' in metadata") from None
        if total <= 0 or unitsize <= 0:
            raise InputError("invalid 'total probes' or 'unitsize' in metadata")

        channels = []
        for key, value in dev.items():
            m = re.fullmatch(r"probe(\d+)", key)
            if not m:
                continue
            num = int(m.group(1))
            if not 1 <= num <= total:
                raise InputError("metadata names %s but the file has %d channels" % (key, total))
            if num > MAX_CHANNELS:
                raise InputError("channel %s (probe %d) is beyond the %d channels kingst-la handles"
                                 % (value, num, MAX_CHANNELS))
            channels.append(Channel(num - 1, value))
        channels.sort(key=lambda c: c.index)
        if not channels:
            raise InputError("session file has no enabled logic channels")

        if capturefile in names:
            entries = [capturefile]
        else:
            entries = []
            while "%s-%d" % (capturefile, len(entries) + 1) in names:
                entries.append("%s-%d" % (capturefile, len(entries) + 1))
    return SrzipFile(path, samplerate, total, unitsize, channels, entries)
