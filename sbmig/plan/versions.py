"""Tiny semver-ish version comparison: 1.18.24 < 1.18.30, 3.8.1 < 3.13.0, 2.7.18.RELEASE == 2.7.18."""
from __future__ import annotations

import re
from typing import Optional

_QUALIFIER_ORDER = {"alpha": -4, "a": -4, "beta": -3, "b": -3, "milestone": -2, "m": -2, "rc": -1, "cr": -1,
                    "snapshot": -5, "": 0, "ga": 0, "final": 0, "release": 0, "jre": 0, "android": 0, "sp": 1}


def parse_version(v: Optional[str]) -> Optional[tuple[tuple[int, ...], int]]:
    """('1.18.24') -> ((1, 18, 24), 0). Returns None for empty/unresolved (`${...}`) or non-numeric versions."""
    if not v or "${" in v:
        return None
    v = v.strip()
    m = re.match(r"^v?(\d+(?:\.\d+)*)(.*)$", v)
    if not m:
        return None
    nums = tuple(int(x) for x in m.group(1).split("."))
    rest = m.group(2).lstrip(".-_").lower()
    qm = re.match(r"^([a-z]*)", rest)
    q = qm.group(1) if qm else ""
    qual = _QUALIFIER_ORDER.get(q, 0)
    return nums, qual


def compare_versions(a: Optional[str], b: Optional[str]) -> Optional[int]:
    """-1 / 0 / 1, or None when either side cannot be parsed."""
    pa, pb = parse_version(a), parse_version(b)
    if pa is None or pb is None:
        return None
    na, nb = pa[0], pb[0]
    width = max(len(na), len(nb))
    na = na + (0,) * (width - len(na))
    nb = nb + (0,) * (width - len(nb))
    if na != nb:
        return -1 if na < nb else 1
    if pa[1] != pb[1]:
        return -1 if pa[1] < pb[1] else 1
    return 0


def version_lt(a: Optional[str], b: str) -> Optional[bool]:
    c = compare_versions(a, b)
    return None if c is None else c < 0
