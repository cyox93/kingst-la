"""Load libsigrokdecode protocol decoders and build decoder stacks.

Follows libsigrokdecode decoder.c (metadata checks) and sigrok-cli decode.c /
parsers.c (option conversion, -P syntax). GPL-3.0-or-later.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from kingst_la import firmware
from kingst_la.pipeline import Channel
from kingst_la.srd import api
from kingst_la.srd.session import Callbacks, Instance


class LoadError(Exception):
    pass


def decoders_dir() -> Path:
    env = os.environ.get("KINGST_LA_DECODERS_DIR")
    return Path(env) if env else firmware.firmware_dir().parent / "decoders"


@dataclass
class DecoderInfo:
    cls: type
    id: str
    name: str
    longname: str
    desc: str
    license: str
    inputs: List[str]
    outputs: List[str]
    tags: List[str]
    channels: Tuple[dict, ...]
    optional_channels: Tuple[dict, ...]
    options: Tuple[dict, ...]
    annotations: Tuple[tuple, ...]
    annotation_rows: Tuple[tuple, ...]
    binary: Tuple[tuple, ...]
    doc: str = ""

    @property
    def all_channels(self) -> Tuple[dict, ...]:
        return tuple(self.channels) + tuple(self.optional_channels)


_loaded: Dict[str, DecoderInfo] = {}


def _prepare(path: Path) -> None:
    sys.modules["sigrokdecode"] = api
    p = str(path)
    if p not in sys.path:
        sys.path.append(p)  # for "from common.srdhelper import ...", without shadowing stdlib


def available(path: Optional[Path] = None) -> List[str]:
    path = path or decoders_dir()
    if not path.is_dir():
        return []
    return sorted(d.name for d in path.iterdir()
                  if (d / "__init__.py").is_file() and (d / "pd.py").is_file() and not d.name.startswith((".", "_")))


def load(name: str, path: Optional[Path] = None) -> DecoderInfo:
    if name in _loaded:
        return _loaded[name]
    path = path or decoders_dir()
    pkg = path / name
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name) or not (pkg / "__init__.py").is_file():
        hint = "" if path.is_dir() else "; run 'kla --install-decoders' first"
        raise LoadError("protocol decoder '%s' not found in %s%s" % (name, path, hint))
    _prepare(path)
    spec = importlib.util.spec_from_file_location(name, pkg / "__init__.py", submodule_search_locations=[str(pkg)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        sys.modules.pop(name, None)
        raise LoadError("cannot load protocol decoder '%s': %s" % (name, e)) from e
    info = _validate(name, module)
    _loaded[name] = info
    return info


def _validate(name: str, module) -> DecoderInfo:
    cls = getattr(module, "Decoder", None)
    if not isinstance(cls, type) or not issubclass(cls, api.Decoder):
        raise LoadError("%s: Decoder class not found or not a sigrokdecode.Decoder" % name)
    if getattr(cls, "api_version", None) != 3:
        raise LoadError("%s: only api_version 3 is supported" % name)
    for attr in ("reset", "start", "decode"):
        if not callable(getattr(cls, attr, None)):
            raise LoadError("%s: missing method %s()" % (name, attr))
    strings = {}
    for attr in ("id", "name", "longname", "desc", "license"):
        v = getattr(cls, attr, None)
        if not isinstance(v, str):
            raise LoadError("%s: attribute '%s' must be a string" % (name, attr))
        strings[attr] = v
    lists = {}
    for attr in ("inputs", "outputs", "tags"):
        v = getattr(cls, attr, None)
        if not isinstance(v, list) or not all(isinstance(s, str) for s in v):
            raise LoadError("%s: attribute '%s' must be a list of strings" % (name, attr))
        lists[attr] = v
    channels = tuple(getattr(cls, "channels", ()))
    optional = tuple(getattr(cls, "optional_channels", ()))
    ids = [c["id"] for c in channels + optional]
    if len(set(ids)) != len(ids):
        raise LoadError("%s: duplicate channel id" % name)
    options = tuple(getattr(cls, "options", ()))
    for o in options:
        if "id" not in o:
            raise LoadError("%s: option without id" % name)
        if "values" in o and ("default" not in o or any(type(v) is not type(o["default"]) for v in o["values"])):
            raise LoadError("%s: option '%s' values do not match its default" % (name, o["id"]))
    annotations = tuple(getattr(cls, "annotations", ()))
    rows = tuple(getattr(cls, "annotation_rows", ()))
    for row in rows:
        if any(not 0 <= i < len(annotations) for i in row[2]):
            raise LoadError("%s: annotation row '%s' refers to a missing class" % (name, row[0]))
    return DecoderInfo(cls=cls, channels=channels, optional_channels=optional, options=options,
                       annotations=annotations, annotation_rows=rows,
                       binary=tuple(getattr(cls, "binary", ())),
                       doc=(module.__doc__ or ""), **strings, **lists)


# --- options -------------------------------------------------------------


def _strtoll0(text: str) -> int:
    """C strtoll(s, &end, 0) requiring the whole string: 0x.. hex, 0.. octal."""
    t = text.strip()
    m = re.fullmatch(r"([+-]?)(0[xX][0-9a-fA-F]+|0[0-7]*|[1-9][0-9]*)", t)
    if not m:
        raise ValueError(text)
    sign, digits = m.groups()
    if digits[:2].lower() == "0x":
        v = int(digits, 16)
    elif digits.startswith("0"):
        v = int(digits, 8)
    else:
        v = int(digits)
    return -v if sign == "-" else v


def _default(o: dict):
    d = o.get("default")
    return int(d) if isinstance(d, bool) else d  # bool travels as int64 in libsigrokdecode


def convert_options(info: DecoderInfo, given: Dict[str, str]) -> Dict:
    known = {o["id"]: o for o in info.options}
    out = {oid: _default(o) for oid, o in known.items()}
    for key, text in given.items():
        if key not in known:
            raise LoadError("Unknown option or channel '%s' for decoder %s" % (key, info.id))
        d = _default(known[key])
        try:
            if isinstance(d, int):
                out[key] = _strtoll0(text)
            elif isinstance(d, float):
                out[key] = float(text)
            else:
                out[key] = text
        except ValueError:
            raise LoadError("Option '%s' of decoder %s requires a number" % (key, info.id)) from None
    return out


# --- -P parsing and stacks -------------------------------------------------


@dataclass
class DecoderSpec:
    name: str
    args: Dict[str, str] = field(default_factory=dict)


def parse_pd(text: str) -> List[DecoderSpec]:
    """'uart:rx=CH0:baudrate=9600,mydec' -> bottom-first stack specs."""
    stack = []
    for token in text.split(","):
        parts = token.split(":")
        name = parts[0].strip()
        if not name:
            raise LoadError("empty protocol decoder name in '%s'" % text)
        args = {}
        for kv in parts[1:]:
            key, sep, value = kv.partition("=")
            if not sep or not key:
                raise LoadError("invalid protocol decoder option '%s'" % kv)
            args[key] = value
        stack.append(DecoderSpec(name, args))
    return stack


def _channel_lookup(channels: Sequence[Channel]) -> Dict[str, int]:
    names = {"CH%d" % c.index: c.index for c in channels}
    names.update({c.name: c.index for c in channels})
    return names


def build_stacks(pd_args: List[str], channels: Sequence[Channel], callbacks: Callbacks,
                 path: Optional[Path] = None) -> List[Instance]:
    """Instantiate every -P stack. Only the bottom decoder of a stack gets channels."""
    counters: Dict[str, int] = {}
    roots = []
    lookup = _channel_lookup(channels)
    for text in pd_args:
        prior = None
        for pos, spec in enumerate(parse_pd(text)):
            info = load(spec.name, path)
            chan_ids = [c["id"] for c in info.all_channels]
            opts = {k: v for k, v in spec.args.items() if k not in chan_ids}
            chmap_given = {k: v for k, v in spec.args.items() if k in chan_ids}
            if pos > 0 and chmap_given:
                raise LoadError("stacked decoder %s cannot take channels" % spec.name)
            options = convert_options(info, opts)
            if chmap_given:
                cmap = []
                for c in info.all_channels:
                    target = chmap_given.get(c["id"])
                    if target is None:
                        cmap.append(-1)
                    elif target in lookup:
                        cmap.append(lookup[target])
                    else:
                        raise LoadError("channel '%s' for %s:%s is not an enabled channel" % (target, info.id, c["id"]))
                missing = [c["id"] for c, b in zip(info.channels, cmap) if b < 0]
                if missing:
                    raise LoadError("decoder %s needs channel(s): %s" % (info.id, ", ".join(missing)))
            else:
                cmap = list(range(len(info.all_channels)))  # identity, as in libsigrokdecode
            n = counters.get(info.id, 0) + 1
            counters[info.id] = n
            inst = Instance(info, "%s-%d" % (info.id, n), options, cmap, callbacks)
            if prior is None:
                roots.append(inst)
            else:
                prior.next.append(inst)
            prior = inst
    return roots
