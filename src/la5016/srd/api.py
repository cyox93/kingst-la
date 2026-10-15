"""Stand-in for libsigrokdecode's `sigrokdecode` Python module.

Protocol decoders do `import sigrokdecode as srd`; the loader registers this
module under that name. The methods forward to the engine instance that the
session attaches as `self._srd` right after constructing the decoder.
"""

from __future__ import annotations

OUTPUT_ANN = 0
OUTPUT_PYTHON = 1
OUTPUT_BINARY = 2
OUTPUT_LOGIC = 3
OUTPUT_META = 4

SRD_CONF_SAMPLERATE = 10000


class Decoder:
    """Base class of every protocol decoder (api_version 3)."""

    def register(self, output_type, proto_id=None, meta=None):
        return self._srd.register(output_type, proto_id, meta)

    def put(self, startsample, endsample, output_id, data):
        return self._srd.put(startsample, endsample, output_id, data)

    def wait(self, conds=None):
        return self._srd.wait(conds)

    def has_channel(self, index):
        return self._srd.has_channel(index)
