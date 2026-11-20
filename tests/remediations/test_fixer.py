"""`sbmig/remediations/fixer.py` (docs/INTERFACES.md "v2.1z-bi" part B as amended by "v2.1z-bj", "Fixer").

The real `run_fix_loop` drives the real `BuildFixLoop` on a tiny tree; the build runner, the LLM fixer and the script
runner are the loop tests' fakes (no Maven, no LLM). Checked: the candidate tree is never written (every outcome),
the loop runs in `work_dir/tree/` which is removed afterwards, fixed bytes land in `work_dir/fixed/`, the patch is
made by difflib, `retry_passed`, the editable-path rules (unit + through the loop + the final byte comparison), stop,
deadline (before stop), the per-build timeout, the exact `llm unavailable`, an exception inside, the JSON shape.
The review's scenarios live in `test_fix_loop_review.py`.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from sbmig.remediations import fixer as fixer_mod
from sbmig.remediations.fixer import (
    check_paths,
    compare_trees,
    is_editable,
    loop_paths,
    make_edit_guard,
    maven_pom_paths,
    plan_notes,
    run_fix_loop,
    unified_patch,
)
from sbmig.remediations.models import BuildOutcome, FixResult, Proposal, RoundState
from tests.loop.fakes import (
    FakeBuildRunner,
    FakeFixer,
    FakeScriptRunner,
    bad_build,
    compile_error,
    make_profile,
    ok_build,
    write_script,
)

FOO = "src/main/java/com/x/Foo.java"
BAR = "src/main/java/com/x/Bar.java"
POM = "<project><artifactId>demo</artifactId></project>\n"
FILES = {"pom.xml": POM, FOO: "class Foo { broken }\n", BAR: "class Bar {}\n"}
MANIFEST = sorted(FILES)
FIRST = BuildOutcome(ok=False, exit_code=1, log="rounds/1/build.log", summary="[ERROR] COMPILATION ERROR")
BUMP = Proposal(id="p-1-1", round=1, ga="com.example:lib", from_version="1.0", to_version="2.0", kind="direct")


def make_tree(tmp_path: Path, gitignore: bytes | None = None, files: dict[str, str] | None = None) -> Path:
    tree = tmp_path / "rounds" / "1" / "candidate"
    for rel, text in (files or FILES).items():
        f = tree / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    if gitignore is not None:
        (tree / ".gitignore").write_bytes(gitignore)
    return tree


def snapshot(root: Path) -> dict[str, bytes]:
    out = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for f in filenames:
            p = Path(dirpath) / f
            out[p.relative_to(root).as_posix()] = p.read_bytes()
    return out


def broken_build(ws, call):
    """Fails while Foo.java still says `broken`."""
    text = (ws.root / FOO).read_text(encoding="utf-8") if (ws.root / FOO).exists() else ""
    return bad_build(compile_error(file=FOO), goal=call["goal"]) if "broken" in text else ok_build(call["goal"])


def run(cfg, tree: Path, *, scripts=None, build=broken_build, stop=lambda: False, deadline=None, journal=None,
        manifest=None, **kw):
    fixer = kw.pop("llm_fixer", None) or FakeFixer(scripts=list(scripts or []))
    return run_fix_loop(cfg, tree, tree.parent / "fixloop", run_id="rem-1-fix-r1", targets=kw.pop("targets", []),
                        applied=[BUMP], manifest_paths=manifest or MANIFEST, first_outcome=FIRST, stop=stop,
                        deadline=deadline, journal=journal,
                        build_runner=kw.pop("build_runner", lambda ws: FakeBuildRunner(ws, fn=build)),
                        llm_fixer=fixer, script_runner=FakeScriptRunner(), profile=kw.pop("profile", make_profile()),
                        **kw)


def fixloop(tree: Path) -> Path:
    return tree.parent / "fixloop"


@pytest.fixture()
def rcfg(cfg):
    cfg.remediation.fix_loop_max_iterations = 3
    return cfg


# ------------------------------------------------------------------------------------------------ fixed
@pytest.mark.parametrize("gitignore", [None, b"*.log\r\nnode_modules/\n# no newline at the end", b"x\n"])
def test_fixed_stores_the_bytes_writes_the_patch_and_leaves_the_tree_alone(rcfg, tmp_path, gitignore):
    tree = make_tree(tmp_path, gitignore)
    before = snapshot(tree)
    res = run(rcfg, tree, scripts=[write_script({FOO: "class Foo { ok }\n"})])
    assert res.attempted and res.fixed and res.reason == "" and not res.retry_passed
    assert res.changed == [FOO]
    assert res.attempts == 1 and res.iterations == 2 and res.remaining == 0
    assert res.llm_calls == 1 and res.input_tokens == 100 and res.output_tokens == 20 and res.models == ["fake-model"]
    # the candidate is never written: same bytes, no .git, no sbmig .gitignore lines
    assert snapshot(tree) == before
    # the fixed bytes are stored durably, the work copy is gone
    fixed = fixloop(tree) / "fixed"
    assert res.fixed_dir == str(fixed)
    assert snapshot(fixed) == {FOO: b"class Foo { ok }\n"}
    assert not (fixloop(tree) / "tree").exists()
    patch = tree.parent / "fix.patch"
    assert res.patch == str(patch) and patch.is_file()
    text = patch.read_text(encoding="utf-8")
    assert f"--- a/{FOO}\n+++ b/{FOO}\n" in text and "-class Foo { broken }" in text and "+class Foo { ok }" in text
    assert ".gitignore" not in text
    # the loop's own journal and logs stay in rounds/<r>/fixloop/
    assert (fixloop(tree) / "journal.jsonl").is_file()


def test_the_first_build_already_passing_is_retry_passed_with_nothing_changed(rcfg, tmp_path):
    tree = make_tree(tmp_path, files={**FILES, FOO: "class Foo { ok }\n"})
    res = run(rcfg, tree)
    assert res.fixed and res.retry_passed and res.changed == [] and res.attempts == 0 and res.llm_calls == 0
    assert res.patch == "" and res.fixed_dir == ""
    assert not (tree.parent / "fix.patch").exists() and not (fixloop(tree) / "fixed").exists()


def test_the_real_analyzer_gives_the_profile(rcfg, tmp_path):
    tree = make_tree(tmp_path)
    res = run(rcfg, tree, scripts=[write_script({FOO: "class Foo { ok }\n"})], profile=None)
    assert res.fixed and res.changed == [FOO]


def test_plan_notes_name_the_bumps_and_the_rules():
    [note] = plan_notes([BUMP])
    assert "com.example:lib 1.0 -> 2.0" in note
    assert "pom.xml must not be edited" in note and "no file may be created or deleted" in note


def test_unified_patch_marks_a_missing_newline_and_binary_files(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for root, text, blob in ((a, "x\ny", b"\xff\x00"), (b, "x\nz\n", b"\xfe\x00")):
        root.mkdir()
        (root / "T.java").write_text(text, encoding="utf-8")
        (root / "data.bin").write_bytes(blob)
    out = unified_patch(a, b, ["T.java", "data.bin"])
    assert "-y\n\\ No newline at end of file\n+z\n" in out
    assert "--- a/data.bin\n+++ b/data.bin\nBinary files differ\n" in out


# ------------------------------------------------------------------------------------------------ not fixed
@pytest.mark.parametrize("gitignore", [None, b"build/\n"])
def test_not_fixed_leaves_the_tree_byte_identical(rcfg, tmp_path, gitignore):
    tree = make_tree(tmp_path, gitignore)
    before = snapshot(tree)
    tries = [write_script({FOO: f"class Foo {{ broken {i} }}\n"}) for i in range(6)]
    res = run(rcfg, tree, scripts=tries)
    assert res.attempted and not res.fixed
    assert res.reason == "not fixed: 1 error(s) left" and res.remaining == 1
    assert res.changed == [] and res.patch == "" and res.fixed_dir == ""
    assert not (tree.parent / "fix.patch").exists() and not (fixloop(tree) / "fixed").exists()
    assert not (fixloop(tree) / "tree").exists()
    assert snapshot(tree) == before
    assert res.llm_calls >= 1 and res.attempts >= 1


def test_llm_unavailable_is_the_bare_reason(rcfg, tmp_path):
    from sbmig.loop.components import LazyFixer
    rcfg.llm.cooldown_seconds = 0

    def factory():
        raise ValueError("llm.base_url is not set (LLM_BASE_URL) at /secret/path")
    tree = make_tree(tmp_path)
    res = run(rcfg, tree, llm_fixer=LazyFixer(factory))
    assert res.attempted and not res.fixed and res.reason == "llm unavailable"
    # the detail stays in the loop's own journal
    assert "LLM_BASE_URL" in (fixloop(tree) / "journal.jsonl").read_text(encoding="utf-8")


# ------------------------------------------------------------------------------------------------ editable paths
def test_loop_paths_leave_out_every_git_segment():
    assert loop_paths(["pom.xml", ".git/HEAD", "svc/.git/config", "svc/a.java", "a/.gitkeep"]) == \
        ["a/.gitkeep", "pom.xml", "svc/a.java"]


@pytest.mark.parametrize("path, ok", [
    (FOO, True), ("src/main/resources/app.properties", True), ("README.md", True),
    ("pom.xml", False), ("mod/pom.xml", False), ("build/parent.xml", False),
    (".mvn/maven.config", False), ("src/.hidden/X.java", False), (".gitignore", False),
    ("mvnw", False), ("mvnw.cmd", False), ("bin/run.sh", False), ("bin/run.BAT", False), ("x.cmd", False),
    ("x.ps1", False), ("lib/a.jar", False), ("a.war", False), ("a.ear", False), ("A.class", False), ("a.zip", False),
])
def test_is_editable(path, ok):
    assert is_editable(path, {"build/parent.xml"}) is ok


def test_maven_pom_paths_finds_poms_under_another_name(tmp_path):
    files = {"pom.xml": "<project><modules><module>svc</module><module>alt/build.xml</module></modules></project>",
             "svc/pom.xml": "<project><parent><groupId>g</groupId><artifactId>p</artifactId><version>1</version>"
                            "<relativePath>../shared/parent-pom.xml</relativePath></parent></project>",
             "alt/build.xml": "<project/>", "shared/parent-pom.xml": "<project/>"}
    tree = make_tree(tmp_path, files=files)
    assert maven_pom_paths(tree) == {"alt/build.xml", "shared/parent-pom.xml"}
    assert maven_pom_paths(tree, ["ci/pom-ci.xml"]) >= {"ci/pom-ci.xml"}
    assert maven_pom_paths(tmp_path / "nothing-here") == set()


def test_check_paths_rules(tmp_path):
    tree = make_tree(tmp_path, files={**FILES, "mvnw": "#!/bin/sh\n", "mod/pom.xml": POM})
    loops = set(MANIFEST) | {"mvnw", "mod/pom.xml"}
    editable = {FOO, BAR}
    assert check_paths(tree, [FOO], loops, editable) is None
    assert check_paths(tree, ["pom.xml"], loops, editable) == "pom.xml must not be edited: pom.xml"
    assert check_paths(tree, ["mod/pom.xml"], loops, editable) == "pom.xml must not be edited: mod/pom.xml"
    assert check_paths(tree, ["mvnw"], loops, editable) == "mvnw must not be edited"
    (tree / "src/main/java/com/x/New.java").write_text("class New {}\n", encoding="utf-8")
    assert check_paths(tree, ["src/main/java/com/x/New.java"], loops, editable) == \
        "src/main/java/com/x/New.java is not a file of the uploaded project"
    (tree / BAR).unlink()
    assert check_paths(tree, [BAR], loops, editable) == f"{BAR} must not be deleted"


def test_make_edit_guard_reads_the_workspace_root(tmp_path):
    tree = make_tree(tmp_path)
    guard = make_edit_guard(MANIFEST, [FOO, BAR])
    ws = SimpleNamespace(root=tree)
    assert guard(ws, "abc", [FOO]) is None
    assert guard(ws, "abc", [FOO, "pom.xml"]) == "pom.xml must not be edited: pom.xml"
    (tree / "other.txt").write_text("x\n", encoding="utf-8")
    assert guard(ws, "abc", ["other.txt"]) == "other.txt is not a file of the uploaded project"


def test_compare_trees(tmp_path):
    a = make_tree(tmp_path / "a", b"x\n", files={**FILES, "mvnw": "sh\n"})
    b = make_tree(tmp_path / "b", b"x\ntarget/\n", files={**FILES, "mvnw": "sh\n", FOO: "class Foo { ok }\n"})
    loops = sorted([*MANIFEST, ".gitignore", "mvnw"])
    editable = {FOO, BAR}
    assert compare_trees(a, b, loops, editable) == ([FOO], None)          # the root .gitignore may differ
    (b / "mvnw").write_text("changed\n", encoding="utf-8")
    assert compare_trees(a, b, loops, editable)[1] == "mvnw must not be edited"
    (b / "mvnw").write_text("sh\n", encoding="utf-8")
    (b / BAR).unlink()
    assert compare_trees(a, b, loops, editable)[1] == f"{BAR} must not be deleted"


@pytest.mark.parametrize("script, reason", [
    (write_script({"pom.xml": "<project/>\n", FOO: "class Foo { ok }\n"}), "pom.xml must not be edited: pom.xml"),
    (write_script({"src/main/java/com/x/New.java": "class New {}\n", FOO: "class Foo { ok }\n"}),
     "src/main/java/com/x/New.java is not a file of the uploaded project"),
    (write_script({"mvnw": "#!/bin/sh\necho hacked\n", FOO: "class Foo { ok }\n"}), "mvnw must not be edited"),
])
def test_the_loop_rejects_a_forbidden_try_and_the_next_one_fixes(rcfg, tmp_path, script, reason):
    tree = make_tree(tmp_path, files={**FILES, "mvnw": "#!/bin/sh\n"})
    before = snapshot(tree)
    fixer = FakeFixer(scripts=[script, write_script({FOO: "class Foo { ok }\n"})])
    res = run(rcfg, tree, llm_fixer=fixer, manifest=sorted([*MANIFEST, "mvnw"]))
    assert res.fixed and res.changed == [FOO]
    assert snapshot(tree) == before
    assert any(reason in " ".join(a.apply_errors) for a in fixer.contexts[1].previous_attempts)


def test_a_deletion_is_rejected_through_the_loop(rcfg, tmp_path):
    tree = make_tree(tmp_path)
    delete = write_script({FOO: "class Foo { ok }\n"})
    delete.code += f"    p.delete({BAR!r})\n"
    fixer = FakeFixer(scripts=[delete, write_script({FOO: "class Foo { ok }\n"})])
    res = run(rcfg, tree, llm_fixer=fixer)
    assert res.fixed and res.changed == [FOO] and (tree / BAR).is_file()
    assert any(f"{BAR} must not be deleted" in " ".join(a.apply_errors) for a in fixer.contexts[1].previous_attempts)


def test_the_final_check_rejects_what_slipped_past_the_guard(rcfg, tmp_path, monkeypatch):
    tree = make_tree(tmp_path)
    before = snapshot(tree)
    monkeypatch.setattr(fixer_mod, "make_edit_guard", lambda loops, editable: (lambda ws, pre, touched: None))
    events = []
    res = run(rcfg, tree, scripts=[write_script({"pom.xml": "<project/>\n", FOO: "class Foo { ok }\n"})],
              journal=events.append)
    assert res.attempted and not res.fixed
    assert res.reason == "rejected: pom.xml must not be edited: pom.xml"
    assert res.changed == [] and not (tree.parent / "fix.patch").exists() and not (fixloop(tree) / "fixed").exists()
    assert snapshot(tree) == before
    assert events == [{"event": "remediation_fix_rejected", "reason": "pom.xml must not be edited: pom.xml"}]


# ------------------------------------------------------------------------------------------------ stop / deadline
def test_a_pending_stop_does_not_start_the_loop(rcfg, tmp_path):
    tree = make_tree(tmp_path)
    before = snapshot(tree)
    res = run(rcfg, tree, stop=lambda: True)
    assert not res.attempted and not res.fixed and res.reason == "stop requested"
    assert snapshot(tree) == before and not fixloop(tree).exists()


def test_a_stop_during_the_loop_leaves_the_tree_alone(rcfg, tmp_path):
    tree = make_tree(tmp_path, b"keep/\n")
    before = snapshot(tree)
    flag = {"stop": False}

    def build(ws, call):
        res = broken_build(ws, call)
        flag["stop"] = True                       # arrives while the first build runs
        return res
    res = run(rcfg, tree, scripts=[write_script({FOO: "class Foo { ok }\n"})], build=build,
              stop=lambda: flag["stop"])
    assert res.attempted and not res.fixed and res.reason == "stop requested"
    assert snapshot(tree) == before


def test_too_little_time_left_does_not_start_the_loop(rcfg, tmp_path):
    tree = make_tree(tmp_path)
    res = run(rcfg, tree, deadline=time.monotonic() + 5)
    assert not res.attempted and res.reason == "time budget used up"


def test_the_deadline_passing_during_the_loop_is_time_even_when_stop_turns_true(rcfg, tmp_path, monkeypatch):
    """The chain budget's wrapped stop() turns true when the time is up: the reason is still the time."""
    clock = {"t": 0.0}
    monkeypatch.setattr(fixer_mod, "_clock", lambda: clock["t"])
    tree = make_tree(tmp_path)
    before = snapshot(tree)

    def build(ws, call):
        clock["t"] = 1000.0                       # the first build eats the rest of the budget
        return broken_build(ws, call)
    res = run(rcfg, tree, scripts=[write_script({FOO: "class Foo { ok }\n"})], build=build, deadline=100.0,
              stop=lambda: clock["t"] >= 100.0)
    assert res.attempted and not res.fixed and res.reason == "time budget used up"
    assert snapshot(tree) == before


def test_each_build_gets_a_timeout_bounded_by_the_time_left(rcfg, tmp_path, monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(fixer_mod, "_clock", lambda: clock["t"])
    rcfg.remediation.build_timeout_minutes = 10
    seen: list[float] = []

    class Runner(FakeBuildRunner):
        timeout_s = 99999.0

        def run(self, goal, **kw):
            seen.append(self.timeout_s)
            clock["t"] += 200.0
            return super().run(goal, **kw)
    tree = make_tree(tmp_path)
    res = run(rcfg, tree, scripts=[write_script({FOO: "class Foo { ok }\n"})], deadline=500.0,
              build_runner=lambda ws: Runner(ws, fn=broken_build))
    assert res.fixed
    assert seen[0] == 500.0 and seen[1] == 300.0               # min(time left, build_timeout_minutes)
    res2 = run(rcfg, make_tree(tmp_path / "b"), scripts=[write_script({FOO: "class Foo { ok }\n"})],
               build_runner=lambda ws: Runner(ws, fn=broken_build))
    assert res2.fixed and seen[-1] == 600.0                    # no deadline: the build cap alone


# ------------------------------------------------------------------------------------------------ errors inside
def test_an_exception_inside_is_an_error_reason_and_the_tree_is_untouched(rcfg, tmp_path):
    tree = make_tree(tmp_path, b"x\n")
    before = snapshot(tree)
    calls = {"n": 0}

    def build(ws, call):
        calls["n"] += 1
        if calls["n"] == 2:                       # after the try changed and committed Foo.java
            raise RuntimeError(f"maven exploded in {ws.root}")
        return broken_build(ws, call)
    res = run(rcfg, tree, scripts=[write_script({FOO: "class Foo { ok }\n"})], build=build)
    assert res.attempted and not res.fixed
    assert res.reason.startswith("error: RuntimeError: maven exploded in ")
    assert str(fixloop(tree)) not in res.reason and str(tmp_path) not in res.reason       # scrubbed
    assert snapshot(tree) == before and not (tree.parent / "fix.patch").exists()
    assert not (fixloop(tree) / "tree").exists()


def test_a_failing_part_before_the_loop_is_an_error_too(rcfg, tmp_path):
    tree = make_tree(tmp_path)
    before = snapshot(tree)

    def boom(ws):
        raise ValueError("no runner")
    res = run(rcfg, tree, build_runner=boom)
    assert res.attempted and res.reason == "error: ValueError: no runner"
    assert snapshot(tree) == before


def test_a_manifest_file_missing_in_the_tree_is_an_error_result(rcfg, tmp_path):
    tree = make_tree(tmp_path)
    res = run(rcfg, tree, manifest=sorted([*MANIFEST, "gone.txt"]))
    assert res.attempted and not res.fixed and res.reason.startswith("error: RemediationError:")


# ------------------------------------------------------------------------------------------------ JSON
def test_round_state_leaves_fix_out_when_none_and_reads_old_json():
    rs = RoundState(round=1)
    data = rs.to_dict()
    assert "fix" not in data and "fix_first_build" not in data and "fix_discarded" not in data
    assert RoundState.from_dict({"round": 2, "committed": True}).fix is None
    rs.fix = FixResult(attempted=True, fixed=True, changed=[FOO], patch="rounds/1/fix.patch")
    rs.fix_first_build = FIRST
    back = RoundState.from_dict(rs.to_dict())
    assert back.fix == rs.fix and back.fix_first_build == FIRST
    assert set(rs.to_dict()["fix"]) == {"attempted", "fixed", "iterations", "attempts", "llm_calls", "input_tokens",
                                        "output_tokens", "models", "changed", "patch", "reason", "remaining"}


def test_the_new_fields_are_in_the_json_only_when_set_and_round_trip():
    f = FixResult(attempted=True, fixed=True, changed=[FOO], patch="rounds/1/fix.patch",
                  fixed_dir="rounds/1/fixloop/fixed", tree_hash="abc", retry_passed=True, discarded_changed=[BAR])
    d = f.to_dict()
    assert d["fixed_dir"] == "rounds/1/fixloop/fixed" and d["tree_hash"] == "abc" and d["retry_passed"] is True
    assert d["discarded_changed"] == [BAR] and FixResult.from_dict(d) == f
    rs = RoundState(round=1, fix_discarded=[FixResult(attempted=True, reason="discarded: the candidate was remade",
                                                      llm_calls=2)])
    back = RoundState.from_dict(rs.to_dict())
    assert back.fix_discarded == rs.fix_discarded and isinstance(back.fix_discarded[0], FixResult)
