"""v2.1z-bi part B: `sbmig/remediations/fixer.py` (docs/INTERFACES.md "v2.1z-bi", "`sbmig/remediations/fixer.py`").

The real `run_fix_loop` drives the real `BuildFixLoop` on a tiny tree; the build runner, the LLM fixer and the script
runner are the loop tests' fakes (no Maven, no LLM). Checked: the fixed case (patch written, root `.gitignore`
restored byte for byte or left absent, no `.git` left, changed list), the not-fixed case (tree byte-identical), the
edit guard's three rules (unit + through the loop + the final check), stop, deadline, an exception inside.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from sbmig.remediations import fixer as fixer_mod
from sbmig.remediations.fixer import check_paths, make_edit_guard, plan_notes, run_fix_loop
from sbmig.remediations.models import BuildOutcome, Proposal, RoundState
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
    for dirpath, dirnames, filenames in os.walk(root):
        for f in filenames:
            p = Path(dirpath) / f
            out[p.relative_to(root).as_posix()] = p.read_bytes()
    return out


def broken_build(ws, call):
    """Fails while Foo.java still says `broken`."""
    text = (ws.root / FOO).read_text(encoding="utf-8") if (ws.root / FOO).exists() else ""
    return bad_build(compile_error(file=FOO), goal=call["goal"]) if "broken" in text else ok_build(call["goal"])


def run(cfg, tree: Path, *, scripts=None, build=broken_build, stop=lambda: False, deadline=None, journal=None, **kw):
    fixer = kw.pop("llm_fixer", None) or FakeFixer(scripts=list(scripts or []))
    return run_fix_loop(cfg, tree, tree.parent / "fixloop", run_id="rem-1-fix-r1", targets=[], applied=[BUMP],
                        manifest_paths=MANIFEST, first_outcome=FIRST, stop=stop, deadline=deadline, journal=journal,
                        build_runner=kw.pop("build_runner", lambda ws: FakeBuildRunner(ws, fn=build)),
                        llm_fixer=fixer, script_runner=FakeScriptRunner(), profile=kw.pop("profile", make_profile()),
                        **kw)


@pytest.fixture()
def rcfg(cfg):
    cfg.remediation.fix_loop_max_iterations = 3
    return cfg


# ------------------------------------------------------------------------------------------------ fixed
@pytest.mark.parametrize("gitignore", [None, b"*.log\r\nnode_modules/\n# no newline at the end", b"x\n"])
def test_fixed_writes_the_patch_and_leaves_no_git(rcfg, tmp_path, gitignore):
    tree = make_tree(tmp_path, gitignore)
    res = run(rcfg, tree, scripts=[write_script({FOO: "class Foo { ok }\n"})])
    assert res.attempted and res.fixed and res.reason == ""
    assert res.changed == [FOO]
    assert res.attempts == 1 and res.iterations == 2 and res.remaining == 0
    assert res.llm_calls == 1 and res.input_tokens == 100 and res.output_tokens == 20 and res.models == ["fake-model"]
    patch = tree.parent / "fix.patch"
    assert res.patch == str(patch) and patch.is_file()
    text = patch.read_text(encoding="utf-8")
    assert "a/src/main/java/com/x/Foo.java" in text and "+class Foo { ok }" in text and ".gitignore" not in text
    assert (tree / FOO).read_text(encoding="utf-8") == "class Foo { ok }\n"
    assert not (tree / ".git").exists()
    if gitignore is None:
        assert not (tree / ".gitignore").exists()
    else:
        assert (tree / ".gitignore").read_bytes() == gitignore
    # the loop's own journal and logs stay in rounds/<r>/fixloop/, never in the tree
    assert (tree.parent / "fixloop" / "journal.jsonl").is_file()
    assert set(snapshot(tree)) == set(FILES) | ({".gitignore"} if gitignore is not None else set())


def test_the_first_build_already_passing_is_fixed_with_nothing_changed(rcfg, tmp_path):
    tree = make_tree(tmp_path, files={**FILES, FOO: "class Foo { ok }\n"})
    res = run(rcfg, tree)
    assert res.fixed and res.changed == [] and res.attempts == 0 and res.llm_calls == 0
    assert (tree.parent / "fix.patch").read_text(encoding="utf-8") == ""


def test_the_real_analyzer_gives_the_profile(rcfg, tmp_path):
    tree = make_tree(tmp_path)
    res = run(rcfg, tree, scripts=[write_script({FOO: "class Foo { ok }\n"})], profile=None)
    assert res.fixed and res.changed == [FOO]


def test_plan_notes_name_the_bumps_and_the_rules():
    [note] = plan_notes([BUMP])
    assert "com.example:lib 1.0 -> 2.0" in note
    assert "pom.xml must not be edited" in note and "no file may be created or deleted" in note


# ------------------------------------------------------------------------------------------------ not fixed
@pytest.mark.parametrize("gitignore", [None, b"build/\n"])
def test_not_fixed_leaves_the_tree_byte_identical(rcfg, tmp_path, gitignore):
    tree = make_tree(tmp_path, gitignore)
    before = snapshot(tree)
    # every answer changes Foo.java but never fixes it: each try is applied, scored and rolled back or kept
    tries = [write_script({FOO: f"class Foo {{ broken {i} }}\n"}) for i in range(6)]
    res = run(rcfg, tree, scripts=tries)
    assert res.attempted and not res.fixed
    assert res.reason == "not fixed: 1 error(s) left" and res.remaining == 1
    assert res.changed == [] and res.patch == ""
    assert not (tree.parent / "fix.patch").exists()
    assert snapshot(tree) == before
    assert res.llm_calls >= 1 and res.attempts >= 1


# ------------------------------------------------------------------------------------------------ the edit guard
def test_check_paths_rules(tmp_path):
    tree = make_tree(tmp_path)
    manifest = set(MANIFEST)
    assert check_paths(tree, [FOO], manifest) is None
    assert check_paths(tree, ["pom.xml"], manifest) == "pom.xml must not be edited: pom.xml"
    (tree / "mod").mkdir()
    (tree / "mod" / "pom.xml").write_text(POM, encoding="utf-8")
    assert check_paths(tree, ["mod/pom.xml"], manifest | {"mod/pom.xml"}) == "pom.xml must not be edited: mod/pom.xml"
    (tree / "src/main/java/com/x/New.java").write_text("class New {}\n", encoding="utf-8")
    assert check_paths(tree, ["src/main/java/com/x/New.java"], manifest) == \
        "src/main/java/com/x/New.java is not a file of the uploaded project"
    (tree / BAR).unlink()
    assert check_paths(tree, [BAR], manifest) == f"{BAR} must not be deleted"


def test_make_edit_guard_reads_the_workspace_root(tmp_path):
    tree = make_tree(tmp_path)
    guard = make_edit_guard(MANIFEST)
    ws = SimpleNamespace(root=tree)
    assert guard(ws, "abc", [FOO]) is None
    assert guard(ws, "abc", [FOO, "pom.xml"]) == "pom.xml must not be edited: pom.xml"
    (tree / "other.txt").write_text("x\n", encoding="utf-8")
    assert guard(ws, "abc", ["other.txt"]) == "other.txt is not a file of the uploaded project"


@pytest.mark.parametrize("script, reason", [
    (write_script({"pom.xml": "<project/>\n", FOO: "class Foo { ok }\n"}), "pom.xml must not be edited: pom.xml"),
    (write_script({"src/main/java/com/x/New.java": "class New {}\n", FOO: "class Foo { ok }\n"}),
     "src/main/java/com/x/New.java is not a file of the uploaded project"),
])
def test_the_loop_rejects_a_forbidden_try_and_the_next_one_fixes(rcfg, tmp_path, script, reason):
    tree = make_tree(tmp_path)
    fixer = FakeFixer(scripts=[script, write_script({FOO: "class Foo { ok }\n"})])
    res = run(rcfg, tree, llm_fixer=fixer)
    assert res.fixed and res.changed == [FOO]
    assert (tree / "pom.xml").read_text(encoding="utf-8") == POM
    assert not (tree / "src/main/java/com/x/New.java").exists()
    # the rejected try's reason reached the next ask
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
    monkeypatch.setattr(fixer_mod, "make_edit_guard", lambda manifest: (lambda ws, pre, touched: None))
    events = []
    res = run(rcfg, tree, scripts=[write_script({"pom.xml": "<project/>\n", FOO: "class Foo { ok }\n"})],
              journal=events.append)
    assert res.attempted and not res.fixed
    assert res.reason == "rejected: pom.xml must not be edited: pom.xml"
    assert res.changed == [] and not (tree.parent / "fix.patch").exists()
    assert snapshot(tree) == before
    assert events == [{"event": "remediation_fix_rejected", "reason": "pom.xml must not be edited: pom.xml"}]


# ------------------------------------------------------------------------------------------------ stop / deadline
def test_a_pending_stop_does_not_start_the_loop(rcfg, tmp_path):
    tree = make_tree(tmp_path)
    before = snapshot(tree)
    res = run(rcfg, tree, stop=lambda: True)
    assert not res.attempted and not res.fixed and res.reason == "stop requested"
    assert snapshot(tree) == before and not (tree.parent / "fixloop").exists()


def test_a_stop_during_the_loop_resets_the_tree(rcfg, tmp_path):
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


def test_the_deadline_passing_during_the_loop_resets_the_tree(rcfg, tmp_path, monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(fixer_mod, "_clock", lambda: clock["t"])
    tree = make_tree(tmp_path)
    before = snapshot(tree)

    def build(ws, call):
        clock["t"] = 1000.0                       # the first build eats the rest of the budget
        return broken_build(ws, call)
    res = run(rcfg, tree, scripts=[write_script({FOO: "class Foo { ok }\n"})], build=build, deadline=100.0)
    assert res.attempted and not res.fixed and res.reason == "time budget used up"
    assert snapshot(tree) == before


# ------------------------------------------------------------------------------------------------ errors inside
def test_an_exception_inside_is_an_error_reason_and_the_tree_is_reset(rcfg, tmp_path):
    # the project's own .gitignore ignores the directory `com/x`: its files are uploaded files all the same
    tree = make_tree(tmp_path, b"x\n")
    before = snapshot(tree)
    calls = {"n": 0}

    def build(ws, call):
        calls["n"] += 1
        if calls["n"] == 2:                       # after the try changed and committed Foo.java
            raise RuntimeError("maven exploded")
        return broken_build(ws, call)
    res = run(rcfg, tree, scripts=[write_script({FOO: "class Foo { ok }\n"})], build=build)
    assert res.attempted and not res.fixed
    assert res.reason == "error: RuntimeError: maven exploded"
    assert snapshot(tree) == before and not (tree.parent / "fix.patch").exists()


def test_a_failing_part_before_the_loop_is_an_error_too(rcfg, tmp_path):
    tree = make_tree(tmp_path)
    before = snapshot(tree)

    def boom(ws):
        raise ValueError("no runner")
    res = run(rcfg, tree, build_runner=boom)
    assert res.attempted and res.reason == "error: ValueError: no runner"
    assert snapshot(tree) == before


# ------------------------------------------------------------------------------------------------ JSON
def test_round_state_leaves_fix_out_when_none_and_reads_old_json():
    rs = RoundState(round=1)
    data = rs.to_dict()
    assert "fix" not in data and "fix_first_build" not in data
    assert RoundState.from_dict({"round": 2, "committed": True}).fix is None
    rs.fix = fixer_mod.FixResult(attempted=True, fixed=True, changed=[FOO], patch="rounds/1/fix.patch")
    rs.fix_first_build = FIRST
    back = RoundState.from_dict(rs.to_dict())
    assert back.fix == rs.fix and back.fix_first_build == FIRST
    assert set(rs.to_dict()["fix"]) == {"attempted", "fixed", "iterations", "attempts", "llm_calls", "input_tokens",
                                        "output_tokens", "models", "changed", "patch", "reason", "remaining"}
