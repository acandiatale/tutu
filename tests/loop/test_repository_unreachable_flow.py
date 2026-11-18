"""Review 2026-11-18 T4 in the fix loop: a transfer failure blocks as `repository_unreachable` without an LLM ask, a
pre-try or a stored script, with a suggestion that says the repository did not answer; the infra flag pattern of the
orchestrator covers the same errors."""
from __future__ import annotations

import pytest

from sbmig.core.models import GAV, BlockReason, BuildError, ErrorKind, Phase
from sbmig.loop.orchestrator import _INFRA_REPO_RE

from .fakes import FakeFixer, bad_build
from .test_v2_script_loop import make_loop

MSG = ("Could not resolve dependencies for project com.x:demo:jar:1.0: Could not transfer artifact org.example:lib:jar:1.0 "
       "from/to nexus (https://nexus.invalid/repository/public/): Connect to nexus.invalid:443 failed: Connect timed out")


def _err() -> BuildError:
    return BuildError(kind=ErrorKind.DEPENDENCY_RESOLUTION, message=MSG, raw="[ERROR] " + MSG, module="demo",
                      artifact=GAV(group_id="org.example", artifact_id="lib", version="1.0"),
                      signature="dependency_resolution|org.example:lib",
                      signature_generic="dependency_resolution|org.example:lib")


def test_transfer_failure_blocks_without_any_try(cfg, ws):
    fixer = FakeFixer(script_fn=lambda *a, **k: pytest.fail("no LLM ask for a repository that did not answer"))
    loop = make_loop(cfg, ws, lambda w, c: bad_build(_err()), fixer)
    pr = loop.run(Phase.VERIFY, "package", 4)
    assert not pr.ok and not pr.attempts
    [b] = pr.blocked
    assert b.reason == BlockReason.REPOSITORY_UNREACHABLE
    assert "the repository did not answer" in b.suggestion and "org.example:lib:1.0" in b.suggestion


def test_the_infra_pattern_covers_the_same_text():
    assert _INFRA_REPO_RE.search(MSG)
    assert _INFRA_REPO_RE.search("status code: 503, reason phrase: Service Unavailable (503)")
