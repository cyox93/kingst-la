"""Sample stream plumbing shared by the capture source, outputs and (later) decoders.

Samples travel as numpy uint16 arrays; bit i of each sample is channel CHi.
The trigger is an event in the stream, not header metadata, because its
position is only known while the RLE data is being expanded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Protocol

import numpy as np


@dataclass(frozen=True)
class Channel:
    index: int  # hardware bit position, 0..15
    name: str   # "CH0" unless renamed with -C CH0=NAME


@dataclass
class Meta:
    samplerate: int                       # Hz, the rate the hardware actually runs at
    channels: List[Channel] = field(default_factory=list)  # enabled channels, in output order
    total_channels: int = 16              # logic channels of the source, enabled or not


class Sink(Protocol):
    def begin(self, meta: Meta) -> None: ...
    def feed(self, samples: np.ndarray) -> None: ...
    def trigger(self) -> None: ...
    def end(self) -> None: ...
