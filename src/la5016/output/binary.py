"""Binary output: raw sample data, 2 bytes per sample, little endian.

Ported from libsigrok src/output/binary.c (receive()).
"""

from __future__ import annotations

from typing import BinaryIO, Mapping, Optional

import numpy as np

from ..pipeline import Meta
from ._common import as_samples, parse_options


class BinaryOutput:
    NAME = "binary"

    def __init__(self, out: BinaryIO, options: Optional[Mapping[str, object]] = None):
        parse_options(self.NAME, {}, options)
        self.out = out

    def begin(self, meta: Meta) -> None:
        pass

    def feed(self, samples: np.ndarray) -> None:
        data = as_samples(samples).astype("<u2", copy=False).tobytes()
        if data:
            self.out.write(data)

    def trigger(self) -> None:
        pass

    def end(self) -> None:
        pass
