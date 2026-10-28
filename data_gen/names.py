"""Company / person name generation and the 'messy variant' helpers."""
from __future__ import annotations

import re

import numpy as np

ADJ = ("Amber Apex Arctic Aspen Atlas Aurora Beacon Birch Bold Bright Cedar Cobalt Copper Coral "
       "Crimson Crystal Delta Dune Eagle Ember Evergreen Falcon Fern Flint Forge Frontier Garnet "
       "Glacier Golden Granite Harbor Hazel Horizon Indigo Iron Ivory Jade Juniper Keystone "
       "Lagoon Lark Lumen Maple Meridian Mesa Mint Mosaic Nimbus Noble North Oak Onyx Orbit "
       "Osprey Pacific Pine Pinnacle Polar Prairie Quartz Radiant Raven Redwood Ridge River Sable "
       "Sage Sierra Silver Slate Solstice Summit Sunrise Tidal Timber Topaz Trident Union Vale "
       "Velvet Verdant Vista Willow Zenith").split()
NOUN = ("Bridge Canyon Circuit Compass Current Dock Field Gate Grove Hub Junction Keep Lane Ledger "
        "Loop Mill Node Orchard Path Peak Pier Point Port Quarry Rail Reef Road Shore Signal Spire "
        "Spring Stack Stone Street Tower Trail Vault Vector View Ward Wharf Works Yard Foundry "
        "Harvest Lantern Anchor Beam Cascade Crest").split()
SUFFIX = ("Systems Labs Technologies Group Partners Industries Solutions Holdings Networks Dynamics "
          "Analytics Logistics Health Capital Foods Studios Robotics Energy Media Retail Freight "
          "Pharma Motors Consulting Software Supply").split()
LEGAL = ("Inc.", "LLC", "Ltd", "Corp.", "Co.", "GmbH", "Pvt Ltd", "PLC", "B.V.")
TLDS = (("com", .70), ("io", .14), ("co", .07), ("net", .04), ("ai", .03), ("org", .02))
INDUSTRIES = ("Software", "Healthcare", "Financial Services", "Manufacturing", "Retail", "Logistics",
              "Education", "Media", "Energy", "Professional Services", "Telecommunications",
              "Real Estate", "Hospitality", "Nonprofit", "Construction")
FIRST = ("Aarav Aditi Alex Ananya Arjun Ben Carlos Chen Chloe Daniel Deepa Diego Elena Emma Eric "
         "Fatima Gabriel Grace Hannah Ishaan Isabel Jack Jamal Jasmine Kavya Kenji Laura Leo Liam "
         "Maria Marcus Meera Mia Mohammed Nadia Neha Nikhil Noah Olivia Omar Priya Rahul Raj Rohan "
         "Sara Sam Sanjay Sofia Tara Thomas Uma Victor Vikram Wei Yuki Zoe").split()
LAST = ("Agarwal Ahmed Anderson Banerjee Bose Brown Chatterjee Chen Clark Das Davis Dutta Evans "
        "Fernandez Garcia Ghosh Gupta Hall Hughes Iyer Jain Johnson Kapoor Kim Kumar Lee Lopez "
        "Mehta Miller Mitra Moore Mukherjee Murphy Nair Nguyen Patel Perez Rao Reddy Roberts "
        "Roy Sanchez Sharma Singh Smith Sinha Taylor Thomas Verma Walker Wang White Wilson Wright "
        "Young").split()
PERSONAL = ("gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "proton.me")


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


class NameFactory:
    def __init__(self, rng: np.random.Generator):
        self.rng = rng
        self.used_names: set[str] = set()
        self.used_domains: set[str] = set()

    def _domain(self, base: str) -> str:
        r = self.rng
        tld = TLDS[int(r.choice(len(TLDS), p=[w for _, w in TLDS]))][0]
        d = f"{slug(base)}.{tld}"
        n = 2
        while d in self.used_domains:
            d = f"{slug(base)}{n}.{tld}"
            n += 1
        self.used_domains.add(d)
        return d

    def company(self) -> tuple[str, str]:
        r = self.rng
        while True:
            a, n, s = r.choice(ADJ), r.choice(NOUN), r.choice(SUFFIX)
            form = r.random()
            base = f"{a} {n}" if form < .45 else (f"{a} {s}" if form < .75 else f"{a} {n} {s}")
            if base not in self.used_names:
                self.used_names.add(base)
                return base, self._domain(base)

    def twin(self, base: str) -> tuple[str, str]:
        """A *different* company whose name nearly collides with `base` (fuzzy-match trap)."""
        r = self.rng
        words = base.split()
        stem = " ".join(words[:2]) if len(words) > 2 else words[0] + " " + r.choice(NOUN)
        for _ in range(50):
            cand = f"{stem} {r.choice(SUFFIX)}"
            if cand not in self.used_names:
                self.used_names.add(cand)
                return cand, self._domain(cand)
        return self.company()

    def person(self) -> tuple[str, str]:
        return str(self.rng.choice(FIRST)), str(self.rng.choice(LAST))


def legal_name(rng: np.random.Generator, base: str) -> str:
    return f"{base} {rng.choice(LEGAL)}" if rng.random() < .7 else base


def _typo(rng, s: str) -> str:
    if len(s) < 5:
        return s
    i = int(rng.integers(1, len(s) - 2))
    return s[:i] + s[i + 1] + s[i] + s[i + 2:]


def messy_variant(rng: np.random.Generator, base: str, legal: str) -> str:
    """How another system might spell the same company."""
    r = rng.random()
    if r < .30:
        return legal
    if r < .50:
        return base
    if r < .62:
        return legal.upper()
    if r < .72:
        return "The " + base
    if r < .82:
        return re.sub(r"[.,]", "", legal)
    if r < .90:
        return base.replace("Technologies", "Tech").replace("Solutions", "Sol.").replace(" and ", " & ")
    if r < .95:
        return _typo(rng, legal)
    return base.lower()


def person_email(rng, first: str, last: str, domain: str) -> str:
    r = rng.random()
    f, l = first.lower(), last.lower()
    if r < .6:
        return f"{f}.{l}@{domain}"
    if r < .8:
        return f"{f[0]}{l}@{domain}"
    if r < .92:
        return f"{f}@{domain}"
    return f"{f}{l[0]}@{domain}"
