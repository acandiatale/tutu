"""실패한 OpenRewrite 실행을 인수인계 줄로 알리는 문구를 만드는 모듈.
- 오케스트레이터가 중단하지 않고 이어 가는 변환 실패에 플래그 ``rewrite-failed``와 이 줄을 남긴다
- 원인 문장에서 비밀 값과 서버 경로를 지운다

Contract: docs/INTERFACES.md "v2.1z-ay". Secrets = URL credentials and the registered secrets of
`sbmig.core.scrub`; the project / run folders become relative, any other absolute path keeps its file name.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from sbmig.core.scrub import scrub

FLAG = "rewrite-failed"
HANDOVER_PREFIX = "the automatic conversion (OpenRewrite) failed — recipe sets not applied: "
DEFAULT_REASON = "OpenRewrite run failed"
MAX_REASON = 300

_USERINFO_RE = re.compile(r"(?P<scheme>\b[a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s@]+@")
_POSIX_ABS_RE = re.compile(r"(?<![\w.:/])/(?:[^\s/]+/)+(?P<name>[^\s/]+)")
_WIN_ABS_RE = re.compile(r"\b[A-Za-z]:\\(?:[^\s\\]+\\)+(?P<name>[^\s\\]+)")


def recipe_set_names(recipe_ids: Iterable[str], cfg: Any) -> list[str]:
    """레시피 id 목록을 설정의 레시피 세트 이름으로 바꾼다. 어느 세트에도 없는 id는 마지막 이름 조각을 쓴다."""
    from sbmig.plan.catalog import config_recipe_sets
    try:
        sets = config_recipe_sets(cfg)
    except Exception:  # noqa: BLE001 - 설정이 없으면 id 이름으로 대신한다
        sets = {}
    out: list[str] = []
    for rid in recipe_ids:
        rid = str(getattr(rid, "id", rid))
        name = next((n for n, ids in sets.items() if rid in ids), None) or rid.rsplit(".", 1)[-1]
        if name not in out:
            out.append(name)
    return out


def clean_reason(error: str, roots: Iterable[str] = ()) -> str:
    """원인 문장의 첫 줄에서 비밀 값과 절대 경로를 지우고 길이를 줄여 반환한다."""
    text = scrub(str(error or "")).strip().splitlines()
    line = text[0].strip() if text else ""
    if not line:
        return DEFAULT_REASON
    for root in sorted((str(r).rstrip("/\\") for r in roots if r), key=len, reverse=True):
        line = line.replace(root + "/", "").replace(root + "\\", "").replace(root, ".")
    line = _USERINFO_RE.sub(lambda m: m.group("scheme") + "***@", line)
    line = _POSIX_ABS_RE.sub(lambda m: "…/" + m.group("name"), line)
    line = _WIN_ABS_RE.sub(lambda m: "…\\" + m.group("name"), line)
    return line if len(line) <= MAX_REASON else line[:MAX_REASON - 1].rstrip() + "…"


def rewrite_failed_followup(sets: Iterable[str], reason: str) -> str:
    """적용하지 못한 레시피 세트와 원인을 담은 인수인계 줄을 반환한다."""
    return (HANDOVER_PREFIX + ", ".join(sets) + f"; reason: {reason} — the files were not converted and the build ran "
            "on the unconverted tree; fix the cause and run the migration again")


__all__ = ["DEFAULT_REASON", "FLAG", "HANDOVER_PREFIX", "clean_reason", "recipe_set_names", "rewrite_failed_followup"]
