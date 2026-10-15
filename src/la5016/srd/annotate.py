"""sigrok-cli style decoder output: annotation lines, -A/-B selection, -L, --show -P.

Follows sigrok-cli decode.c (show_pd_annotations, show_pd_binary,
setup_pd_annotations, setup_pd_binary) and show.c. GPL-3.0-or-later.
"""

from __future__ import annotations

import re
from typing import BinaryIO, Dict, List, Optional, Sequence, TextIO

from la5016.srd.loader import DecoderInfo, LoadError
from la5016.srd.session import Callbacks, Instance

ALL = -1


def _canon(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def parse_annotations(text: Optional[str], infos: Dict[str, DecoderInfo]) -> Dict[str, List[int]]:
    """-A value -> {decoder id: [class index or ALL]}. None means every loaded decoder, all classes."""
    if text is None:
        return {i: [ALL] for i in infos}
    out: Dict[str, List[int]] = {}
    for tok in text.split(","):
        if not tok:
            break
        dec_id, _, names = tok.partition("=")
        info = infos.get(dec_id)
        if info is None:
            raise LoadError("Protocol decoder '%s' not found." % dec_id)
        if not info.annotations:
            raise LoadError("Protocol decoder '%s' has no annotations." % dec_id)
        sel = out.setdefault(dec_id, [])
        if not names:
            sel.append(ALL)
            continue
        for name in names.split(":"):
            if not name:
                break
            idx = next((i for i, a in enumerate(info.annotations) if _canon(a[0]) == _canon(name)), None)
            if idx is not None:
                sel.append(idx)
                continue
            row = next((r for r in info.annotation_rows if _canon(r[0]) == _canon(name)), None)
            if row is None:
                raise LoadError("Annotation '%s' not found for protocol decoder '%s'." % (name, dec_id))
            sel.extend(row[2])
    return out


def parse_binary(text: str, infos: Dict[str, DecoderInfo]) -> Dict[str, int]:
    """-B value -> {decoder id: binary class index or ALL}; a later entry for the same decoder wins."""
    out: Dict[str, int] = {}
    for tok in text.split(","):
        if not tok:
            break
        dec_id, sep, cls = tok.partition("=")
        info = infos.get(dec_id)
        if info is None:
            raise LoadError("Protocol decoder '%s' not found." % dec_id)
        if not info.binary:
            raise LoadError("Protocol decoder '%s' has no binary output." % dec_id)
        if sep:
            idx = next((i for i, b in enumerate(info.binary) if b[0] == cls), None)
            if idx is None:
                raise LoadError("binary output '%s' not found for protocol decoder '%s'." % (cls, dec_id))
            out[dec_id] = idx
        else:
            out[dec_id] = ALL
    return out


class AnnotationPrinter(Callbacks):
    def __init__(self, out: TextIO, visible: Dict[str, List[int]], samplenum: bool = False, loglevel: int = 2):
        self.out = out
        self.visible = visible
        self.show_snum = samplenum or loglevel > 2
        self.quote = '"' if loglevel > 2 else ""
        self.show_class = self.show_abbrev = loglevel > 3

    def annotation(self, inst: Instance, proto_id, ss, es, cls, texts):
        sel = self.visible.get(inst.info.id)
        if sel is None or (ALL not in sel and cls not in sel):
            return
        q = self.quote
        parts = []
        if self.show_snum:
            parts.append("%d-%d " % (ss, es))
        parts.append("%s: " % proto_id)
        if self.show_class:
            parts.append("%s: " % inst.info.annotations[cls][0])
        parts.append("%s%s%s" % (q, texts[0], q))
        if self.show_abbrev:
            parts.extend(" %s%s%s" % (q, t, q) for t in texts[1:])
        self.out.write("".join(parts) + "\n")
        self.out.flush()


class BinaryWriter(Callbacks):
    def __init__(self, out: BinaryIO, visible: Dict[str, int]):
        self.out = out
        self.visible = visible

    def binary(self, inst: Instance, proto_id, ss, es, cls, data):
        sel = self.visible.get(inst.info.id)
        if sel is None or (sel != ALL and sel != cls):
            return
        self.out.write(data)
        self.out.flush()


def _gvariant(v) -> str:
    if isinstance(v, str):
        return "'%s'" % v.replace("\\", "\\\\").replace("'", "\\'")
    if isinstance(v, bool):
        return str(int(v))
    return repr(v)


def list_text(infos: Sequence[DecoderInfo], loglevel: int = 2) -> str:
    lines = ["Supported protocol decoders:"]
    for info in sorted(infos, key=lambda i: i.id):
        lines.append("  %-20s %s" % (info.id, info.longname))
        if loglevel >= 3:
            lines.append("  %-20s %s" % ("", info.desc))
    return "\n".join(lines) + "\n\n"


def show_text(info: DecoderInfo) -> str:
    out = ["ID: %s" % info.id, "Name: %s" % info.name, "Long name: %s" % info.longname,
           "Description: %s" % info.desc, "License: %s" % info.license]

    def section(title, items):
        out.append(title)
        out.extend(items or ["None."])

    section("Possible decoder input IDs:", ["- %s" % s for s in info.inputs])
    section("Possible decoder output IDs:", ["- %s" % s for s in info.outputs])
    section("Decoder tags:", ["- %s" % s for s in info.tags])
    section("Annotation classes:", ["- %s: %s" % (a[0], a[1]) for a in info.annotations])
    section("Annotation rows:", ["- %s (%s): %s" % (r[0], r[1], ", ".join(info.annotations[i][0] for i in r[2]))
                                 for r in info.annotation_rows])
    section("Binary classes:", ["- %s: %s" % (b[0], b[1]) for b in info.binary])
    section("Required channels:", ["- %s (%s): %s" % (c["id"], c["name"], c["desc"]) for c in info.channels])
    section("Optional channels:", ["- %s (%s): %s" % (c["id"], c["name"], c["desc"])
                                   for c in info.optional_channels])
    section("Options:", ["- %s: %s (%sdefault %s)" % (o["id"], o.get("desc", ""),
                                                      "".join(_gvariant(v) + ", " for v in o.get("values", ())),
                                                      _gvariant(o.get("default")))
                         for o in info.options])
    text = "\n".join(out) + "\n"
    if info.doc:
        doc = info.doc[1:] if info.doc.startswith("\n") else info.doc
        text += "Documentation:\n%s\n" % doc
    return text
