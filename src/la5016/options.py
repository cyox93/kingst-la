"""Pure parsers for the sigrok-cli compatible command line.

Every function takes the raw option text and either returns a typed value or
raises UsageError with a message fit for stderr. Nothing here touches the device.
"""

from __future__ import annotations

import os
import re
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from .pipeline import Channel


class UsageError(ValueError):
    """Invalid command line input; the CLI prints it and exits with 1."""


DRIVER = "kingst-la2016"
OUTPUT_FORMATS = ("bits", "hex", "csv", "vcd", "binary")
_EXT_FORMATS = {".vcd": "vcd", ".csv": "csv", ".bin": "binary"}

THRESHOLDS = (0.4, 0.6, 0.9, 1.2, 1.4, 2.0, 2.5, 4.0)  # volts, hardware presets
DEFAULT_THRESHOLD = 1.4

BASECLOCK = 800_000_000
MAX_SAMPLERATE = 500_000_000
MIN_SAMPLERATE = -(-BASECLOCK // 0xFFFF)  # smallest rate whose divider fits the 16-bit register

# The rates listed by --show, as in sigrok's rates_500mhz table. Any rate
# between MIN_SAMPLERATE and MAX_SAMPLERATE is accepted and rounded to what
# the hardware can do (see hardware_divider()).
SAMPLERATE_LIST = tuple(int(r) for r in (
    20e3, 50e3, 100e3, 200e3, 500e3,
    1e6, 2e6, 5e6, 10e6, 20e6, 50e6,
    100e6, 200e6, 500e6,
))
STREAM_MAX_SAMPLERATE = 100_000_000

CONFIG_KEYS = {"samplerate", "captureratio", "voltage_threshold", "continuous"}
PWM_GROUPS = ("PWM1", "PWM2")
PWM_KEYS = {"enabled", "output_frequency", "duty_cycle"}

TRIGGER_CONDITIONS = ("0", "1", "r", "f")

_SI_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([kKmMgG]?)\s*(?:[hH][zZ])?")
_SI_MULT = {"": 1, "k": 10**3, "K": 10**3, "m": 10**6, "M": 10**6, "g": 10**9, "G": 10**9}
_TIME_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(ms|s)?")
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")
_CH_RE = re.compile(r"CH(\d+)")


def _integral(value: Decimal, text: str, what: str) -> int:
    if value != value.to_integral_value():
        raise UsageError(f"invalid {what} '{text}': not a whole number")
    return int(value)


def parse_si(text: str) -> int:
    """"100", "1k", "1.5m", "1 MHz" -> integer (k=1e3, m/M=1e6, g/G=1e9)."""
    m = _SI_RE.fullmatch(text.strip())
    if not m:
        raise UsageError(f"invalid size '{text}': expected a number with optional k/m/g suffix")
    return _integral(Decimal(m.group(1)) * _SI_MULT[m.group(2)], text, "size")


def parse_time_ms(text: str) -> int:
    """"100", "100ms", "1.5s" -> milliseconds (> 0); a bare number is ms."""
    m = _TIME_RE.fullmatch(text.strip())
    if not m:
        raise UsageError(f"invalid time '{text}': expected e.g. 100, 100ms or 1.5s")
    scale = 1000 if m.group(2) == "s" else 1
    ms = _integral(Decimal(m.group(1)) * scale, text, "time (in ms)")
    if ms <= 0:
        raise UsageError(f"invalid time '{text}': must be greater than 0")
    return ms


def parse_kv(text: str) -> Dict[str, str]:
    """"a=1:b=2" -> {"a": "1", "b": "2"}; "" -> {}. Every item needs '='."""
    out: Dict[str, str] = {}
    if text == "":
        return out
    for item in text.split(":"):
        key, sep, value = item.partition("=")
        key = key.strip()
        if not sep or not key:
            raise UsageError(f"invalid option '{item}' in '{text}': expected key=value")
        if key in out:
            raise UsageError(f"option '{key}' given twice in '{text}'")
        out[key] = value.strip()
    return out


def parse_driver(text: str) -> Tuple[str, Dict[str, str]]:
    """"kingst-la2016[:conn=1.5]" -> (name, options)."""
    name, _, rest = text.partition(":")
    name = name.strip()
    if name != DRIVER:
        raise UsageError(f"unsupported driver '{name}': only '{DRIVER}' is supported")
    return name, parse_kv(rest)


def parse_conn(text: str) -> Tuple[int, int]:
    """"1.5" -> (bus, address)."""
    m = re.fullmatch(r"(\d+)\.(\d+)", text.strip())
    if not m:
        raise UsageError(f"invalid conn '{text}': expected <bus>.<address>, e.g. 1.5")
    return int(m.group(1)), int(m.group(2))


def _ch_index(text: str, total: int) -> int:
    m = _CH_RE.fullmatch(text.strip())
    if not m:
        raise UsageError(f"invalid channel '{text}': expected CH0..CH{total - 1}")
    idx = int(m.group(1))
    if idx >= total:
        raise UsageError(f"channel '{text}' out of range: expected CH0..CH{total - 1}")
    return idx


def parse_channels(text: Optional[str], total: int = 16) -> List[Channel]:
    """-C value -> enabled channels in user order. None means all channels.

    Items: "CHn", "CHa-CHb" (inclusive), "CHn=NAME".
    """
    if text is None:
        return [Channel(i, f"CH{i}") for i in range(total)]
    if not text.strip():
        raise UsageError("empty channel list")
    chans: List[Channel] = []
    for item in text.split(","):
        item = item.strip()
        if not item:
            raise UsageError(f"empty item in channel list '{text}'")
        spec, sep, name = item.partition("=")
        if "-" in spec:
            if sep:
                raise UsageError(f"cannot rename a channel range: '{item}'")
            a_text, _, b_text = spec.partition("-")
            a, b = _ch_index(a_text, total), _ch_index(b_text, total)
            if a > b:
                raise UsageError(f"invalid channel range '{item}': start is after end")
            chans.extend(Channel(i, f"CH{i}") for i in range(a, b + 1))
        else:
            idx = _ch_index(spec, total)
            name = name.strip()
            if sep and not name:
                raise UsageError(f"empty channel name in '{item}'")
            chans.append(Channel(idx, name if sep else f"CH{idx}"))
    seen_idx, seen_name = set(), set()
    for c in chans:
        if c.index in seen_idx:
            raise UsageError(f"channel CH{c.index} given more than once")
        if c.name in seen_name:
            raise UsageError(f"channel name '{c.name}' used more than once")
        seen_idx.add(c.index)
        seen_name.add(c.name)
    return chans


def parse_triggers(text: str, channels: List[Channel]) -> Dict[int, str]:
    """"CH0=r,CH1=1" -> {0: "r", 1: "1"} over enabled channels.

    A channel is named by its current name or its original "CHn".
    The hardware supports levels plus at most one rising/falling edge.
    """
    by_name = {f"CH{c.index}": c.index for c in channels}
    by_name.update({c.name: c.index for c in channels})  # current names win
    out: Dict[int, str] = {}
    if not text.strip():
        raise UsageError("empty trigger list")
    for item in text.split(","):
        name, sep, cond = (s.strip() for s in item.partition("="))
        if not name or not sep:
            raise UsageError(f"invalid trigger '{item}': expected <channel>=<0|1|r|f>")
        if name not in by_name:
            raise UsageError(f"trigger channel '{name}' is not an enabled channel")
        if cond == "e":
            raise UsageError("the device does not support any-edge triggers ('e')")
        if cond not in TRIGGER_CONDITIONS:
            raise UsageError(f"invalid trigger condition '{cond}' for {name}: expected 0, 1, r or f")
        idx = by_name[name]
        if idx in out:
            raise UsageError(f"trigger for channel '{name}' given more than once")
        out[idx] = cond
    if sum(1 for c in out.values() if c in "rf") > 1:
        raise UsageError("the device supports only one edge trigger (r/f)")
    return out


def parse_output_format(text: str) -> Tuple[str, Dict[str, str]]:
    """"csv:time=true" -> ("csv", {"time": "true"})."""
    name, _, rest = text.partition(":")
    name = name.strip()
    if name not in OUTPUT_FORMATS:
        raise UsageError(
            f"unsupported output format '{name}': expected one of {', '.join(OUTPUT_FORMATS)}")
    return name, parse_kv(rest)


def format_from_filename(path: str) -> str:
    """Guess the output format from the -o extension."""
    ext = os.path.splitext(path)[1].lower()
    if ext not in _EXT_FORMATS:
        raise UsageError(
            f"cannot guess output format from '{path}': pass -O "
            f"({', '.join(OUTPUT_FORMATS)})")
    return _EXT_FORMATS[ext]


def parse_threshold(text: str) -> float:
    """"1.4-1.4" or "1.4" -> one of THRESHOLDS."""
    parts = text.strip().split("-")
    if len(parts) > 2 or not all(_NUM_RE.fullmatch(p.strip()) for p in parts):
        raise UsageError(f"invalid voltage_threshold '{text}': expected x-x, e.g. 1.4-1.4")
    lo, hi = float(parts[0]), float(parts[-1])
    if abs(lo - hi) > 1e-6:
        raise UsageError(f"invalid voltage_threshold '{text}': low and high must be equal")
    for t in THRESHOLDS:
        if abs(lo - t) <= 1e-6:
            return t
    raise UsageError(
        f"unsupported voltage_threshold '{text}': expected one of "
        f"{', '.join(str(t) for t in THRESHOLDS)}")


def hardware_divider(divider: int) -> int:
    """The divider the LA5016 actually applies.

    Measured on hardware (2026-10-16): 1 gives 500 MHz, 2..7 all give
    200 MHz (divider 4), and larger values are rounded down to a multiple
    of 8. libsigrok assumes any divider works.
    """
    if divider <= 1:
        return 1
    if divider < 8:
        return 4
    return divider & ~7


def samplerate_divider(requested: int) -> int:
    """Divider of BASECLOCK the hardware uses for the requested rate."""
    if not MIN_SAMPLERATE <= requested <= MAX_SAMPLERATE:
        raise UsageError(
            f"samplerate {requested} Hz out of range: "
            f"{MIN_SAMPLERATE} Hz .. {MAX_SAMPLERATE} Hz")
    return hardware_divider(BASECLOCK // requested)


def effective_samplerate(requested: int) -> int:
    """Rate the hardware actually runs at for the requested rate."""
    return min(BASECLOCK // samplerate_divider(requested), MAX_SAMPLERATE)


def parse_bool(text: str, what: str = "value") -> bool:
    t = text.strip().lower()
    if t in ("on", "true", "yes", "1"):
        return True
    if t in ("off", "false", "no", "0"):
        return False
    raise UsageError(f"invalid {what} '{text}': expected on or off")


def parse_captureratio(text: str) -> int:
    """Pre-trigger percentage, 0..100."""
    t = text.strip()
    if not t.isdigit() or int(t) > 100:
        raise UsageError(f"invalid captureratio '{text}': expected an integer 0..100")
    return int(t)


def parse_samples(text: str) -> int:
    """--samples value, e.g. "1m"; must be > 0."""
    n = parse_si(text)
    if n <= 0:
        raise UsageError(f"invalid sample count '{text}': must be greater than 0")
    return n


def parse_config(text_list: List[str]) -> dict:
    """Merge all -c options (later wins) into typed values.

    samplerate stays the requested rate; see effective_samplerate().
    """
    raw: Dict[str, str] = {}
    for text in text_list:
        if not text.strip():
            raise UsageError("empty -c option")
        raw.update(parse_kv(text))
    out: dict = {}
    for key, value in raw.items():
        if key == "samplerate":
            rate = parse_si(value)
            samplerate_divider(rate)  # range check
            out[key] = rate
        elif key == "captureratio":
            out[key] = parse_captureratio(value)
        elif key == "voltage_threshold":
            out[key] = parse_threshold(value)
        elif key == "continuous":
            out[key] = parse_bool(value, key)
        else:
            raise UsageError(
                f"unknown config key '{key}': expected one of {', '.join(sorted(CONFIG_KEYS))}")
    return out


def parse_si_float(text: str) -> float:
    """Like parse_si() but allows fractions: "0.5", "1.5k", "2.5 MHz"."""
    m = _SI_RE.fullmatch(text.strip())
    if not m:
        raise UsageError(f"invalid number '{text}'")
    return float(m.group(1)) * _SI_MULT[m.group(2)]


def parse_channel_group(text: str) -> int:
    """-g value -> PWM index (0 for PWM1, 1 for PWM2)."""
    if text not in PWM_GROUPS:
        raise UsageError(f"unknown channel group '{text}': expected {' or '.join(PWM_GROUPS)}")
    return PWM_GROUPS.index(text)


def parse_pwm_config(text_list: List[str]) -> dict:
    """-c options for a PWM group: enabled (bool), output_frequency (Hz), duty_cycle (%)."""
    raw: Dict[str, str] = {}
    for text in text_list:
        if not text.strip():
            raise UsageError("empty -c option")
        raw.update(parse_kv(text))
    out: dict = {}
    for key, value in raw.items():
        if key == "enabled":
            out[key] = parse_bool(value, key)
        elif key == "output_frequency":
            out[key] = parse_si_float(value)
        elif key == "duty_cycle":
            v = value.strip()
            out[key] = parse_si_float(v[:-1] if v.endswith("%") else v)
        else:
            raise UsageError(
                f"unknown PWM config key '{key}': expected one of {', '.join(sorted(PWM_KEYS))}")
    return out
