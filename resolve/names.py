"""Company-name normalisation and fuzzy lookup.

Normalisation removes what systems legitimately disagree on (legal suffix, case, punctuation, leading 'The',
'&' vs 'and', common abbreviations). It must NOT remove what distinguishes two different companies, so
'Amber Bridge Labs' and 'Amber Bridge Systems' keep different keys (they are different companies; see the
name-twin guard in resolve.py).
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from difflib import SequenceMatcher

LEGAL = {"inc", "llc", "ltd", "corp", "co", "gmbh", "pvt", "plc", "b", "v", "bv", "company", "corporation", "limited"}
ABBR = {"tech": "technologies", "sol": "solutions", "intl": "international"}
WS_SUFFIX = re.compile(r"\s*(\(staging\)|-\s*team\s+b)\s*$", re.I)     # extra product workspaces of the same company


def tokens(name: str | None) -> list[str]:
    if not name:
        return []
    s = WS_SUFFIX.sub("", str(name)).lower().replace("&", " and ")
    s = re.sub(r"[.,'()\-_]", " ", s)
    toks = [t for t in s.split() if t]
    if toks and toks[0] == "the":
        toks = toks[1:]
    return [t for t in (ABBR.get(t, t) for t in toks) if t not in LEGAL]


def name_key(name: str | None) -> str:
    return " ".join(tokens(name))


def slug_key(name: str | None) -> str:
    """Spaces removed: product workspaces are often a slug of the company name ('amberbridgelabs')."""
    return "".join(tokens(name))


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def edit_distance(a: str, b: str, cap: int = 3) -> int:
    """Optimal-string-alignment distance: insert/delete/substitute/transpose-adjacent each cost 1.
    Detects typos ('Stack' vs 'Satck') without treating 'Labs' vs 'Systems' as close."""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev2, prev = None, list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        prev2, prev = prev, cur
    return prev[-1]


def trigrams(s: str) -> set[str]:
    s = f"  {s} "
    return {s[i:i + 3] for i in range(len(s) - 2)}


class NameIndex:
    """Exact lookup by key / slug, plus a trigram-blocked fuzzy lookup (avoids comparing against every account)."""

    def __init__(self, ids: list[str], names: list[str]):
        self.names = dict(zip(ids, names))
        self.keys = {i: name_key(n) for i, n in self.names.items()}
        self.by_key: dict[str, list[str]] = defaultdict(list)
        self.by_slug: dict[str, list[str]] = defaultdict(list)
        self.tri: dict[str, set[str]] = defaultdict(set)
        for i, k in self.keys.items():
            self.by_key[k].append(i)
            self.by_slug[k.replace(" ", "")].append(i)
            for g in trigrams(k):
                self.tri[g].add(i)

    def exact(self, name: str | None) -> list[str]:
        return list(self.by_key.get(name_key(name), []))

    def exact_slug(self, name: str | None) -> list[str]:
        return list(self.by_slug.get(slug_key(name), []))

    def fuzzy(self, name: str | None, top: int = 3, min_shared: int = 4) -> list[tuple[float, str]]:
        k = name_key(name)
        if not k:
            return []
        hits = Counter()
        for g in trigrams(k):
            for i in self.tri.get(g, ()):
                hits[i] += 1
        scored = [(similarity(k, self.keys[i]), i) for i, c in hits.most_common(12) if c >= min_shared]
        return sorted(scored, reverse=True)[:top]
