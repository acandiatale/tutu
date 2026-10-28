"""sbmig.kb — 파일 지식베이스(KB, v2.1)의 패키지 입구.
오케스트레이터·플릿·웹 서버가 드는 창구(``FileKnowledgeBase``), ``sbmig kb`` CLI(``kb_cli``), KB 쓰기가 거치는
말단 도구(잠금·마스킹·jsonl)를 내보낸다. 공개 이름은 처음 쓰일 때 불러온다(모듈 ``__getattr__``):
``sbmig.errors`` 가 ``sbmig.kb.redact`` / ``sbmig.kb.lock`` 을 import 해도 store·cli(→ ``sbmig.errors``)까지
끌려와 순환 import 가 생기지 않게 하려는 것이다. KB 는 파일뿐이다(DB 금지, 불변조건 3). 테스트·데모 실행은
``out/`` 아래 KB 에만 쓴다(불변조건 6).

sbmig.kb — the file-based knowledge base, v2.1 (docs/v2/PLAN-v2.1.md §3.4): the facade the orchestrator, the
fleet and the web server hold, the ``sbmig kb`` CLI, and the leaf helpers the KB writes go through.

Public API (loaded lazily, so ``sbmig.kb.redact`` / ``sbmig.kb.lock`` stay leaf imports for :mod:`sbmig.errors`):
    FileKnowledgeBase(cfg, kb_dir=None)    facade over sbmig.errors.CaseStore: cases, match, record_run, reindex,
                                           stats, validate, export_zip / import_zip, lock
    kb_cli                                 typer sub-app, mounted by sbmig/cli.py as `sbmig kb`
    case_id_for(l2)                        sha1(l2)[:10] (sbmig.errors.common)
    redact(text, cfg=None)                 secret VALUES masked by context (sbmig.kb.redact, round 10)
    kb_lock(kb_dir, timeout)               the cross-process write lock <kb>/.lock (sbmig.kb.lock)
    project_name(profile, ws) / changes_as_diff(changes) / rel_posix(path, ws)   record helpers (sbmig.kb.common)


"""
from __future__ import annotations

import importlib
from typing import Any

_EXPORTS = {
    "FileKnowledgeBase": ("sbmig.kb.store", "FileKnowledgeBase"),
    "atomic_write": ("sbmig.kb.store", "atomic_write"),
    "kb_cli": ("sbmig.kb.cli", "kb_cli"),
    "case_id_for": ("sbmig.errors.common", "case_id_for"),
    "case_id_of": ("sbmig.errors.common", "case_id_of"),
    "CaseStore": ("sbmig.errors.cases", "CaseStore"),
    "redact": ("sbmig.kb.redact", "redact"),
    "kb_lock": ("sbmig.kb.lock", "kb_lock"),
    "project_name": ("sbmig.kb.common", "project_name"),
    "changes_as_diff": ("sbmig.kb.common", "changes_as_diff"),
    "rel_posix": ("sbmig.kb.common", "rel_posix"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    """지연 로드: 처음 쓰일 때 해당 모듈을 import 하고 globals 에 넣어 둔다(다음부터는 바로 찾음)."""
    try:
        module, attr = _EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module 'sbmig.kb' has no attribute {name!r}") from None
    value = getattr(importlib.import_module(module), attr)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))
