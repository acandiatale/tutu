"""레시피가 바꾼 as-is ``provided`` scope를 되돌리는 모듈.
as-is에서 provided였던 의존성과 그 jakarta 후속 좌표를 다시 provided로 바꾼다.

Works without the restructure context: a reduced run (scope jdk-jakarta) has none and calls it from the orchestrator.

The OpenRewrite jakarta recipe turns ``javax.servlet-api`` provided into ``jakarta.servlet-api`` compile.
The restructure calls this at the end of its work (``LegacyContextMixin._lc_restore_scopes``), a reduced run
after every ``transform.rewrite``. A dependency that is already provided is left alone, so repeated calls
give the same result.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .pom_editor import PomEditError, PomEditor

SCOPE_NOTE_PREFIX = "as-is provided scopes restored after the recipes: "
_KEEP_SCOPES = ("provided", "test", "system", "import")


@dataclass
class ScopeRestore:
    """scope를 되돌린 항목, 인수인계 문장, 저장한 pom 경로를 담는 클래스.

    ``restored``: "g:a: <old> → provided (as-is scope, <pom>)" entries; ``followups``: the switch-off sentences;
    ``changed``: workspace-relative poms written."""
    restored: list[str] = field(default_factory=list)
    followups: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)


def asis_provided_keys(poms: Iterable[Any], successors: Mapping[str, str]) -> dict[str, str]:
    """as-is pom에서 provided로 선언된 g:a와 그 jakarta 후속 g:a를, 선언한 pom 경로와 함께 반환한다.
    다른 as-is pom이 compile이나 runtime으로 선언한 g:a는 뺀다. 그쪽 scope를 따른다.

    ``successors`` maps an as-is g:a to its jakarta g:a (``scope_successors``); both keys get the same pom."""
    provided: dict[str, str] = {}
    elsewhere: set[str] = set()
    for p in poms:
        for d in getattr(p, "dependencies", None) or []:
            if not (d.group_id and d.artifact_id):
                continue
            keys = [d.key, *([successors[d.key]] if d.key in successors else [])]
            if (d.scope or "") == "provided":
                for k in keys:
                    provided.setdefault(k, p.path)
            elif (d.scope or "compile") in ("compile", "runtime"):
                elsewhere.update(keys)
    return {k: v for k, v in provided.items() if k not in elsewhere}


def restore_asis_scopes(ws: Any, asis_poms: Iterable[Any], pom_paths: Iterable[str], successors: Mapping[str, str],
                        *, on: bool = True) -> ScopeRestore:
    """as-is에서 provided였는데 지금 compile이나 runtime인 의존성을 ``pom_paths``의 pom에서 provided로 되돌린다.
    ``on``이 False면 pom은 고치지 않고 인수인계 문장만 만든다. 읽을 수 없는 pom은 건너뛴다.

    Scopes in ``_KEEP_SCOPES`` (provided / test / system / import) are never touched. Raises OSError / PomEditError
    only from a save."""
    out = ScopeRestore()
    provided = asis_provided_keys(asis_poms, successors)
    if not provided:
        return out
    for rel in pom_paths:
        try:
            ed = PomEditor(ws.abs(rel))
        except PomEditError:
            continue
        for el in ed.dependency_elements(False):
            g = ed.gav_of(el)
            scope = (g.scope or "compile").strip()
            if g.key not in provided or scope in _KEEP_SCOPES:
                continue
            if not on:
                fu = (f"{rel}: {g.key} is {scope} although the as-is declaration was provided "
                      "(layout.legacy_context.restore_asis_scopes: false) — the container provides it")
                if fu not in out.followups:
                    out.followups.append(fu)
                continue
            ed.set_child_text(el, "scope", "provided")
            entry = f"{g.key}: {scope} → provided (as-is scope, {rel})"
            if entry not in out.restored:
                out.restored.append(entry)
        if ed.changed:
            ed.save()
            out.changed.append(ws.rel(ed.path))
    return out


def project_pom_paths(ws: Any, base: str = "") -> list[str]:
    """루트 pom을 먼저 두고 ``base`` 아래의 다른 프로젝트 pom을 이어서 반환한다. ``src/`` 아래 pom은 뺀다.

    The same list as ``Restructurer._project_poms``; poms under src/ are test data."""
    base = base.strip("/")
    root = f"{base}/pom.xml" if base else "pom.xml"
    poms = [p for p in ws.pom_files() if "/src/" not in f"/{p}" and (not base or p.startswith(base + "/")) and p != root]
    return ([root] if ws.exists(root) else []) + poms


def scope_note(entries: Iterable[str]) -> str:
    """되돌린 scope 항목으로 단계 메모 한 줄을 만든다.

    The one note text of both flows: ``legacy_apply`` (restructure) and ``Orchestrator`` (reduced run) call it."""
    return SCOPE_NOTE_PREFIX + "; ".join(entries)


__all__ = ["SCOPE_NOTE_PREFIX", "ScopeRestore", "asis_provided_keys", "project_pom_paths", "restore_asis_scopes",
           "scope_note"]
