"""Review 2026-11-18 L6: a fixer plugin whose `propose_script` returns None (or something that is not a proposal) fails
that one attempt (apply_failed, the reason on the attempt), not the run (it used to raise AttributeError → internal error)."""
from __future__ import annotations

import pytest

from sbmig.core.models import Phase

from .fakes import FakeFixer
from .test_v2_script_loop import FOO, broken_build, make_loop


@pytest.mark.parametrize("answer", [None, "a string", {"code": ""}])
def test_propose_script_without_a_proposal_fails_the_attempt(cfg, ws, answer):
    fixer = FakeFixer(script_fn=lambda w, c, ctx: answer)
    loop = make_loop(cfg, ws, broken_build(FOO), fixer)
    pr = loop.run(Phase.VERIFY, "package", 2)
    assert not pr.ok and pr.attempts
    a = pr.attempts[0]
    assert a.outcome == "apply_failed"
    assert any("returned no script proposal" in e for e in a.apply_errors)
    assert loop.budget.llm_calls >= 1
