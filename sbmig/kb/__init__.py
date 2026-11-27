"""파일 KB 패키지의 진입점 모듈. KB 파사드, ``sbmig kb`` CLI, 잠금과 마스킹 같은 도구를 내보낸다.

Public names are imported on first access, so ``sbmig.errors`` importing ``sbmig.kb.redact`` /
``sbmig.kb.lock`` creates no import cycle. The KB is files only (no database). Tests and demo runs write only
to KBs under ``out/``.

Public API (``_EXPORTS``, loaded on first access):
    FileKnowledgeBase(cfg, kb_dir=None)    facade over sbmig.errors.CaseStore: cases, match, record_run, reindex,
                                           stats, validate, export_zip / import_zip, lock
    kb_cli                                 typer sub-app, mounted by sbmig/cli.py as `sbmig kb`
    case_id_for(l2)                        sha1(l2)[:10] (sbmig.errors.common)
    redact(text, cfg=None)                 secret VALUES masked by context (sbmig.kb.redact)
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
    """이름을 처음 쓸 때 그 모듈을 import하고, 값을 globals에 저장해 둔다.
    ``_EXPORTS``에 없는 이름이면 AttributeError를 던진다."""
    try:
        module, attr = _EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module 'sbmig.kb' has no attribute {name!r}") from None
    value = getattr(importlib.import_module(module), attr)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))
