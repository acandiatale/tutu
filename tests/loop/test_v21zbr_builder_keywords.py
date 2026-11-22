"""v2.1z-br, visible effect 3: a TypeError raised while a build is running is not answered by a second build.

The three call sites that passed a newer keyword and retried without it on TypeError (`BuildFixLoop._build` with
`skip_tests`, the orchestrator's final verify with `skip_tests`, the orchestrator's OpenRewrite call with
`extra_args`) decide from the callee's signature before the one call. A builder whose body raises TypeError is called
exactly once and the TypeError travels as any other exception does at that site; an older builder without the
keyword still gets the old call."""
from __future__ import annotations

import functools
from unittest import mock

import pytest

from sbmig.core.models import Phase, RewriteResult
from sbmig.loop import callee
from sbmig.loop.callee import takes_keyword
from sbmig.loop.orchestrator import Orchestrator

from .fakes import FakeBuildRunner, FakeFixer, FakeRewriteRunner, bad_build, ok_build
from .test_fixloop import make_loop
from .test_orchestrator import make_orch
from .test_tests_skipped import runtime_fail_build, test_failure

INSIDE = "a TypeError raised inside the build, not by the call"


@pytest.fixture(autouse=True)
def tests_compiled(cfg):
    cfg.build.skip_test_compile = False          # the tests-skipped fallback only acts where tests are compiled


# ------------------------------------------------------------------------------------------------ the decision
class _Runner:
    def run(self, goal, *, module=None, extra_args=(), label="", skip_test_compile=False, skip_tests=False):
        return goal


class _Old:
    def run(self, goal, *, module=None, extra_args=(), label="", skip_test_compile=False):
        return goal


class _Wrapper:
    def __init__(self, inner):
        self.inner = inner

    def run(self, goal, **kw):
        return self.inner.run(goal, **kw)


class _ForwardsToOld(_Old):
    def run(self, goal, **kw):                   # forwards everything to the base method, which has no skip_tests
        return super().run(goal, **kw)


class _ForwardsToNew(_Runner):
    def run(self, goal, **kw):
        return super().run(goal, **kw)


class _Uninspectable:
    __signature__ = "not a signature"            # inspect.signature raises TypeError

    def __call__(self, *a, **k):
        return None


def _plain(goal, *, skip_tests=False):
    return goal


def test_takes_keyword_reads_every_kind_of_callee():
    assert takes_keyword(_Runner().run, "skip_tests") and takes_keyword(_Runner().run, "extra_args")
    assert not takes_keyword(_Old().run, "skip_tests")
    assert takes_keyword(_Wrapper(_Old()).run, "skip_tests")               # **kwargs: accepts (the wrapper's business)
    assert not takes_keyword(_ForwardsToOld().run, "skip_tests")           # forwards to a base without it
    assert takes_keyword(_ForwardsToNew().run, "skip_tests")
    assert takes_keyword(functools.partial(_plain, "package"), "skip_tests")
    assert not takes_keyword(functools.partial(_Old().run, "package"), "skip_tests")
    assert takes_keyword(mock.Mock().run, "skip_tests")                    # a plain mock takes anything
    assert not takes_keyword(mock.create_autospec(_Old, instance=True).run, "skip_tests")
    assert takes_keyword(mock.create_autospec(_Runner, instance=True).run, "skip_tests")
    assert takes_keyword(_Uninspectable(), "skip_tests")                   # unknown: the current interface


def test_the_decision_is_made_once_per_callee(monkeypatch):
    calls: list[str] = []
    real = callee._decide

    def counting(fn, name, depth=0):
        calls.append(name)
        return real(fn, name, depth)

    monkeypatch.setattr(callee, "_decide", counting)

    class Fresh:
        def run(self, goal, *, skip_tests=False):
            return goal

    a, b = Fresh(), Fresh()
    for _ in range(3):
        assert takes_keyword(a.run, "skip_tests") and takes_keyword(b.run, "skip_tests")
    assert calls == ["skip_tests"]


# ------------------------------------------------------------------------------------------------ fixloop._build
class _RaisingRunner(FakeBuildRunner):
    """Accepts `skip_tests`; its body raises TypeError on the skip-tests build."""

    def run(self, goal, *, module=None, extra_args=(), label="", skip_test_compile=False, skip_tests=False):
        res = super().run(goal, module=module, extra_args=extra_args, label=label,
                          skip_test_compile=skip_test_compile, skip_tests=skip_tests)
        if skip_tests:
            raise TypeError(INSIDE)
        return res


def test_fixloop_build_calls_a_raising_builder_once(cfg, ws, profile):
    build = _RaisingRunner(ws, fn=runtime_fail_build(test_failure()))
    loop = make_loop(cfg, ws, profile, build=build)
    with pytest.raises(TypeError, match=INSIDE):
        loop._build("package", None, "verify-skip-tests-run", Phase.VERIFY, 1, skip_tests=True)
    assert len(build.calls) == 1 and build.calls[0]["skip_tests"] is True
    assert not any("-DskipTests" in c["extra_args"] for c in build.calls)


def test_fixloop_build_through_a_kwargs_wrapper_calls_once(cfg, ws, profile):
    inner = _RaisingRunner(ws, fn=runtime_fail_build(test_failure()))
    loop = make_loop(cfg, ws, profile, build=_Wrapper(inner))
    with pytest.raises(TypeError, match=INSIDE):
        loop._build("package", None, "verify-skip-tests-run", Phase.VERIFY, 1, skip_tests=True)
    assert len(inner.calls) == 1


def test_fixloop_build_older_runner_gets_the_argument_in_one_call(cfg, ws, profile):
    class OldRunner:
        def __init__(self):
            self.calls: list[list[str]] = []

        def run(self, goal, *, module=None, extra_args=(), label="", skip_test_compile=False):
            self.calls.append(list(extra_args))
            return ok_build()

    r = OldRunner()
    loop = make_loop(cfg, ws, profile, build=r)
    assert loop._build("package", None, "x", Phase.VERIFY, 1, skip_tests=True).ok
    assert r.calls == [["-DskipTests"]]


# ------------------------------------------------------------------------------------------------ orchestrator verify
class _RaisingFinalVerify(FakeBuildRunner):
    """The loop's tests-skipped fallback passes; the final verify that asks for `skip_tests` raises inside."""

    def run(self, goal, *, module=None, extra_args=(), label="", skip_test_compile=False, skip_tests=False):
        res = super().run(goal, module=module, extra_args=extra_args, label=label,
                          skip_test_compile=skip_test_compile, skip_tests=skip_tests)
        if label == "verify" and skip_tests:
            raise TypeError(INSIDE)
        return res


def test_final_verify_calls_a_raising_builder_once(cfg, ws, monkeypatch):
    """The TypeError travels like any exception of the final build: the run aborts with an internal error. The
    abort's own best-effort final build (invariant 1, a separate site that builds after ANY exception) is switched
    off here so that every call counted is the site's."""
    monkeypatch.setattr(Orchestrator, "_final_build_after_abort", lambda self, report: None)
    build = _RaisingFinalVerify(ws, fn=runtime_fail_build(test_failure()))
    report = make_orch(cfg, ws, build=build, fixer=FakeFixer()).run()
    assert not report.success
    assert "aborted" in report.flags
    assert any(f.startswith("run aborted: internal error: TypeError") and INSIDE in f for f in report.manual_followups)
    skip_verifies = [c for c in build.calls if c["label"] == "verify" and c["skip_tests"]]
    assert len(skip_verifies) == 1
    assert not any(c["label"] == "verify" and "-DskipTests" in c["extra_args"] for c in build.calls)
    assert any(c["label"].endswith("skip-tests-run") for c in build.calls)   # the fallback did run first


def test_final_verify_older_runner_gets_the_argument(cfg, ws):
    class OldRunner:
        def __init__(self):
            self.calls: list[dict] = []

        def run(self, goal, *, module=None, extra_args=(), label="", skip_test_compile=False):
            self.calls.append({"label": label, "extra_args": list(extra_args)})
            ok = "-DskipTests" in extra_args or skip_test_compile or goal.split()[-1] not in ("package", "verify")
            return ok_build() if ok else bad_build(test_failure())

    r = OldRunner()
    report = make_orch(cfg, ws, build=r, fixer=FakeFixer()).run()
    assert "tests-skipped" in report.flags
    verify = [c for c in r.calls if c["label"] == "verify"]
    assert verify[-1]["extra_args"].count("-DskipTests") == 1


# ------------------------------------------------------------------------------------------------ orchestrator rewrite
class _RaisingRewrite(FakeRewriteRunner):
    def run(self, recipes, *, extra_recipe_files=(), module=None, dry_run=False, label="", extra_args=()):
        self.runs.append({"recipes": [r.id for r in recipes], "module": module, "extra_args": list(extra_args)})
        raise TypeError(INSIDE)


def test_rewrite_calls_a_raising_runner_once(cfg, ws):
    rw = _RaisingRewrite(ws)
    try:
        make_orch(cfg, ws, rewrite=rw).run()
    except TypeError as e:
        assert INSIDE in str(e)
    assert len(rw.runs) == 1


def test_rewrite_runner_without_extra_args_gets_the_old_call(cfg, ws):
    rw = FakeRewriteRunner(ws)                    # run(recipes, *, extra_recipe_files, module, dry_run, label)
    report = make_orch(cfg, ws, rewrite=rw).run()
    assert report.success and len(rw.runs) == 1


def test_rewrite_runner_with_extra_args_gets_them(cfg, ws):
    class NewRewrite(FakeRewriteRunner):
        def run(self, recipes, *, extra_recipe_files=(), module=None, dry_run=False, label="", extra_args=()):
            self.runs.append({"label": label, "extra_args": list(extra_args)})
            return RewriteResult(ok=True, recipes=[r.id for r in recipes], dry_run=dry_run)

    rw = NewRewrite(ws)
    assert make_orch(cfg, ws, rewrite=rw).run().success
    assert len(rw.runs) == 1 and "extra_args" in rw.runs[0]
