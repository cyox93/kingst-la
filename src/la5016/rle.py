"""RLE expansion of LA5016 capture memory (design §4.3).

Layout: each 16-byte transfer = 5 x (u16 LE pin values, u8 repeat count)
followed by one u8 sequence number. A packet means "these pin values held for
`repeat` samples" (0..255; 0 contributes nothing).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

import numpy as np

from .pipeline import Sink

TRANSFER_SIZE = 16
PACKETS_PER_TRANSFER = 5
MAX_REPEAT = 255

_PIN_LO = np.arange(PACKETS_PER_TRANSFER) * 3
_REP_COL = _PIN_LO + 2
_SEQ_COL = 15


@dataclass
class ExpandStats:
    samples: int = 0              # samples fed to the sink
    packets: int = 0              # packets consumed
    transfers: int = 0            # full transfers consumed
    seq_breaks: int = 0           # transfers whose seq != (previous + 1) % 256
    leftover_bytes: int = 0       # trailing partial transfer ignored at end of input
    truncated: bool = False       # stopped because limit_samples was reached
    trigger_sample: Optional[int] = None  # sample index of the emitted trigger


class _Expander:
    def __init__(self, sink: Sink, trigger_packets: Optional[int],
                 limit: Optional[int], max_chunk: int) -> None:
        self.sink = sink
        self.trig = trigger_packets
        self.limit = limit
        self.max_chunk = max_chunk
        self.st = ExpandStats()
        self.prev_seq: Optional[int] = None

    def _feed(self, out: np.ndarray) -> bool:
        """Feed `out` in <= max_chunk pieces honoring the limit. False => stop."""
        st = self.st
        if self.limit is not None:
            room = self.limit - st.samples
            if len(out) >= room:
                out = out[:room]
                st.truncated = True
        for i in range(0, len(out), self.max_chunk):
            self.sink.feed(out[i:i + self.max_chunk])
        st.samples += len(out)
        return not st.truncated

    def packets(self, pins: np.ndarray, reps: np.ndarray) -> bool:
        """Expand a bounded run of packets, splitting at the trigger. False => stop."""
        st = self.st
        i, n = 0, len(pins)
        while True:
            if self.trig is not None and st.trigger_sample is None and st.packets == self.trig:
                # Reached only while samples < limit, so a trigger at the limit is dropped.
                self.sink.trigger()
                st.trigger_sample = st.samples
            if i == n:
                return True
            j = n
            if self.trig is not None and st.trigger_sample is None:
                j = min(n, i + self.trig - st.packets)
            out = np.repeat(pins[i:j], reps[i:j])
            st.packets += j - i
            if len(out) and not self._feed(out):
                return False
            i = j

    def transfers(self, rows: np.ndarray) -> bool:
        """Process full transfers (shape (-1, 16), uint8). False => stop."""
        st = self.st
        step = max(1, self.max_chunk // (PACKETS_PER_TRANSFER * MAX_REPEAT))
        for a in range(0, len(rows), step):
            blk = rows[a:a + step]
            seq = blk[:, _SEQ_COL].astype(np.int32)
            if self.prev_seq is not None:
                st.seq_breaks += int(seq[0] != (self.prev_seq + 1) & 0xFF)
            st.seq_breaks += int(np.count_nonzero(seq[1:] != (seq[:-1] + 1) & 0xFF))
            self.prev_seq = int(seq[-1])
            st.transfers += len(blk)
            pins = (blk[:, _PIN_LO].astype(np.uint16)
                    | (blk[:, _PIN_LO + 1].astype(np.uint16) << 8)).ravel()
            reps = blk[:, _REP_COL].ravel()
            if not self.packets(pins, reps):
                return False
        return True


def expand(raw_chunks: Iterable[bytes], sink: Sink, *, trigger_packets: Optional[int] = None,
           limit_samples: Optional[int] = None, max_chunk: int = 1 << 22) -> ExpandStats:
    """Expand raw RLE capture bytes into uint16 sample arrays fed to `sink`.

    Only `sink.feed()` and `sink.trigger()` are called; begin/end are the
    caller's job. Chunks may split transfers anywhere. Fed arrays are never
    empty and never longer than `max_chunk`.

    trigger_packets: None = no trigger; otherwise `sink.trigger()` is called
    once, right after that many packets (0 = before the first sample). It is
    not emitted if the data ends first, if there is no data at all, or if its
    sample index is >= limit_samples (no sample would follow it).

    limit_samples: stop after exactly that many samples without reading more
    input; `truncated` is set whenever the limit is reached.

    Sequence breaks are counted in the stats, not logged.
    """
    if max_chunk < 1:
        raise ValueError("max_chunk must be >= 1")
    ex = _Expander(sink, trigger_packets, limit_samples, max_chunk)
    if limit_samples is not None and limit_samples <= 0:
        ex.st.truncated = True
        return ex.st
    rem = b""
    for chunk in raw_chunks:
        data = rem + bytes(chunk) if rem else chunk
        full = len(data) - len(data) % TRANSFER_SIZE
        rem = bytes(data[full:])
        if not full:
            continue
        rows = np.frombuffer(data, dtype=np.uint8, count=full).reshape(-1, TRANSFER_SIZE)
        if not ex.transfers(rows):
            return ex.st
    ex.st.leftover_bytes = len(rem)
    return ex.st
