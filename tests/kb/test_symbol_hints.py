from __future__ import annotations

from sbmig.core.models import BuildError, ErrorCluster, ErrorKind, FileChange, Phase, PhaseResult
from sbmig.kb import FileKnowledgeBase

from .helpers import make_attempt, with_kb

L1 = "compile|cannot_find_symbol|class JsonComponent|package org.springframework.boot.jackson"
L2 = "compile|cannot_find_symbol|JsonComponent"
MONEY = "src/main/java/com/example/json/Money.java"


def _err(sig: str = L1) -> BuildError:
    return BuildError(
        kind=ErrorKind.COMPILE,
        message="cannot find symbol",
        file=MONEY,
        line=3,
        symbol="class JsonComponent",
        signature=sig,
        signature_generic=L2,
    )


def _attempt(aid: str):
    changes = [
        FileChange(
            path=MONEY,
            op="modify",
            search="import org.springframework.boot.jackson.JsonComponent;",
            replace="import org.springframework.boot.jackson.JacksonComponent;",
        ),
        FileChange(
            path=MONEY,
            op="modify",
            search="@JsonComponent\nclass Money {",
            replace="@JacksonComponent\nclass Money {",
        ),
    ]
    return make_attempt(aid, changes=changes, errors=[_err()], analysis="renamed")


def test_symbol_hint_first(kb, ws, profile):
    kb.harvest(PhaseResult(phase=Phase.SPRING, attempts=[_attempt("a")]), profile, ws)
    # a different L1 (other location) — same symbol: the case is found via L2, the symbol hint comes first
    cluster = ErrorCluster(
        id="c",
        kind=ErrorKind.COMPILE,
        errors=[_err("compile|cannot_find_symbol|other")],
        signatures=["compile|cannot_find_symbol|other"],
    )
    m = kb.match(cluster, ws, profile)
    assert m.hints[0] == (
        "[symbol JsonComponent] fixed in 1 project(s) by annotation-replace, type-rename: "
        "org.springframework.boot.jackson.JsonComponent → "
        "org.springframework.boot.jackson.JacksonComponent "
        "(recipe JsonComponentToJacksonComponent)"
    )
    assert m.hints[1].startswith("[case ")
    assert m.candidate_recipe_ids == ["JsonComponentToJacksonComponent"]


def test_symbol_hint_without_signature_match(kb, ws, profile):
    kb.harvest(PhaseResult(phase=Phase.SPRING, attempts=[_attempt("a")]), profile, ws)
    e = BuildError(
        kind=ErrorKind.TEST_COMPILE,
        message="cannot find symbol",
        symbol="class JsonComponent",
        signature="test_compile|cannot_find_symbol|x",
        signature_generic="test_compile|cannot_find_symbol|y",
    )
    m = kb.match(ErrorCluster(id="c", kind=ErrorKind.TEST_COMPILE, errors=[e]), ws, profile)
    assert m.cases == [] and len(m.hints) == 1 and m.hints[0].startswith("[symbol JsonComponent] fixed in 1")


def test_symbol_hints_respect_max_hints_and_switches(kb_cfg, ws, profile):
    kb = FileKnowledgeBase(with_kb(kb_cfg, max_hints=1))
    kb.harvest(PhaseResult(phase=Phase.SPRING, attempts=[_attempt("a")]), profile, ws)
    cluster = ErrorCluster(id="c", kind=ErrorKind.COMPILE, errors=[_err()], signatures=[L1])
    m = kb.match(cluster, ws, profile)
    assert len(m.hints) == 1 and m.hints[0].startswith("[symbol JsonComponent]")
    off = FileKnowledgeBase(with_kb(kb_cfg, symbol_index=False))
    assert all(not h.startswith("[symbol") for h in off.match(cluster, ws, profile).hints)
    no_hints = FileKnowledgeBase(with_kb(kb_cfg, use_hints=False))
    assert no_hints.match(cluster, ws, profile).hints == []
