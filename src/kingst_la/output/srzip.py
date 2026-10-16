"""srzip output: the sigrok session file (.sr) that PulseView opens.

Ported from libsigrok src/output/srzip.c. The archive holds "version"
("2"), "metadata" (a GKeyFile) and the logic data in "logic-1-1",
"logic-1-2", ... chunks of up to 4 MiB. A sample keeps all device bits
(bit i is CHi; unitsize 2 for the 16-channel LA5016) and the metadata lists
the enabled channels as probe<index+1>, so files match those of libsigrok's
kingst-la2016 driver.
"""

from __future__ import annotations

import zipfile
from typing import BinaryIO, List, Mapping, Optional, Tuple

import numpy as np

from ..pipeline import Meta
from ._common import PACKAGE_VERSION, as_samples, parse_options, samplerate_string

CHUNK_SIZE = 4 * 1024 * 1024   # bytes per logic-1-N entry, as libsigrok buffers them


def keyfile_escape(value: str) -> str:
    """Escape a value as g_key_file_set_string() does."""
    out = []
    for i, ch in enumerate(value):
        if ch == " " and i == 0:
            out.append("\\s")
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        else:
            out.append(ch)
    return "".join(out)


def keyfile_text(groups: List[Tuple[str, List[Tuple[str, str]]]]) -> str:
    """Serialize like g_key_file_to_data(): groups separated by a blank line."""
    parts = []
    for name, entries in groups:
        lines = ["[%s]" % name] + ["%s=%s" % (k, keyfile_escape(v)) for k, v in entries]
        parts.append("\n".join(lines) + "\n")
    return "\n".join(parts)


class SrzipOutput:
    NAME = "srzip"

    def __init__(self, out: BinaryIO, options: Optional[Mapping[str, object]] = None):
        parse_options(self.NAME, {}, options)
        seekable = getattr(out, "seekable", None)
        if not (seekable and seekable()):
            # libsigrok: "srzip output module requires a file name, cannot save."
            raise ValueError("output format 'srzip' needs a file (-o FILE)")
        self.out = out
        self.zip: Optional[zipfile.ZipFile] = None
        self.meta: Optional[Meta] = None
        self.mask = np.uint16(0)
        self.buf: List[bytes] = []
        self.fill = 0
        self.chunks = 0
        self.samples = 0

    def begin(self, meta: Meta) -> None:
        self.meta = meta
        self.mask = np.uint16(sum(1 << c.index for c in meta.channels))
        self.unitsize = (meta.total_channels + 7) // 8
        self.dtype = "<u2" if self.unitsize == 2 else "u1"
        self.zip = zipfile.ZipFile(self.out, "w", zipfile.ZIP_DEFLATED)
        self.zip.writestr("version", "2")

    def feed(self, samples: np.ndarray) -> None:
        data = (as_samples(samples) & self.mask).astype(self.dtype, copy=False)
        if not data.size:
            return
        self.samples += data.size
        raw = data.tobytes()
        while raw:
            take = (CHUNK_SIZE - self.fill) // self.unitsize * self.unitsize
            self.buf.append(raw[:take])
            self.fill += len(raw[:take])
            raw = raw[take:]
            if CHUNK_SIZE - self.fill < self.unitsize:
                self._flush()

    def _flush(self) -> None:
        if not self.fill:
            return
        self.chunks += 1
        self.zip.writestr("logic-1-%d" % self.chunks, b"".join(self.buf))
        self.buf, self.fill = [], 0

    def trigger(self) -> None:
        pass   # srzip has no trigger marker

    def end(self) -> None:
        self._flush()
        dev = [("capturefile", "logic-1"),
               ("total probes", str(self.meta.total_channels)),
               ("samplerate", samplerate_string(self.meta.samplerate)),
               ("total analog", "0")]
        dev += [("probe%d" % (c.index + 1), c.name) for c in sorted(self.meta.channels, key=lambda c: c.index)]
        if self.samples:
            dev.append(("unitsize", str(self.unitsize)))   # libsigrok adds it with the first data
        text = keyfile_text([("global", [("sigrok version", PACKAGE_VERSION)]), ("device 1", dev)])
        self.zip.writestr("metadata", text)
        self.zip.close()
        self.zip = None

    def abort(self) -> None:
        if self.zip is not None:
            try:
                self.zip.close()
            except (OSError, ValueError):
                pass
            self.zip = None
