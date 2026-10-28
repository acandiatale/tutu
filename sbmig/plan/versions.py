"""작은 버전 비교 도구 — 숫자 마디를 숫자로 비교한다(1.18.24
< 1.18.30, 3.8.1 < 3.13.0, 2.7.18.RELEASE == 2.7.18).
흐름상 위치: 권고의 버전 하한 판정(``sbmig.advisory.evaluate.below``) 등에서 쓴다. ``${...}`` 참조나 숫자로
시작하지 않는 버전은 None(모름) — 호출한 쪽이 모를 때의 처리를 정한다.

Tiny semver-ish version comparison: 1.18.24 < 1.18.30, 3.8.1 < 3.13.0, 2.7.18.RELEASE == 2.7.18."""
from __future__ import annotations

import re
from typing import Optional

# 수식어 순서: snapshot < alpha < beta < milestone < rc < (없음 = GA = RELEASE) < sp
_QUALIFIER_ORDER = {"alpha": -4, "a": -4, "beta": -3, "b": -3, "milestone": -2, "m": -2, "rc": -1, "cr": -1,
                    "snapshot": -5, "": 0, "ga": 0, "final": 0, "release": 0, "jre": 0, "android": 0, "sp": 1}


def parse_version(v: Optional[str]) -> Optional[tuple[tuple[int, ...], int]]:
    """버전 문자열 → (숫자 마디 튜플, 수식어 순위). 비었거나 ``${...}`` 이거나 숫자가 아니면 None.

    ('1.18.24') -> ((1, 18, 24), 0). Returns None for empty/unresolved (`${...}`) or non-numeric versions."""
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
    """비교 결과 -1 / 0 / 1. 짧은 쪽은 0 으로 채워 비교한다(3.1 == 3.1.0). 한쪽이라도 못 읽으면 None.

    -1 / 0 / 1, or None when either side cannot be parsed."""
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
    """``a < b`` 인가. 못 읽으면 None(모름)."""
    c = compare_versions(a, b)
    return None if c is None else c < 0
