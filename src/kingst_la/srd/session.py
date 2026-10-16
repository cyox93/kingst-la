"""Decoder instances, stacks and the sample feed, as in libsigrokdecode.

Ported from libsigrokdecode instance.c / session.c / type_decoder.c
(GPL-3.0-or-later).

Each stack's bottom decoder runs decode() in a worker thread. feed() hands a
chunk to the worker and blocks until the worker has consumed it and waits for
more, so only one thread runs at a time and output order is deterministic.
Stacked decoders have no thread: the lower decoder's put(OUTPUT_PYTHON) calls
their decode(ss, es, data) directly.
"""

from __future__ import annotations

import logging
import operator
import threading
import traceback
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence

import numpy as np

from kingst_la.pipeline import Meta
from kingst_la.srd import api, conditions

log = logging.getLogger("srd")

MASK64 = (1 << 64) - 1


class DecoderError(Exception):
    """A decoder raised; the session stops."""


class _Terminate(BaseException):
    """Unwinds decode() when the session aborts."""


@dataclass
class _Output:
    type: int
    proto_id: str
    meta: Optional[tuple] = None


class Callbacks:
    """Receivers for decoder output; the defaults drop everything."""

    def annotation(self, inst: "Instance", proto_id: str, ss: int, es: int, cls: int, texts: List[str]) -> None:
        pass

    def binary(self, inst: "Instance", proto_id: str, ss: int, es: int, cls: int, data: bytes) -> None:
        pass

    def meta(self, inst: "Instance", proto_id: str, ss: int, es: int, name: str, value) -> None:
        pass


class Instance:
    def __init__(self, info, inst_id: str, options: Dict, channelmap: Sequence[int], callbacks: Callbacks):
        self.info = info                 # loader.DecoderInfo
        self.inst_id = inst_id
        self.channelmap = list(channelmap)
        self.callbacks = callbacks
        self.next: List[Instance] = []
        self.outputs: List[_Output] = []

        self.py = info.cls()
        self.py._srd = self
        if hasattr(info.cls, "options"):
            self.py.options = dict(options)

        # Sample feed state (guarded by self._cv).
        self._cv = threading.Condition()
        self._buf: Optional[np.ndarray] = None
        self._abs_start = 0
        self._cur = 0
        self._have_data = False
        self._eof = False
        self._terminate = False
        self._finished = False
        self._thread: Optional[threading.Thread] = None
        self.error: Optional[BaseException] = None
        self._conds = None               # last condition list, for the condition-less wait() rule
        self._old: List[int] = [2] * len(self.channelmap)
        self._seeded = False

    # --- called by decoders through api.Decoder ------------------------------

    def register(self, output_type, proto_id=None, meta=None):
        proto_id = self.inst_id if proto_id is None else proto_id
        if output_type == api.OUTPUT_META:
            if meta is None or len(meta) != 3 or meta[0] not in (int, float):
                raise TypeError("Unsupported type.")
            meta = tuple(meta)
        else:
            meta = None
        for i, o in enumerate(self.outputs):
            if o.type == output_type and o.proto_id == proto_id and (meta is None or o.meta == meta):
                return i
        self.outputs.append(_Output(output_type, proto_id, meta))
        return len(self.outputs) - 1

    def has_channel(self, index):
        if not 0 <= index < len(self.channelmap):
            raise IndexError("invalid channel index %d" % index)
        return self.channelmap[index] != -1

    def put(self, startsample, endsample, output_id, data):
        ss = operator.index(startsample) & MASK64
        es = operator.index(endsample) & MASK64
        if not 0 <= output_id < len(self.outputs):
            raise ValueError("%s: invalid output id %r" % (self.inst_id, output_id))
        out = self.outputs[output_id]
        if out.type == api.OUTPUT_ANN:
            if not (isinstance(data, list) and len(data) == 2 and isinstance(data[0], int)
                    and 0 <= data[0] < len(self.info.annotations)
                    and isinstance(data[1], list) and data[1] and all(isinstance(s, str) for s in data[1])):
                log.error("%s: invalid annotation %r", self.inst_id, data)
                return None
            self.callbacks.annotation(self, out.proto_id, ss, es, data[0], data[1])
        elif out.type == api.OUTPUT_PYTHON:
            for upper in self.next:
                try:
                    upper.py.decode(ss, es, data)
                except Exception as e:
                    _report(upper, e)
        elif out.type == api.OUTPUT_BINARY:
            if not (isinstance(data, list) and len(data) == 2 and isinstance(data[0], int)
                    and 0 <= data[0] < len(self.info.binary)
                    and isinstance(data[1], (bytes, bytearray)) and data[1]):
                log.error("%s: invalid binary output %r", self.inst_id, data)
                return None
            self.callbacks.binary(self, out.proto_id, ss, es, data[0], bytes(data[1]))
        elif out.type == api.OUTPUT_META:
            # convert_meta(): PyLong_Check / PyFloat_Check, so bool and float subclasses pass.
            if out.meta[0] is int:
                if not isinstance(data, int):
                    raise TypeError("This output was registered as 'int', but something else was passed.")
                data = int(data)
            else:
                if not isinstance(data, float):
                    raise TypeError("This output was registered as 'float', but something else was passed.")
                data = float(data)
            self.callbacks.meta(self, out.proto_id, ss, es, out.meta[1], data)
        return None

    def wait(self, conds=None):
        nch = len(self.channelmap)
        parsed = conditions.parse(conds, nch, self.channelmap)
        if parsed is None:
            skip = 1 if (self._cur or self._conds is not None) else 0
            parsed = conditions.skip_condition(skip)
        self._conds = parsed
        with self._cv:
            while True:
                while not self._have_data and not self._terminate:
                    self._cv.wait()
                if self._terminate:
                    raise _Terminate()
                buf = self._buf
                if self._cur == 0 and not self._seeded and len(buf):
                    s = int(buf[0])
                    self._old = [((s >> b) & 1) if b >= 0 else 2 for b in self.channelmap]
                    self._seeded = True
                idx, matched = conditions.search(parsed, buf, self._cur - self._abs_start,
                                                 self.channelmap, self._old)
                if idx is not None:
                    self._cur = self._abs_start + idx
                    self.py.samplenum = self._cur
                    self.py.matched = matched or None
                    s = int(buf[idx])
                    return tuple(((s >> b) & 1) if b >= 0 else 0xFF for b in self.channelmap)
                self._cur = self._abs_start + len(buf)
                self._have_data = False
                self._cv.notify_all()
                if self._eof:
                    self.py.samplenum = self._cur
                    if getattr(self, "_eof_raised", False):
                        raise _Terminate()
                    self._eof_raised = True
                    raise EOFError("samples exhausted")

    # --- driven by the session ----------------------------------------------

    def metadata(self, samplerate: int) -> None:
        if samplerate and hasattr(self.py, "metadata"):
            self.py.metadata(api.SRD_CONF_SAMPLERATE, samplerate)
        for upper in self.next:
            upper.metadata(samplerate)

    def start(self) -> None:
        self.py.start()
        self.py.samplenum = 0
        self.py.matched = None
        for upper in self.next:
            upper.start()

    def _run(self) -> None:
        try:
            self.py.decode()
        except (EOFError, _Terminate):
            pass
        except BaseException as e:  # reported by feed()/eof() in the main thread
            self.error = e
            self.error_tb = traceback.format_exc()
        finally:
            with self._cv:
                self._finished = True
                self._have_data = False
                self._cv.notify_all()

    def _handoff(self) -> None:
        with self._cv:
            self._have_data = True
            self._cv.notify_all()
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name=self.inst_id, daemon=True)
                self._thread.start()
            while self._have_data and not self._finished:
                self._cv.wait()
        if self.error is not None:
            raise DecoderError(self._error_text()) from self.error

    def feed(self, buf: np.ndarray, abs_start: int) -> None:
        if self._finished:
            return
        with self._cv:
            self._buf = buf
            self._abs_start = abs_start
        self._handoff()
        self._flush()

    def eof(self) -> None:
        if self._thread is not None and not self._finished:
            with self._cv:
                self._buf = np.zeros(0, np.uint16)
                self._abs_start = self._cur
                self._eof = True
            self._handoff()
            self._thread.join()
            if self.error is not None:
                raise DecoderError(self._error_text()) from self.error
        self._flush()

    def terminate(self) -> None:
        with self._cv:
            self._terminate = True
            self._cv.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _flush(self) -> None:
        if hasattr(self.py, "flush"):
            self.py.flush()
        for upper in self.next:
            upper._flush()

    def _error_text(self) -> str:
        return "%s: Protocol decoder instance %s: %s" % (type(self.error).__name__, self.inst_id, self.error)


def _report(inst: Instance, e: BaseException) -> None:
    log.error("%s: Protocol decoder instance %s: %s", type(e).__name__, inst.inst_id, e)
    log.debug("%s", traceback.format_exc())


class DecoderSink:
    """pipeline.Sink that feeds decoder stacks."""

    def __init__(self, stacks: List[Instance]):
        self.stacks = stacks
        self._pos = 0

    def begin(self, meta: Meta) -> None:
        for root in self.stacks:
            root.metadata(meta.samplerate)
            root.start()

    def feed(self, samples: np.ndarray) -> None:
        for root in self.stacks:
            root.feed(samples, self._pos)
        self._pos += len(samples)

    def trigger(self) -> None:
        pass

    def end(self) -> None:
        for root in self.stacks:
            root.eof()

    def abort(self) -> None:
        for root in self.stacks:
            root.terminate()
