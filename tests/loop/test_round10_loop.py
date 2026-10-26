"""Round 10 (2026-10-26) loop items: A6 LLM failure classification + "same HTTP 400 for different prompts" = a
configuration error; A9 a pinned `block` case reaches the BlockedItem; A12 the watchdog stays silent while a Maven
build / an LLM call heartbeats by itself; A8 test-skip fallbacks are per build target."""
from __future__ import annotations

import logging
import time

import pytest

from sbmig.core.models import (BlockReason, BuildError, CaseAction, CaseHit, ErrorKind, FixProposal, KBMatch,
                               LLMUsage, Phase)
from sbmig.loop import fixloop as fl
from sbmig.loop.fixloop import is_llm_transport_error, llm_400_body

from .fakes import FakeBuildRunner, FakeFixer, FakeKB, bad_build, journal_events, ok_build, write_fix
from .test_fixloop import make_loop

FOO = "src/main/java/com/x/Foo.java"
BAR = "src/main/java/com/x/Bar.java"
GONE = "compile|cannot_find_symbol|class Gone"


def l1(file: str) -> BuildError:
    """File-less L1, the case (L2) per file: Foo and Bar are two cases."""
    return BuildError(kind=ErrorKind.COMPILE, message="cannot find symbol", file=file, line=3, module="demo",
                      symbol="class Gone", signature=GONE,
                      signature_generic="compile|cannot_find_symbol|" + file.rsplit("/", 1)[-1])


def files_build(w, call):
    errs = [l1(f) for f in (FOO, BAR) if "broken" in w.read(f)]
    return bad_build(*errs) if errs else ok_build()


# ------------------------------------------------------------------------------------------------ A6
@pytest.mark.parametrize("analysis,transport", [
    # no longer transport: words / numbers that merely look like it
    ("llm-error: PermissionError: [WinError 32] the process cannot access C:\\sbmig\\config\\x.yml", False),
    ("llm-error: KeyError: 'line 512 of the config'", False),
    ("llm-error: ValueError: could not connect the dots in /home/me/connection/http.txt", False),
    # explicit markers
    ("llm-error: LLM request failed after 4 attempt(s) (status=None) cause=ConnectionError: Failed to establish "
     "a new connection: [WinError 10061] ?", True),
    ("llm-error: LLM request failed after 2 attempt(s) (status=None, cause=ConnectTimeout): ConnectTimeout: x", True),
    ("llm-error: LLM request failed (status=401): unauthorized", True),
    ("llm-error: LLM request failed (status=403): forbidden", True),
    ("llm-error: LLM request failed (status=404): no such route", True),
    ("llm-error: LLM request failed after 4 attempt(s) (status=429): slow down", True),
    ("llm-error: ReadTimeout: HTTPSConnectionPool(host='gw'): Read timed out", True),
    ("llm-error: LLMHardDeadline: LLM call exceeded the hard deadline of 600s", True),
    ("llm-error: LLMConfigError: LLM_BASE_URL is not set (.env or environment: the full gateway URL)", True),
    ("llm-error: llm.base_url is not set (LLM_BASE_URL)", True),
    ("llm-error: LLM adapter Gw failed (status=None, cause=RuntimeError): boom", True),
    # the token limit cut an empty answer: the prompt is too big, not an outage
    ("llm-error: empty answer with finish_reason=length: request rejected by its size", False),
    ("llm-error: LLM request failed (status=400): {\"error\": \"unsupported model\"}", False),
])
def test_transport_classification_round10(analysis, transport):
    assert is_llm_transport_error(analysis) is transport


def test_400_body_extraction():
    assert llm_400_body('llm-error: LLM request failed (status=400): {"error":  "unsupported model x"}') == \
        '{"error": "unsupported model x"}'
    assert llm_400_body('llm-error: LLM request failed (status=400): {"error":"maximum context length is 8192"}') \
        is None                                                     # too big: never a configuration error
    assert llm_400_body("llm-error: LLM request failed (status=503): busy") is None
    assert llm_400_body("fixed it") is None


def test_same_400_for_two_prompts_is_a_configuration_error(cfg, ws, profile):
    calls: list[str] = []

    def fix(w, c, ctx):
        calls.append(c.errors[0].file)
        return FixProposal(changes=[], model="m", prompt_hash=f"h-{c.errors[0].file}-{len(calls)}",
                           analysis='llm-error: LLM request failed (status=400): {"error": "unsupported model #m"}',
                           llm_usage=LLMUsage(model="m", calls=1))

    loop = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=files_build), fixer=FakeFixer(fn=fix))
    pr = loop.run(Phase.SPRING, "compile", 4)
    assert len(calls) == 2                                          # no third LLM call in the run
    assert "llm-config-error" in loop.flags and loop.llm_config_error == '{"error": "unsupported model #m"}'
    assert loop.llm_unavailable and "HTTP 400" in loop.llm_unavailable
    fu = [f for f in loop.followups if "same HTTP 400" in f]
    assert len(fu) == 1 and "unsupported model #m" in fu[0]
    assert {b.reason for b in pr.blocked} == {BlockReason.LLM_UNAVAILABLE}
    assert [e for e in journal_events(ws) if e["event"] == "llm_config_error"]


def test_one_400_is_only_a_failed_attempt(cfg, ws, profile):
    n = {"i": 0}

    def fix(w, c, ctx):
        n["i"] += 1
        if n["i"] == 1:
            return FixProposal(changes=[], model="m", prompt_hash="h1", llm_usage=LLMUsage(model="m", calls=1),
                               analysis='llm-error: LLM request failed (status=400): {"error": "bad tool call"}')
        return write_fix(c.errors[0].file, "class X { ok }\n")

    loop = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=files_build), fixer=FakeFixer(fn=fix))
    pr = loop.run(Phase.VERIFY, "package", 4)
    assert pr.ok and "llm-config-error" not in loop.flags and loop.llm_unavailable is None


def test_non_transport_exception_from_the_fixer_is_a_failed_attempt(cfg, ws, profile):
    def fix(w, c, ctx):
        raise PermissionError("[WinError 32] another process uses C:\\sbmig\\config\\settings.xml")

    cfg.llm.cooldown_seconds = 0
    loop = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=files_build), fixer=FakeFixer(fn=fix))
    pr = loop.run(Phase.VERIFY, "package", 8)          # no script ever → `new` again, then repeated_failure
    assert loop.llm_unavailable is None and "llm-unavailable" not in loop.flags
    assert {b.reason for b in pr.blocked} == {BlockReason.REPEATED_FAILURE}


# ------------------------------------------------------------------------------------------------ A9 (loop)
def test_pinned_block_case_becomes_a_blocked_item_with_the_persons_text(cfg, ws, profile):
    def match(c):
        if c.errors[0].file != FOO:
            return KBMatch()
        return KBMatch(case_hits=[CaseHit(case_id="case-9", name="Gone", errors=list(c.errors), pinned=True,
                                          action=CaseAction(kind="block", block_reason="vendor API, ask team X",
                                                            suggestion="see ticket ABC-1"))])

    fixer = FakeFixer(fn=lambda w, c, ctx: write_fix(c.errors[0].file, "class X { ok }\n"))
    loop = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=files_build), fixer=fixer,
                     kb=FakeKB(match=match))
    pr = loop.run(Phase.VERIFY, "package", 3)
    assert [ctx_c for ctx_c in fixer.contexts]                         # Bar still went to the LLM
    item = next(b for b in pr.blocked if any(FOO in f for f in b.files))
    assert item.reason == BlockReason.EXTERNAL_API_INCOMPATIBLE
    assert item.suggestion == "vendor API, ask team X see ticket ABC-1"
    assert "broken" in ws.read(FOO)                                    # never touched


# ------------------------------------------------------------------------------------------------ A12
def _plan_loop(cfg, ws, profile, *, build, fixer):
    return make_loop(cfg, ws, profile, build=build, fixer=fixer)


def test_watchdog_silent_while_a_build_or_an_llm_call_runs(cfg, ws, profile, monkeypatch, caplog):
    # thresholds well above a git commit (tens of ms), well below the "slow" calls
    monkeypatch.setattr(fl, "WATCHDOG_S", 0.05)
    monkeypatch.setattr(fl, "WATCHDOG_QUIET_S", 0.4)
    monkeypatch.setattr(fl, "WATCHDOG_DUMP_S", 0.6)
    from sbmig.fix.client import llm_call_in_flight
    ws.write(BAR, "class Bar { ok }\n")
    ws.commit("one cluster only")

    def slow_build(w, call):
        time.sleep(0.9)                                    # a long mvn that heartbeats by itself
        return files_build(w, call)

    def slow_llm(w, c, ctx):
        with llm_call_in_flight():                         # the HTTP call (LLMFixer._call marks it)
            time.sleep(0.9)
        return write_fix(c.errors[0].file, "class X { ok }\n")

    with caplog.at_level(logging.WARNING, logger="sbmig"):
        pr = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=slow_build),
                       fixer=FakeFixer(fn=slow_llm)).run(Phase.SPRING, "compile", 2)
    assert pr.ok
    assert not [r for r in caplog.records if "fix loop quiet for" in r.getMessage()]
    assert not list(ws.run_dir.glob("threads-*.txt"))


def test_watchdog_still_speaks_when_nothing_heartbeats(cfg, ws, profile, monkeypatch, caplog):
    monkeypatch.setattr(fl, "WATCHDOG_S", 0.02)
    monkeypatch.setattr(fl, "WATCHDOG_QUIET_S", 0.05)

    def slow_prompt_packing(w, c, ctx):                    # stuck outside the HTTP call (nearest_pom, grep …)
        time.sleep(0.25)
        return write_fix(c.errors[0].file, "class X { ok }\n")

    with caplog.at_level(logging.WARNING, logger="sbmig"):
        make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=files_build),
                  fixer=FakeFixer(fn=slow_prompt_packing)).run(Phase.SPRING, "compile", 2)
    assert [r for r in caplog.records if "fix loop quiet for" in r.getMessage()]


# ------------------------------------------------------------------------------------------------ A8 (loop)
def test_skip_flags_are_per_target(cfg, ws, profile):
    loop = make_loop(cfg, ws, profile)
    loop.test_compile_skipped_targets.add("svc-a")
    assert loop.skips_for("svc-a") == (True, False)
    assert loop.skips_for("svc-a/pom.xml") == (True, False)
    assert loop.skips_for("svc-b") == (False, False)
    assert loop.skips_for(None) == (True, False)                 # a whole-project build contains svc-a
    loop.tests_skipped_targets.add("")                           # a whole-project loop applies everywhere
    assert loop.skips_for("svc-b") == (False, True)
