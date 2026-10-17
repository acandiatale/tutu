"""Plain helpers (builders, fake LLM) for KB tests."""
from __future__ import annotations

from typing import Optional

from sbmig.core.interfaces import ChatMessage, LLMResponse
from sbmig.core.models import (
    BuildError,
    CaseType,
    DecisionKind,
    ErrorCluster,
    ErrorKind,
    FileChange,
    FixAttempt,
    KBCase,
    Phase,
    TriageDecision,
)

L1 = ("compile|cannot find symbol|class WebSecurityConfigurerAdapter|"
      "package org.springframework.security.config.annotation.web.configuration")
L2 = "compile|cannot_find_symbol|WebSecurityConfigurerAdapter"

SECURITY_SRC = """package com.example.config;

import org.springframework.security.config.annotation.web.configuration.WebSecurityConfigurerAdapter;

public class SecurityConfig extends WebSecurityConfigurerAdapter {
}
"""


class FakeLLM:
    """Scripted LLMClientLike: returns the queued answers in order (last one repeats)."""

    def __init__(self, *answers: str):
        self.answers = list(answers) or ["NONE"]
        self.calls: list[list[ChatMessage]] = []

    def complete(self, messages, *, model: Optional[str] = None, max_tokens: Optional[int] = None,
                 temperature: Optional[float] = None, stop: Optional[list[str]] = None) -> LLMResponse:
        self.calls.append(list(messages))
        ans = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return LLMResponse(content=ans, model="fake")


def make_error(l1: str = L1, l2: str = L2, file: str = "src/main/java/com/example/config/SecurityConfig.java",
               raw: str = "") -> BuildError:
    return BuildError(kind=ErrorKind.COMPILE, message="cannot find symbol", file=file, line=5,
                      symbol="class WebSecurityConfigurerAdapter", signature=l1, signature_generic=l2, raw=raw)


def make_cluster(*errors: BuildError) -> ErrorCluster:
    errs = list(errors) or [make_error()]
    return ErrorCluster(id="c1", kind=errs[0].kind, file=errs[0].file, errors=errs,
                        signatures=sorted({e.signature for e in errs}))


def security_change(path: str = "src/main/java/com/example/config/SecurityConfig.java") -> FileChange:
    return FileChange(path=path, op="modify",
                      search="public class SecurityConfig extends WebSecurityConfigurerAdapter {",
                      replace="public class SecurityConfig {")


def make_case(**kw) -> KBCase:
    data = dict(id="", project_fingerprint="fp-a", case_type=CaseType.SPRING_BOOT, phase=Phase.SPRING,
                signatures=[L1], signatures_generic=[L2], changes=[security_change()],
                summary="replace WebSecurityConfigurerAdapter")
    data.update(kw)
    return KBCase(**data)


def make_attempt(aid: str = "a1", kind: DecisionKind = DecisionKind.LLM_FIX, outcome: str = "improved",
                 applied: bool = True, resolved: Optional[bool] = True, changes=None, errors=None,
                 analysis: str = "Removed WebSecurityConfigurerAdapter base class.\nmore detail",
                 recipe_ids=None, kb_case_ids=None) -> FixAttempt:
    errs = errors if errors is not None else [make_error()]
    return FixAttempt(
        id=aid, phase=Phase.SPRING, iteration=1, cluster_id="c1",
        decision=TriageDecision(cluster_id="c1", kind=kind, recipe_ids=recipe_ids or [],
                                kb_case_ids=kb_case_ids or []),
        changes=[security_change()] if changes is None else changes,
        applied=applied, outcome=outcome, cluster_resolved=resolved, model="fake-model",
        cluster_signatures=sorted({e.signature for e in errs}), cluster_errors=errs, analysis=analysis,
    )


VALID_RECIPE = """type: specs.openrewrite.org/v1beta/recipe
name: com.company.kb.RenameFooToBar
displayName: Rename Foo to Bar
description: Foo moved to Bar in library 2.0.
recipeList:
  - org.openrewrite.java.ChangeType:
      oldFullyQualifiedTypeName: com.lib.Foo
      newFullyQualifiedTypeName: com.lib.Bar
"""


def recipe_yaml(name: str = "com.company.kb.RenameFooToBar") -> str:
    return VALID_RECIPE.replace("com.company.kb.RenameFooToBar", name)


def with_kb(cfg, **kw):
    """Copy of cfg with kb.* fields overridden."""
    return cfg.model_copy(update={"kb": cfg.kb.model_copy(update=kw)})
