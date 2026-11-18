"""Review 2026-11-18 T3, loop side: the `sbmig-bundled` pre-try asks the repository through
`sbmig.restructure.bundled_jars.repository_state_fn`. A repository that did not answer for the jar's coordinates is not
"the jar is not there": nothing is wired (neither a compile nor a system dependency), no "publish it to Nexus" follow-up
is written, and the case blocks as `repository_unreachable` like a transfer failure (no LLM try, no stored script, nothing
credited), with a detail that names the jar and the reason."""
from __future__ import annotations

import pytest

from sbmig.core.models import BlockReason, Phase

from .fakes import FakeFixer, bad_build, journal_events
from .test_bundled_jars_pretry import JAR, _ws, make_loop, missing, seed_jar, wired  # noqa: F401 - fixture

REASON = "remote repositories unreadable: nexus (https://nexus.invalid/repository/public/): Read timed out"


def _jar():
    return seed_jar(group_id="com.acme", artifact_id="acme-seed", version="1.0", source="pom_properties")


class UnreadableRepo:
    def pom_text(self, g, a, v):
        return None

    def versions(self, g, a):
        return []

    def remote_failures(self, g, a):
        return REASON


class AbsentRepo(UnreadableRepo):
    def remote_failures(self, g, a):
        return ""


def test_an_unreadable_repository_blocks_the_case_without_wiring(cfg, tmp_path, wired, monkeypatch):  # noqa: F811
    ws = _ws(tmp_path)
    fixer = FakeFixer(script_fn=lambda *a, **k: pytest.fail("no LLM ask while the repository did not answer"))
    loop = make_loop(cfg, ws, lambda w, call: bad_build(missing()), jars=[_jar()], fixer=fixer)
    monkeypatch.setattr(loop, "_repository", lambda: UnreadableRepo())
    pr = loop.run(Phase.VERIFY, "package", 4)
    assert not pr.ok and pr.attempts == [] and wired == []
    assert fixer.contexts == [] and loop.budget.llm_calls == 0
    [b] = pr.blocked
    assert b.reason == BlockReason.REPOSITORY_UNREACHABLE
    assert "acme-seed-1.0.jar" in b.suggestion and "com.acme:acme-seed:1.0" in b.suggestion
    assert REASON in b.suggestion
    assert not any("Nexus" in f or "unknown_jar" in f for f in loop.followups)
    assert not [e for e in journal_events(ws) if e["event"] == "bundled_jar_wired"]
    ev = [e for e in journal_events(ws) if e["event"] == "blocked"]
    assert ev and ev[-1]["reason"] == "repository_unreachable"


def test_an_unreadable_repository_with_unknown_jar_skip_writes_no_follow_up(cfg, tmp_path, wired, monkeypatch):  # noqa: F811
    cfg.intake.unknown_jar = "skip"
    ws = _ws(tmp_path)
    loop = make_loop(cfg, ws, lambda w, call: bad_build(missing()), jars=[_jar()],
                     fixer=FakeFixer(script_fn=lambda *a, **k: pytest.fail("no LLM ask")))
    monkeypatch.setattr(loop, "_repository", lambda: UnreadableRepo())
    pr = loop.run(Phase.VERIFY, "package", 3)
    assert wired == [] and [b.reason for b in pr.blocked] == [BlockReason.REPOSITORY_UNREACHABLE]
    assert not any("unknown_jar" in f for f in loop.followups)


def test_a_lookup_that_raises_is_unreadable_too(cfg, tmp_path, wired, monkeypatch):  # noqa: F811
    class Broken(UnreadableRepo):
        def pom_text(self, g, a, v):
            raise OSError("connection reset")
    loop = make_loop(cfg, _ws(tmp_path), lambda w, call: bad_build(missing()), jars=[_jar()],
                     fixer=FakeFixer(script_fn=lambda *a, **k: pytest.fail("no LLM ask")))
    monkeypatch.setattr(loop, "_repository", lambda: Broken())
    pr = loop.run(Phase.VERIFY, "package", 3)
    assert wired == []
    [b] = pr.blocked
    assert b.reason == BlockReason.REPOSITORY_UNREACHABLE and "connection reset" in b.suggestion


def test_an_absent_answer_still_wires_the_jar_as_system_scope(cfg, tmp_path, wired, monkeypatch):  # noqa: F811
    loop = make_loop(cfg, _ws(tmp_path), lambda w, call: bad_build(missing()), jars=[_jar()])
    monkeypatch.setattr(loop, "_repository", lambda: AbsentRepo())
    loop.run(Phase.VERIFY, "package", 2)
    assert [(w["jar"], w["repository_has"]) for w in wired] == [(JAR, False)]
