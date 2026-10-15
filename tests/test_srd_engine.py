"""Decoder engine semantics, with small decoders written in the test."""

import textwrap

import numpy as np
import pytest

from la5016.pipeline import Channel, Meta
from la5016.srd import annotate, loader, session

CHANNELS = [Channel(i, "CH%d" % i) for i in range(16)]

EDGES = '''
import sigrokdecode as srd

class Decoder(srd.Decoder):
    api_version = 3
    id = 'edges'
    name = 'Edges'
    longname = 'Edge lister'
    desc = 'Reports edges.'
    license = 'gplv3+'
    inputs = ['logic']
    outputs = ['edges']
    tags = ['Util']
    channels = ({'id': 'd', 'name': 'D', 'desc': 'data'},)
    optional_channels = ({'id': 'x', 'name': 'X', 'desc': 'extra'},)
    options = (
        {'id': 'edge', 'desc': 'Edge', 'default': 'e', 'values': ('e', 'r', 'f')},
        {'id': 'limit', 'desc': 'Limit', 'default': 0},
        {'id': 'scale', 'desc': 'Scale', 'default': 1.0},
        {'id': 'flag', 'desc': 'Flag', 'default': True},
    )
    annotations = (('edge', 'Edge'), ('end', 'End'))
    annotation_rows = (('all', 'All', (0, 1)),)
    binary = (('raw', 'Raw'),)

    def __init__(self):
        self.reset()

    def reset(self):
        self.calls = []

    def metadata(self, key, value):
        if key == srd.SRD_CONF_SAMPLERATE:
            self.samplerate = value

    def start(self):
        self.out_ann = self.register(srd.OUTPUT_ANN)
        self.out_py = self.register(srd.OUTPUT_PYTHON)
        self.out_bin = self.register(srd.OUTPUT_BINARY)
        assert self.register(srd.OUTPUT_ANN) == self.out_ann

    def decode(self):
        n = 0
        first = self.wait()
        self.put(0, 0, self.out_ann, [0, ['first %d %r %r' % (self.samplenum, first, self.has_channel(1))]])
        try:
            while True:
                pins = self.wait({0: self.options['edge']})
                n += 1
                self.put(self.samplenum, self.samplenum, self.out_ann, [0, ['%d=%d' % (self.samplenum, pins[0]), 'e']])
                self.put(self.samplenum, self.samplenum, self.out_py, ('edge', self.samplenum, pins[0]))
                self.put(self.samplenum, self.samplenum, self.out_bin, [0, bytes([pins[0]])])
                if self.options['limit'] and n >= self.options['limit']:
                    return
        except EOFError:
            self.put(self.samplenum, self.samplenum, self.out_ann, [1, ['eof %d' % self.samplenum]])
            raise
'''

UPPER = '''
import sigrokdecode as srd

class Decoder(srd.Decoder):
    api_version = 3
    id = 'upper'
    name = 'Upper'
    longname = 'Stacked lister'
    desc = 'Counts edges from below.'
    license = 'gplv3+'
    inputs = ['edges']
    outputs = []
    tags = ['Util']
    annotations = (('count', 'Count'),)

    def __init__(self):
        self.n = 0

    def reset(self):
        pass

    def start(self):
        self.out_ann = self.register(srd.OUTPUT_ANN)

    def decode(self, ss, es, data):
        self.n += 1
        self.put(ss, es, self.out_ann, [0, ['#%d %s' % (self.n, data[0])]])
'''

BROKEN = UPPER.replace("'upper'", "'broken'").replace(
    "    annotations =", "    channels = ({'id': 'd', 'name': 'D', 'desc': 'data'},)\n    annotations =").replace(
    "    def decode(self, ss, es, data):", "    def decode(self):\n        self.wait({0: 'r'})\n        raise ValueError('boom')\n\n    def unused(self, ss, es, data):")


@pytest.fixture
def decoders(tmp_path):
    for name, src in (("edges", EDGES), ("upper", UPPER), ("broken", BROKEN)):
        d = tmp_path / name
        d.mkdir()
        (d / "__init__.py").write_text("'''\n%s decoder.\n'''\nfrom .pd import Decoder\n" % name)
        (d / "pd.py").write_text(textwrap.dedent(src))
    loader._loaded.clear()
    yield tmp_path
    loader._loaded.clear()


class Recorder(session.Callbacks):
    def __init__(self):
        self.anns = []
        self.bins = []

    def annotation(self, inst, proto_id, ss, es, cls, texts):
        self.anns.append((proto_id, ss, es, cls, texts[0]))

    def binary(self, inst, proto_id, ss, es, cls, data):
        self.bins.append(data)


def run(path, pds, samples, chunk=3, rec=None):
    rec = rec or Recorder()
    stacks = loader.build_stacks(pds, CHANNELS, rec, path)
    sink = session.DecoderSink(stacks)
    sink.begin(Meta(1000, CHANNELS))
    x = np.array(samples, np.uint16)
    for i in range(0, len(x), chunk):
        sink.feed(x[i:i + chunk])
    sink.end()
    return rec, stacks


SIG = [0, 0, 1, 1, 0, 1, 1, 1, 0, 0]  # edges at 2, 4, 5, 8


@pytest.mark.parametrize("chunk", [1, 2, 3, 10])
def test_edges_eof_and_chunking(decoders, chunk):
    rec, _ = run(decoders, ["edges:d=CH0"], SIG, chunk)
    texts = [a[4] for a in rec.anns]
    assert texts == ["first 0 (0, 255) False", "2=1", "4=0", "5=1", "8=0", "eof 10"]
    assert rec.bins == [b"\x01", b"\x00", b"\x01", b"\x00"]


def test_option_conversion_and_identity_map(decoders):
    _, stacks = run(decoders, ["edges:edge=r:limit=0x2"], SIG)
    inst = stacks[0]
    assert inst.py.options == {"edge": "r", "limit": 2, "scale": 1.0, "flag": 1}
    assert inst.channelmap == [0, 1]   # no channels given: identity, all connected


def test_channel_mapping_by_name(decoders):
    chans = [Channel(3, "SDA"), Channel(5, "CH5")]
    stacks = loader.build_stacks(["edges:d=SDA:x=CH5"], chans, Recorder(), decoders)
    assert stacks[0].channelmap == [3, 5]
    with pytest.raises(loader.LoadError, match="not an enabled channel"):
        loader.build_stacks(["edges:d=CH9"], chans, Recorder(), decoders)
    with pytest.raises(loader.LoadError, match="needs channel"):
        loader.build_stacks(["edges:x=SDA"], chans, Recorder(), decoders)
    with pytest.raises(loader.LoadError, match="Unknown option"):
        loader.build_stacks(["edges:d=SDA:speed=1"], chans, Recorder(), decoders)
    with pytest.raises(loader.LoadError, match="requires a number"):
        loader.build_stacks(["edges:limit=ten"], chans, Recorder(), decoders)


def test_strtoll_base0():
    assert loader._strtoll0("010") == 8
    assert loader._strtoll0("0x1F") == 31
    assert loader._strtoll0("-12") == -12
    with pytest.raises(ValueError):
        loader._strtoll0("09")


def test_stack_and_instance_ids(decoders):
    rec, stacks = run(decoders, ["edges:d=CH0:edge=r,upper", "edges:d=CH0:edge=f"], SIG)
    ids = {a[0] for a in rec.anns}
    assert ids == {"edges-1", "edges-2", "upper-1"}
    ups = [a[4] for a in rec.anns if a[0] == "upper-1"]
    assert ups == ["#1 edge", "#2 edge"]
    # Per chunk, stack 1 runs before stack 2: annotation order is deterministic.
    first_two_edges = [a for a in rec.anns if a[4] in ("2=1", "4=0")]
    assert [a[0] for a in first_two_edges] == ["edges-1", "edges-2"]


def test_decode_return_stops_that_stack_only(decoders):
    rec, _ = run(decoders, ["edges:d=CH0:limit=1"], SIG)
    assert [a[4] for a in rec.anns] == ["first 0 (0, 255) False", "2=1"]


def test_decoder_exception_raises(decoders):
    with pytest.raises(session.DecoderError, match="ValueError: Protocol decoder instance broken-1: boom"):
        run(decoders, ["broken"], SIG)


def test_annotation_printer_formats(decoders, capsys):
    infos = {"edges": loader.load("edges", decoders)}
    for level, sel, expect in [
        (2, None, "edges-1: 2=1"),
        (3, None, '2-2 edges-1: "2=1"'),
        (4, None, '2-2 edges-1: edge: "2=1" "e"'),
        (2, "edges=end", "edges-1: eof 10"),
        (2, "edges=ALL", "edges-1: 2=1"),     # row id, canonical compare
    ]:
        import io
        out = io.StringIO()
        pr = annotate.AnnotationPrinter(out, annotate.parse_annotations(sel, infos), loglevel=level)
        run(decoders, ["edges:d=CH0"], SIG, rec=pr)
        lines = out.getvalue().splitlines()
        assert expect in lines, (level, sel, lines)
        if sel == "edges=end":
            assert lines == ["edges-1: eof 10"]


def test_annotation_selection_errors(decoders):
    infos = {"edges": loader.load("edges", decoders)}
    with pytest.raises(loader.LoadError):
        annotate.parse_annotations("uart", infos)
    with pytest.raises(loader.LoadError):
        annotate.parse_annotations("edges=nope", infos)
    assert annotate.parse_binary("edges=raw", infos) == {"edges": 0}
    with pytest.raises(loader.LoadError):
        annotate.parse_binary("edges=RAW", infos)   # exact match for -B


def test_show_and_list_text(decoders):
    info = loader.load("edges", decoders)
    text = annotate.show_text(info)
    assert "ID: edges\nName: Edges\nLong name: Edge lister\n" in text
    assert "- edge: Edge ('e', 'r', 'f', default 'e')\n" in text
    assert "- limit: Limit (default 0)\n" in text
    assert "- all (All): edge, end\n" in text
    assert "Optional channels:\n- x (X): extra\n" in text
    assert "Documentation:\nedges decoder.\n" in text
    assert annotate.list_text([info]) == "Supported protocol decoders:\n  %-20s %s\n\n" % ("edges", "Edge lister")
