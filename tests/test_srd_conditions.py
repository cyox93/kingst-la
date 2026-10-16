import copy
import random

import numpy as np
import pytest

from kingst_la.srd import conditions as c


def run(fn, conds, buf, start, cmap, old):
    """Repeated wait() calls over one buffer, like a decoder loop."""
    out = []
    pos = start
    while True:
        idx, matched = fn(conds, buf, pos, cmap, old)
        if idx is None:
            out.append(("end", tuple(old), [t.skipped for cond in conds for t in cond]))
            return out
        out.append((idx, matched, tuple(old)))
        pos = idx
        if len(out) > 200:
            return out


def compare(spec, buf, cmap, start=0, old=None):
    old = list(old if old is not None else [int(buf[0] >> b) & 1 if b >= 0 else 2 for b in cmap])
    a = c.parse(copy.deepcopy(spec), len(cmap), cmap)
    b = c.parse(copy.deepcopy(spec), len(cmap), cmap)
    ra = run(c.search_reference, a, buf, start, cmap, list(old))
    rb = run(c.search, b, buf, start, cmap, list(old))
    assert ra == rb, spec


def test_parse_forms():
    assert c.parse(None, 1, [0]) is None
    assert c.parse({}, 1, [0]) is None
    assert c.parse([], 1, [0]) is None
    assert [t.kind for t in c.parse({0: "rising"}, 1, [0])[0]] == [c.RISING]  # first letter counts
    assert c.parse({0: "x"}, 1, [0])[0][0].kind == c.ALWAYS_FALSE
    assert c.parse({5: "h"}, 1, [0])[0][0].kind == c.ALWAYS_FALSE
    assert c.parse({0: "h"}, 1, [-1])[0][0].kind == c.ALWAYS_FALSE          # unmapped channel
    assert c.parse({"skip": -1}, 1, [0])[0][0].kind == c.ALWAYS_FALSE
    assert c.parse({"anything": 3}, 1, [0])[0][0].kind == c.SKIP
    with pytest.raises(c.ConditionError):
        c.parse([{0: "h"}, 3], 1, [0])
    with pytest.raises(c.ConditionError):
        c.parse((0, "h"), 1, [0])


def test_edge_matches_at_the_edge_sample_and_not_again():
    buf = np.array([0, 0, 1, 1, 0, 1], np.uint16)
    conds = c.parse({0: "r"}, 1, [0])
    old = [0]
    assert c.search(conds, buf, 0, [0], old)[0] == 2
    assert c.search(conds, buf, 2, [0], old)[0] == 5   # same sample re-checked, edge consumed


def test_no_edge_at_sample_zero():
    buf = np.array([1, 0, 1], np.uint16)
    conds = c.parse({0: "e"}, 1, [0])
    assert c.search(conds, buf, 0, [0], [1])[0] == 1


def test_level_rematches_same_sample():
    buf = np.array([0, 1, 1], np.uint16)
    conds = c.parse({0: "h"}, 1, [0])
    old = [0]
    assert c.search(conds, buf, 0, [0], old)[0] == 1
    assert c.search(conds, buf, 1, [0], old)[0] == 1


def test_skip_counts():
    buf = np.zeros(10, np.uint16)
    assert c.search(c.parse({"skip": 0}, 1, [0]), buf, 3, [0], [0])[0] == 3
    assert c.search(c.parse({"skip": 4}, 1, [0]), buf, 3, [0], [0])[0] == 7
    assert c.search(c.parse({"skip": 9}, 1, [0]), buf, 3, [0], [0])[0] is None


def test_skip_persists_across_chunks():
    conds = c.parse({"skip": 5}, 1, [0])
    old = [0]
    assert c.search(conds, np.zeros(3, np.uint16), 0, [0], old)[0] is None
    assert c.search(conds, np.zeros(10, np.uint16), 0, [0], old)[0] == 2


def test_matched_reports_every_condition():
    buf = np.array([0, 1, 1, 0], np.uint16)
    idx, matched = c.search(c.parse([{0: "r"}, {0: "h"}, {0: "f"}], 1, [0]), buf, 0, [0], [0])
    assert (idx, matched) == (1, (True, True, False))


TERMS = ["h", "l", "r", "f", "e", "n"]


def random_spec(rng, nch):
    conds = []
    for _ in range(rng.randint(1, 3)):
        d = {}
        for _ in range(rng.randint(1, 3)):
            if rng.random() < 0.25:
                d["skip"] = rng.choice([0, 1, 2, 7, 40, 300, 1000])
            else:
                d[rng.randrange(-1, nch + 1)] = rng.choice(TERMS)
        conds.append(d)
    return conds if len(conds) > 1 or rng.random() < 0.5 else conds[0]


@pytest.mark.parametrize("seed", range(300))
def test_vectorized_matches_reference(seed):
    rng = random.Random(seed)
    nch = rng.randint(1, 3)
    cmap = [rng.choice([0, 1, 2, 5, -1]) for _ in range(nch)]
    density = rng.choice([0.02, 0.2, 0.5])
    np_rng = np.random.default_rng(seed)
    n = rng.choice([1, 5, 300, 3000])
    flips = np_rng.random((n, 6)) < density
    bits = np.cumsum(flips, axis=0) & 1
    buf = (bits * (1 << np.arange(6))).sum(axis=1).astype(np.uint16)
    compare(random_spec(rng, nch), buf, cmap, start=rng.randrange(n))


def test_large_buffer_is_fast():
    import time
    buf = np.zeros(4_000_000, np.uint16)
    buf[3_999_000:] = 1
    conds = c.parse({0: "r"}, 1, [0])
    t = time.perf_counter()
    assert c.search(conds, buf, 0, [0], [0])[0] == 3_999_000
    assert time.perf_counter() - t < 0.5
