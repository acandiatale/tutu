"""2026-10-23 integration: an LLM text-replace fix recorded in project A becomes a `replace` case in the catalog;
in project B the real `case-replace` handler (found through the real HandlerRegistry) applies it without the LLM,
only on the reported line; after successes in two projects the case is promoted to `active`."""
from __future__ import annotations

from pathlib import Path

from sbmig.core.config import load_config
from sbmig.core.models import (BuildError, CaseType, ErrorAttemptRef, ErrorCaseRecord, ErrorCluster, ErrorKind, ProjectProfile,
                               ReplacePair)
from sbmig.core.workspace import Workspace
from sbmig.kb import FileKnowledgeBase
from sbmig.kb.catalog import case_id_for
from sbmig.triage.handlers import HandlerRegistry
from sbmig.triage.handlers.case_replace import clear_catalog_cache

from .helpers import V1_KB_FLAGS

REPO = Path(__file__).resolve().parents[2]
L2 = "compile|cannot_find_symbol|clientIp"
L1 = "compile|cannot_find_symbol|method clientIp(HttpServletRequest)|class com.company.legacy.web.LegacyRequestHelper"
SRC = """package com.example.web;

import com.company.legacy.web.LegacyRequestHelper;

public class Filter {
    String a(Object r) { return LegacyRequestHelper.clientIp(r); }
    String clientIpCache = "clientIp";   // same word elsewhere: must stay
}
"""


def _cfg(kb_dir: Path):
    # v1 path (handler case-replace + learned replaces): its switches are off by default since v2
    return load_config(REPO / "config" / "artifacts.local-e2e.yml", repo_root=REPO,
                       overrides={"kb": {"dir": str(kb_dir), "auto_promote_after_projects": 2, **V1_KB_FLAGS}})


def _record(project: str, run: str, *, method: str, resolver: str, status: str = "resolved", llm_replace: bool = False,
            failed_first: bool = False) -> ErrorCaseRecord:
    attempts = []
    if failed_first:
        attempts.append(ErrorAttemptRef(attempt_id=f"{run}:spring-1-1", fix_method="llm", outcome="no_change",
                                        failure="the targeted error(s) were still there after the rebuild"))
    attempts.append(ErrorAttemptRef(attempt_id=f"{run}:spring-2-1", fix_method=method, resolver_id=resolver,
                                    outcome="improved" if status == "resolved" else "no_change",
                                    change_kind="text_replace"))
    return ErrorCaseRecord(
        id=f"{run}-e1", project=project, project_fingerprint=project, run_id=run, error_kind=ErrorKind.COMPILE,
        message="cannot find symbol", file="src/main/java/com/example/web/Filter.java", line=6,
        symbol="method clientIp(HttpServletRequest)", location="class com.company.legacy.web.LegacyRequestHelper",
        signature=L1, signature_generic=L2, error_symbol="clientIp", status=status, fix_method=method,
        resolver_id=resolver, resolved_by=attempts[-1].attempt_id if status == "resolved" else None,
        targeted=True, change_kind="text_replace", replace_pairs=[ReplacePair(old="clientIp", new="remoteClientIp")],
        replace_rule_candidate=llm_replace, attempts=attempts, attempt_count=len(attempts),
        case_id=case_id_for(L2))


def test_learned_replace_is_applied_by_the_handler_and_promoted(tmp_path):
    clear_catalog_cache()
    cfg = _cfg(tmp_path / "kb")
    kb = FileKnowledgeBase(cfg)
    # project A: the LLM fixed it with a pure text replace, after one failed attempt
    kb.append_error_cases([_record("project-a", "run-a", method="llm", resolver="gpt", llm_replace=True,
                                   failed_first=True)], [])
    case = kb.catalog().get(L2)
    assert case is not None and case.id == case_id_for(L2)
    assert case.action.kind == "replace" and case.status == "candidate"
    assert [(p.old, p.new) for p in case.action.replace_pairs] == [("clientIp", "remoteClientIp")]
    assert any("llm:no_change" in a for e in case.evidence.timeline for a in e.attempts)
    assert (tmp_path / "kb" / "views" / "cases.md").is_file()

    # project B: the same error → the real registry picks case-replace (no LLM) and it edits only the reported line
    src = tmp_path / "proj"
    (src / "src/main/java/com/example/web").mkdir(parents=True)
    (src / "src/main/java/com/example/web/Filter.java").write_text(SRC, encoding="utf-8")
    ws = Workspace.from_input(src, tmp_path / "out", run_id="run-b")
    err = BuildError(kind=ErrorKind.COMPILE, message="cannot find symbol",
                     file="src/main/java/com/example/web/Filter.java", line=6, signature=L1, signature_generic=L2,
                     symbol="method clientIp(HttpServletRequest)")
    cluster = ErrorCluster(id="compile:x", kind=ErrorKind.COMPILE, file=err.file, errors=[err], signatures=[L1])
    profile = ProjectProfile(root=str(ws.root), name="project-b", case_type=CaseType.SPRING_BOOT)
    reg = HandlerRegistry.default(cfg)
    matching = [h.id for h in reg.matching(cluster, profile, cfg)]
    assert matching == ["case-replace"]
    changes = reg.get("case-replace").apply(ws, cluster, profile, cfg)
    assert [c.path for c in changes] == ["src/main/java/com/example/web/Filter.java"]
    text = ws.read("src/main/java/com/example/web/Filter.java")
    assert "LegacyRequestHelper.remoteClientIp(r)" in text
    assert 'String clientIpCache = "clientIp";' in text                    # not a reported line: untouched

    # a symbol-only hit (same symbol, another error) never triggers the replace
    other = err.model_copy(update={"signature": "compile|cannot_find_symbol|variable clientIp|class *",
                                   "signature_generic": "compile|cannot_find_symbol|variable clientIp"})
    other_cluster = cluster.model_copy(update={"errors": [other], "signatures": [other.signature]})
    assert "case-replace" not in [h.id for h in reg.matching(other_cluster, profile, cfg)]

    # projects B and C: case-replace resolved it → promotion after 2 distinct projects, no failure
    kb.append_error_cases([_record("project-b", "run-b", method="handler", resolver="case-replace")], [])
    assert kb.catalog().get(L2).status == "candidate"
    kb.append_error_cases([_record("project-c", "run-c", method="handler", resolver="case-replace")], [])
    promoted = kb.catalog().get(L2)
    assert promoted.status == "active" and promoted.evidence.action_successes == 2
    assert sorted(promoted.evidence.action_projects) == ["project-b", "project-c"]
    clear_catalog_cache()
