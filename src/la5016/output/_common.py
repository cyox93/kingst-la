"""Helpers shared by the output formats.

Ports sr_si_string_u64(), sr_samplerate_string() and sr_period_string()
from libsigrok src/strutil.c, and the option conversion of sigrok-cli
parsers.c:generic_arg_to_opt().
"""

from __future__ import annotations

import time
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from .. import __version__

# Driver longname of libsigrok's kingst-la2016 driver (CSV header).
DRIVER_LONGNAME = "Kingst LA2016"
# Replaces "libsigrok <version>" wherever libsigrok prints its version.
PACKAGE_VERSION = f"la5016 {__version__}"

FLT_MIN = 1.17549435082228750797e-38


def ctime_now() -> str:
    """C ctime() of the current local time, without the trailing newline."""
    return time.ctime()


def si_string_u64(x: int, unit: str) -> str:
    divisors = [1, 10**3, 10**6, 10**9, 10**12, 10**15, 10**18]
    i = 0
    while i < len(divisors) - 1 and x // divisors[i] >= 1000:
        i += 1
    quot = x // divisors[i]
    fract = ""
    if i:
        fract = ".%0*d" % (i * 3, x % divisors[i])
        fract = fract.rstrip("0.")
    prefix = "kMGTPE"[i - 1] if i else ""
    return f"{quot}{fract} {prefix}{unit}"


def samplerate_string(samplerate: int) -> str:
    return si_string_u64(samplerate, "Hz")


def period_string(v_p: int, v_q: int) -> str:
    freq = 1 / (v_p / v_q)
    if freq > 1e9:
        v, unit = v_p / v_q * 1000000000000.0, "ps"
    elif freq > 1e6:
        v, unit = v_p / v_q * 1000000000.0, "ns"
    elif freq > 1e3:
        v, unit = v_p / v_q * 1000000.0, "us"
    elif freq > 1:
        v, unit = v_p / v_q * 1000.0, "ms"
    else:
        v, unit = v_p / v_q, "s"
    prec = 0 if (v - int(v)) < FLT_MIN else 3
    return "%.*f %s" % (prec, v, unit)


# ---- options -------------------------------------------------------------

# spec: option name -> (type, default); type is "uint32", "bool", "str"
# or a tuple of allowed strings.
OptionSpec = Dict[str, Tuple[object, object]]

_TRUE = ("", "true", "yes", "on", "1")
_FALSE = ("false", "no", "off", "0")


def parse_options(fmt: str, spec: OptionSpec,
                  options: Optional[Mapping[str, object]]) -> Dict[str, object]:
    """Fill in defaults and convert string values, like sr_output_new()."""
    options = dict(options or {})
    for key in options:
        if key not in spec:
            accepted = ", ".join(spec) if spec else "(none)"
            raise ValueError(f"output format '{fmt}' has no option '{key}' "
                             f"(accepted: {accepted})")
    result = {k: default for k, (_, default) in spec.items()}
    for key, raw in options.items():
        typ = spec[key][0]
        result[key] = _convert(fmt, key, typ, raw)
    return result


def _convert(fmt: str, key: str, typ: object, raw: object) -> object:
    bad = ValueError(f"invalid value {raw!r} for option '{key}' of output format '{fmt}'")
    if typ == "uint32":
        if isinstance(raw, bool):
            raise bad
        try:
            val = int(raw)  # type: ignore[call-overload]
        except (TypeError, ValueError):
            raise bad from None
        if not 0 <= val < 2**32:
            raise bad
        return val
    if typ == "bool":
        if isinstance(raw, bool):
            return raw
        s = str(raw).strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
        raise bad
    if isinstance(typ, tuple):
        if raw not in typ:
            raise ValueError(f"invalid value {raw!r} for option '{key}' of output "
                             f"format '{fmt}' (one of: {', '.join(typ)})")
        return raw
    return str(raw)


# ---- vectorized text assembly ---------------------------------------------

Block = Tuple[np.ndarray, Optional[np.ndarray]]


def const_block(n: int, text: bytes) -> Block:
    arr = np.frombuffer(text, dtype=np.uint8)
    return np.broadcast_to(arr, (n, arr.size)), None


def uint_block(values: np.ndarray) -> Block:
    """Decimal digits of unsigned ints, one row each, leading zeros masked."""
    v = np.asarray(values, dtype=np.uint64)
    n = v.size
    width = len(str(int(v.max()))) if n else 1
    digits = np.empty((n, width), dtype=np.uint8)
    rest = v.copy()
    for col in range(width - 1, -1, -1):
        digits[:, col] = (rest % 10).astype(np.uint8) + ord("0")
        rest //= 10
    ndig = np.ones(n, dtype=np.int64)
    for k in range(1, width):
        ndig += v >= np.uint64(10**k)
    keep = np.arange(width)[None, :] >= (width - ndig)[:, None]
    return digits, keep


def assemble(blocks: Sequence[Block]) -> bytes:
    """Concatenate per-row column blocks, dropping masked bytes, row-major."""
    if not blocks:
        return b""
    n = blocks[0][0].shape[0]
    if n == 0:
        return b""
    arrays: List[np.ndarray] = []
    masks: List[np.ndarray] = []
    any_mask = False
    for arr, mask in blocks:
        arrays.append(arr)
        if mask is None:
            masks.append(np.ones(arr.shape, dtype=bool))
        else:
            any_mask = True
            masks.append(np.broadcast_to(mask, arr.shape))
    mat = np.concatenate(arrays, axis=1)
    if not any_mask:
        return np.ascontiguousarray(mat).tobytes()
    return mat[np.concatenate(masks, axis=1)].tobytes()


def channel_bits(samples: np.ndarray, index: int) -> np.ndarray:
    """0/1 uint8 array of bit `index` of each sample."""
    return ((samples >> np.uint16(index)) & np.uint16(1)).astype(np.uint8)


def as_samples(samples: np.ndarray) -> np.ndarray:
    return np.ascontiguousarray(samples, dtype=np.uint16).reshape(-1)
