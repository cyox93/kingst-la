"""wait() condition matching, with the semantics of libsigrokdecode find_match().

Ported from libsigrokdecode instance.c / type_decoder.c (GPL-3.0-or-later).

A condition list is OR'ed; terms inside one condition (dict) are AND'ed and
short-circuit in insertion order. Matching starts at the current sample,
inclusive. Edge terms compare against the previous sample; at absolute sample
0 the previous value is sample 0 itself, so no edge matches there. A skip term
counts the samples on which it is evaluated without matching and matches once
the count reaches its value; counts persist across chunks within one wait().

`search()` is vectorized with numpy; `search_reference()` is a literal
per-sample port used to verify it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import numpy as np

HIGH, LOW, RISING, FALLING, EITHER, NO_EDGE, SKIP, ALWAYS_FALSE = range(8)

_TERM_TYPES = {"h": HIGH, "l": LOW, "r": RISING, "f": FALLING, "e": EITHER, "n": NO_EDGE}


@dataclass
class Term:
    kind: int
    channel: int = -1   # decoder channel index for pin terms
    count: int = 0      # samples to skip
    skipped: int = 0    # samples skipped so far


class ConditionError(TypeError):
    pass


def parse(conds, num_channels: int, channelmap: Sequence[int]) -> Optional[List[List[Term]]]:
    """Parse wait() arguments. Returns None for "no condition"."""
    if conds is None:
        return None
    if isinstance(conds, dict):
        if not conds:
            return None
        conds = [conds]
    elif isinstance(conds, list):
        if not conds:
            return None
    else:
        raise ConditionError("Condition list is neither a list nor a dict.")
    out = []
    for cond in conds:
        if not isinstance(cond, dict):
            raise ConditionError("Condition is not a dict.")
        terms = []
        for key, value in cond.items():
            if isinstance(key, int):
                if not isinstance(value, str) or not value:
                    raise ConditionError("Failed to get the value.")
                kind = _TERM_TYPES.get(value[0], ALWAYS_FALSE)
                if key < 0 or key >= num_channels or channelmap[key] < 0:
                    # Out of range in C; unmapped optional channel is undefined in C.
                    kind = ALWAYS_FALSE
                terms.append(Term(kind, channel=key))
            elif isinstance(key, str):
                if not isinstance(value, int):
                    raise ConditionError("Failed to get number of samples to skip.")
                terms.append(Term(SKIP if value >= 0 else ALWAYS_FALSE, count=value))
            else:
                raise ConditionError("Term key is neither a string nor a number.")
        out.append(terms)
    return out


def skip_condition(count: int) -> List[List[Term]]:
    return [[Term(SKIP, count=count)]]


def _pin(sample: int, bit: int) -> int:
    return (sample >> bit) & 1


def _term_matches(term: Term, old: int, new: int) -> bool:
    k = term.kind
    if k == SKIP:
        if term.skipped == term.count:
            return True
        term.skipped += 1
        return False
    if k == HIGH:
        return new == 1
    if k == LOW:
        return new == 0
    if k == RISING:
        return old == 0 and new == 1
    if k == FALLING:
        return old == 1 and new == 0
    if k == EITHER:
        return old != new
    if k == NO_EDGE:
        return old == new
    return False


def search_reference(conds: List[List[Term]], buf: np.ndarray, start: int,
                     channelmap: Sequence[int], old: List[int]) -> Tuple[Optional[int], Tuple[bool, ...]]:
    """Literal port of find_match(). `old` holds previous pin values per
    decoder channel and is updated in place. Returns (index, matched)."""
    for i in range(start, len(buf)):
        s = int(buf[i])
        matched = []
        for cond in conds:
            ok = True
            for t in cond:
                if t.kind == ALWAYS_FALSE:
                    ok = False
                    break
                o = n = 0
                if t.kind != SKIP:
                    n = _pin(s, channelmap[t.channel])
                    o = old[t.channel]
                if not _term_matches(t, o, n):
                    ok = False
                    break
            matched.append(ok)
        for ch, bit in enumerate(channelmap):
            if bit >= 0:
                old[ch] = _pin(s, bit)
        if any(matched):
            return i, tuple(matched)
    return None, ()


_FIRST_WINDOW = 256


def search(conds: List[List[Term]], buf: np.ndarray, start: int,
           channelmap: Sequence[int], old: List[int]) -> Tuple[Optional[int], Tuple[bool, ...]]:
    """Vectorized equivalent of search_reference()."""
    n = len(buf)
    if start >= n:
        return None, ()
    if any(sum(t.kind == SKIP for t in c) > 1 for c in conds):
        return search_reference(conds, buf, start, channelmap, old)

    # Pure skip conditions resolve arithmetically, without touching samples.
    if all(len(c) == 1 and c[0].kind in (SKIP, ALWAYS_FALSE) for c in conds):
        hit_at = [start + c[0].count - c[0].skipped if c[0].kind == SKIP else None for c in conds]
        best = min((h for h in hit_at if h is not None), default=None)
        found = best is not None and best < n
        end = best if found else n - 1
        for c in conds:
            if c[0].kind == SKIP:
                c[0].skipped = min(c[0].count, c[0].skipped + (end - start + 1))
        _update_old(buf[end], channelmap, old)
        if not found:
            return None, ()
        return best, tuple(h == best for h in hit_at)

    lo = start
    width = _FIRST_WINDOW
    while lo < n:
        hi = min(n, lo + width)
        idx, matched = _search_window(conds, buf, lo, hi, channelmap, old)
        if idx is not None:
            return idx, matched
        lo = hi
        width *= 4
    return None, ()


def _update_old(sample, channelmap, old):
    s = int(sample)
    for ch, bit in enumerate(channelmap):
        if bit >= 0:
            old[ch] = (s >> bit) & 1


def _search_window(conds, buf, lo, hi, channelmap, old):
    seg = buf[lo:hi]
    m = hi - lo
    cache = {}

    def pins(ch):
        if ch not in cache:
            bit = channelmap[ch]
            new = ((seg >> bit) & 1).astype(np.int8)
            prev = np.empty(m, np.int8)
            prev[0] = old[ch]
            prev[1:] = new[:-1]
            cache[ch] = (prev, new)
        return cache[ch]

    def pin_term(t):
        prev, new = pins(t.channel)
        k = t.kind
        if k == HIGH:
            return new == 1
        if k == LOW:
            return new == 0
        if k == RISING:
            return (prev == 0) & (new == 1)
        if k == FALLING:
            return (prev == 1) & (new == 0)
        if k == EITHER:
            return prev != new
        return prev == new  # NO_EDGE

    results = []
    skip_info = []
    for cond in conds:
        acc = np.ones(m, bool)
        dead = False
        skip = None
        eligible = None
        for t in cond:
            if t.kind == ALWAYS_FALSE:
                dead = True
                break
            if t.kind == SKIP:
                eligible = acc.copy()
                before = np.cumsum(eligible) - eligible   # eligible samples before each one
                hit = eligible & (t.skipped + before >= t.count)
                acc = hit
                skip = t
            else:
                acc = acc & pin_term(t)
        results.append(np.zeros(m, bool) if dead else acc)
        skip_info.append((skip, eligible))
    anym = np.logical_or.reduce(results) if len(results) > 1 else results[0]
    hits = np.flatnonzero(anym)
    end = int(hits[0]) if len(hits) else m - 1
    for skip, eligible in skip_info:
        if skip is not None and eligible is not None:
            skip.skipped = min(skip.count, skip.skipped + int(eligible[:end + 1].sum()))
    _update_old(seg[end], channelmap, old)
    if not len(hits):
        return None, ()
    return lo + end, tuple(bool(r[end]) for r in results)
