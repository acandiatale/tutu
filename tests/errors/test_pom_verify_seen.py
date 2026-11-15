"""`ErrorList.add_pom_verify(seen=...)`: the pom corrections of a framework project's reduced run are recorded with
`seen="transform"` and are verified by the next build exactly like the restructure's (`verify_poms` keys on status)."""
from __future__ import annotations

import json

from sbmig.core.models import BuildError, BuildResult, ErrorKind
from sbmig.errors import ErrorList
from sbmig.loop.entries import RunErrors


def build(*errors) -> BuildResult:
    return BuildResult(ok=not errors, goal="package", command=["mvn"], exit_code=1 if errors else 0, duration_s=1.0,
                       log_path="/x.log", errors=list(errors))


def test_transform_entries_are_verified_like_restructure_ones(tmp_path, cfg):
    el = ErrorList(tmp_path / "run", "r", project="demo", cfg=cfg, roots=[tmp_path / "run" / "project"])
    t = el.add_pom_verify("pom.xml", "scope restored: provided (as-is): a:b (was compile)", seen="transform")
    m = el.add_pom_verify("m/pom.xml", "source encoding written: project.build.sourceEncoding=MS949", seen="transform")
    r = el.add_pom_verify("x/pom.xml", "parent → comp-jar")
    assert (t.seen, m.seen, r.seen) == ("transform", "transform", "restructure")
    assert t.status == m.status == "pom-verify"
    bad = BuildError(kind=ErrorKind.DEPENDENCY_RESOLUTION, message="Could not resolve a:b", file="m/pom.xml")
    done = el.verify_poms(build(bad))
    assert t in done and r in done and m not in done and m.status == "pom-verify"
    assert el.verify_poms(build()) == [m] and m.resolved == "build"
    saved = [json.loads(x) for x in el.path.read_text(encoding="utf-8").splitlines()]
    assert {e["message"][:12]: e["seen"] for e in saved}["scope restor"] == "transform"


def test_run_errors_passes_seen_and_old_targets_still_work(tmp_path, cfg):
    el = ErrorList(tmp_path / "run", "r", project="demo", cfg=cfg, roots=[tmp_path / "run" / "project"])
    re_ = RunErrors(el)
    assert re_.add_pom_verify("pom.xml", "x", "transform").seen == "transform"
    assert re_.add_pom_verify("pom.xml", "y").seen == "restructure"

    class Old:                                          # a list without the `seen` parameter (older plugin / fake)
        def __init__(self):
            self.calls = []

        def add_pom_verify(self, file, note):
            self.calls.append((file, note))
            return file

    old = Old()
    assert RunErrors(old).add_pom_verify("pom.xml", "z", "transform") == "pom.xml" and old.calls == [("pom.xml", "z")]
