"""sigrok-compatible output formats (bits, hex, csv, vcd, binary).

Each format follows the libsigrok module src/output/<name>.c and writes
bytes to a binary stream through the pipeline Sink interface.
"""

from __future__ import annotations

from typing import BinaryIO, Dict, Mapping, Optional

from ..pipeline import Sink
from .binary import BinaryOutput
from .bits import BitsOutput
from .csv import CsvOutput
from .hex import HexOutput
from .vcd import VcdOutput

FORMATS: Dict[str, type] = {
    "bits": BitsOutput,
    "hex": HexOutput,
    "csv": CsvOutput,
    "vcd": VcdOutput,
    "binary": BinaryOutput,
}


def create(name: str, out: BinaryIO, options: Optional[Mapping[str, str]] = None) -> Sink:
    """Create the output `name` writing to `out`; options are strings as on the CLI."""
    try:
        cls = FORMATS[name]
    except KeyError:
        raise ValueError(f"unknown output format '{name}' "
                         f"(available: {', '.join(FORMATS)})") from None
    return cls(out, dict(options or {}))


__all__ = ["FORMATS", "create", "BitsOutput", "HexOutput", "CsvOutput",
           "VcdOutput", "BinaryOutput"]
