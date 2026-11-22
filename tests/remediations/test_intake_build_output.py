"""v2.1z-bh: build output carried by an uploaded ZIP is left out of the remediation's file list.

`<module>/target/` beside a `pom.xml` is validated like every entry, kept in `original/source.zip`, and left out of
`accepted/` and `original/manifest.json`, so `mvn clean` can never remove a listed file (the company run that ended
`FAILED internal_error` while freezing a failed candidate).
"""
from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

import pytest

from sbmig.remediations import i18n, intake
from sbmig.remediations.models import FAILED, IntakeLimits, IntakeRejected, IntakeResult, RunPaths, read_json
from sbmig.remediations.report import build_report_md

from .test_integration import APP, LIB_VERSIONS, LIB_VULN, MavenEmu, start, war_pom, zip_contents

POM = b"<project><modelVersion>4.0.0</modelVersion><groupId>com.example</groupId><artifactId>app</artifactId></project>"
CLS = b"\xca\xfe\xba\xbe\x00\x00\x00\x41"


def make_zip(path: Path, files: dict[str, bytes], dirs: tuple[str, ...] = ()) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for d in dirs:
            zf.writestr(d if d.endswith("/") else d + "/", b"")
        for name, data in files.items():
            zf.writestr(name, data)
    return path


def accept(tmp_path: Path, files: dict[str, bytes], dirs: tuple[str, ...] = ()) -> tuple[IntakeResult, RunPaths]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    up = make_zip(tmp_path / "up.zip", files, dirs)
    run = tmp_path / "run"
    return intake.accept_zip(up, run, IntakeLimits()), RunPaths(run)


# ---------------------------------------------------------------------------- the pure function
def test_nested_modules_and_root_pom():
    files = ["pom.xml", "target/app.jar", "target/classes/com/example/A.class",
             "core/pom.xml", "core/target/classes/com/example/B.class", "core/src/main/java/com/example/B.java",
             "web/sub/pom.xml", "web/sub/target/x.war", "web/sub/src/main/webapp/index.jsp"]
    assert intake.build_output_paths(files) == [
        "core/target/classes/com/example/B.class", "target/app.jar", "target/classes/com/example/A.class",
        "web/sub/target/x.war"]


def test_target_without_a_sibling_pom_is_ordinary_content():
    files = ["pom.xml", "core/pom.xml", "docs/target/readme.txt",
             "core/src/main/java/com/example/target/T.java", "lib/target/tool.jar"]
    assert intake.build_output_paths(files) == []


def test_a_pom_inside_build_output_never_makes_a_module():
    files = ["pom.xml", "target/pom.xml", "target/target/x.class", "target/classes/A.class",
             "other/target/pom.xml", "other/target/target/y.class"]
    # the outermost target decides: target/pom.xml is build output and does not make target/ a module of its own.
    # other/target has no other/pom.xml, so it is ordinary content and its own pom.xml makes other/target a module
    assert intake.build_output_paths(files) == [
        "other/target/target/y.class", "target/classes/A.class", "target/pom.xml", "target/target/x.class"]


def test_wrapped_single_top_folder():
    files = ["proj/pom.xml", "proj/target/classes/A.class", "proj/src/main/java/A.java"]
    assert intake.build_output_paths(files) == ["proj/target/classes/A.class"]


def test_a_file_named_target_is_not_a_folder():
    assert intake.build_output_paths(["pom.xml", "target"]) == []


# ---------------------------------------------------------------------------- accept_zip
def test_accept_leaves_build_output_out_of_manifest_and_accepted(tmp_path, caplog):
    src = b"package com.example;\nclass A {}\n"
    files = {"pom.xml": POM, "src/main/java/com/example/A.java": src,
             "target/classes/com/example/A.class": CLS, "target/app-0.1.jar": b"PK-jar",
             "core/pom.xml": POM, "core/target/classes/com/example/B.class": CLS}
    caplog.set_level("INFO", logger="sbmig.remediations.intake")
    res, paths = accept(tmp_path, files, dirs=("target", "target/classes", "core/target/generated-sources"))
    kept = ["core/pom.xml", "pom.xml", "src/main/java/com/example/A.java"]
    assert intake.manifest_paths(paths.manifest) == kept
    on_disk = sorted(p.relative_to(paths.accepted).as_posix() for p in paths.accepted.rglob("*") if p.is_file())
    assert on_disk == kept
    assert not list(paths.accepted.rglob("target"))
    assert res.files == 3 and res.unpacked_bytes == len(POM) * 2 + len(src)
    assert res.tree_hash == intake.tree_hash(paths.accepted, kept)
    assert res.build_output_files == 3
    note = "build output left out: 3 file(s) under core/target, target"
    assert note in res.notes
    assert note in caplog.text
    # the original ZIP keeps every byte
    with zipfile.ZipFile(paths.original_zip) as zf:
        assert "target/classes/com/example/A.class" in zf.namelist()


def test_note_lists_at_most_five_folders(tmp_path):
    files = {"pom.xml": POM}
    for i in range(7):
        files[f"m{i}/pom.xml"] = POM
        files[f"m{i}/target/A.class"] = CLS
    res, _paths = accept(tmp_path, files)
    assert res.build_output_files == 7
    assert "build output left out: 7 file(s) under m0/target, m1/target, m2/target, m3/target, m4/target +2 more" \
        in res.notes


def test_all_build_output_is_rejected_as_empty(tmp_path, monkeypatch):
    # the outermost pom.xml that makes a folder a module is never build output itself, so this shape cannot come
    # from a real ZIP; the guard is pinned by declaring every file build output
    monkeypatch.setattr(intake, "build_output_paths", lambda files: sorted(files))
    up = make_zip(tmp_path / "up.zip", {"pom.xml": POM, "target/A.class": CLS})
    with pytest.raises(IntakeRejected) as e:
        intake.accept_zip(up, tmp_path / "run", IntakeLimits())
    assert e.value.code == "empty"
    assert not (tmp_path / "run" / "accepted").exists() and not (tmp_path / "run" / "original" / "manifest.json").exists()


def test_limits_still_count_build_output(tmp_path):
    up = make_zip(tmp_path / "up.zip", {"pom.xml": POM, "target/a.class": CLS, "target/b.class": CLS})
    with pytest.raises(IntakeRejected) as e:
        intake.accept_zip(up, tmp_path / "run", IntakeLimits(max_entries=2))
    assert e.value.code == "too_many_entries"
    assert not (tmp_path / "run" / "accepted").exists()


def test_wrapped_zip_keeps_project_root(tmp_path):
    res, paths = accept(tmp_path, {"proj/pom.xml": POM, "proj/target/classes/A.class": CLS,
                                   "proj/src/main/java/A.java": b"class A {}\n"})
    assert res.project_root == "proj"
    assert intake.manifest_paths(paths.manifest) == ["proj/pom.xml", "proj/src/main/java/A.java"]
    assert "build output left out: 1 file(s) under proj/target" in res.notes


def test_no_build_output_keeps_json_unchanged(tmp_path):
    res, _paths = accept(tmp_path, {"pom.xml": POM, "target/readme": b"x", "docs/target/a.txt": b"y"})
    # target/readme IS build output (beside the root pom); docs/target is not
    assert res.build_output_files == 1
    res2, _ = accept(tmp_path / "b", {"pom.xml": POM, "docs/target/a.txt": b"y"})
    assert res2.build_output_files == 0 and res2.notes == []
    assert "build_output_files" not in res2.to_dict()
    assert res.to_dict()["build_output_files"] == 1
    old = res2.to_dict()
    assert IntakeResult.from_dict(old).build_output_files == 0
    assert IntakeResult.from_dict(res.to_dict()).build_output_files == 1


def test_korean_note():
    text = i18n.ko_text("build output left out: 3 file(s) under core/target, target")
    assert text == ("업로드한 ZIP의 빌드 산출물 3개(`core/target`, `target`)는 검사와 결과 ZIP에서 제외했습니다. "
                    "원본 ZIP에는 그대로 있습니다.")
    more = i18n.ko_text("build output left out: 9 file(s) under a/target, b/target, c/target, d/target, e/target +2 more")
    assert "`e/target` 외 2곳)" in more and "+2 more" not in more
    assert i18n.rule_keys("build output left out: 1 file(s) under target") == ["build_output_left_out"]


# ---------------------------------------------------------------------------- the graph
class CleaningMaven(MavenEmu):
    """`mvn clean package`: every non-tree call removes the `target/` folders first, as Maven's clean phase does,
    even when the build then fails."""

    def __call__(self, cmd, *, cwd, env, log_path, timeout_s):
        if not any(a.startswith("-DoutputFile=") for a in cmd) and "clean" in cmd:
            for t in sorted(Path(cwd).rglob("target"), key=lambda p: len(p.parts), reverse=True):
                if t.is_dir() and (t.parent / "pom.xml").is_file():
                    shutil.rmtree(t)
        return super().__call__(cmd, cwd=cwd, env=env, log_path=log_path, timeout_s=timeout_s)


def _start_with_build_output(tmp_path: Path):
    files = {"pom.xml": war_pom(), **APP, "target/classes/example/App.class": CLS,
             "target/app-0.1.war": b"PK-stale-war"}
    env = start(tmp_path, files, vulns=LIB_VULN, versions=LIB_VERSIONS, fail_on={"example:lib:1.2.4"})
    cleaning = CleaningMaven(env.mvn.world, env.mvn.fail_on)
    env.comps.maven.runner = cleaning
    env.mvn = cleaning
    return env


def test_candidate_build_failure_with_build_output_ends_candidate_build_failed(tmp_path):
    env = _start_with_build_output(tmp_path)
    out = env.run()
    assert out.status == FAILED and out.reason == "candidate_build_failed", (out.reason, out.message)
    assert out.delivery is not None and not out.delivery.build_ok
    names = set(zip_contents(env.paths.delivery_zip.read_bytes()))
    assert "pom.xml" in names and not any(n.startswith("target/") for n in names)
    # IVAS saw the original bytes once (the baseline), the stale build output never in the delivery
    assert len(env.ivas.uploads) == 1
    report = env.paths.report_md.read_text(encoding="utf-8")
    line = "업로드한 ZIP의 빌드 산출물 2개(`target`)는 검사와 결과 ZIP에서 제외했습니다. 원본 ZIP에는 그대로 있습니다."
    assert report.count(line) == 1
    assert "결과 ZIP의 파일 목록은 원본에서 빌드 산출물을 뺀 목록과 같습니다." in report
    state = read_json(env.paths.state)
    assert state["intake"]["build_output_files"] == 2


def test_the_cleaning_runner_reproduces_the_company_failure(tmp_path, monkeypatch):
    """Without the intake rule AND without the after-build synchronisation (`sync_tree_table`, which replaced the
    direct `restore_missing` call in v2.1z-bk) the same run ends `internal_error` (proves the graph test above can fail)."""
    monkeypatch.setattr(intake, "build_output_paths", lambda files: [])
    monkeypatch.setattr(intake, "sync_tree_table", lambda tree, expected, table, paths: ([], []))
    env = _start_with_build_output(tmp_path)
    out = env.run()
    assert out.status == FAILED and out.reason != "candidate_build_failed"
    assert "missing" in (out.message or "")


def test_restore_missing_is_the_second_net_when_build_output_was_listed(tmp_path, monkeypatch):
    """v2.1z-bj: with build output in the manifest (the intake rule off), the files `mvn clean` removed are copied back
    from accepted/ after the build (`sync_tree_table` since v2.1z-bk), so the run still ends `candidate_build_failed` with a frozen delivery."""
    monkeypatch.setattr(intake, "build_output_paths", lambda files: [])
    env = _start_with_build_output(tmp_path)
    out = env.run()
    assert out.status == FAILED and out.reason == "candidate_build_failed", (out.reason, out.message)
    assert out.delivery is not None and not out.delivery.build_ok


def test_report_without_build_output_has_no_note(tmp_path):
    env = start(tmp_path, {"pom.xml": war_pom(), **APP}, vulns=LIB_VULN, versions=LIB_VERSIONS,
                fail_on={"example:lib:1.2.4"})
    out = env.run()
    md = build_report_md(out, env.paths)
    assert "빌드 산출물" not in md


# ---------------------------------------------------------------------------- v2.1z-bj: `src/**/target` is content
def test_a_target_under_src_is_content_even_beside_a_pom():
    """The rule of `core.workspace.is_build_output_path`: a `target` segment after a `src` segment never counts, even
    when a test resource carries a whole project (its own pom.xml) with a `target/` folder."""
    files = ["pom.xml", "src/main/java/com/example/A.java",
             "src/test/resources/proj/pom.xml", "src/test/resources/proj/target/classes/X.class",
             "src/it/demo/pom.xml", "src/it/demo/target/demo.jar",
             "core/pom.xml", "core/src/test/resources/p/pom.xml", "core/src/test/resources/p/target/y.txt",
             "core/target/classes/B.class"]
    assert intake.build_output_paths(files) == ["core/target/classes/B.class"]


def test_the_rule_agrees_with_the_workspace_rule(tmp_path):
    from sbmig.core.workspace import is_build_output_path
    files = ["pom.xml", "target/a.jar", "src/test/resources/proj/pom.xml", "src/test/resources/proj/target/x",
             "m/pom.xml", "m/target/classes/B.class", "m/src/target/C.java", "docs/target/readme.txt"]
    root = tmp_path / "tree"
    for f in files:
        (root / f).parent.mkdir(parents=True, exist_ok=True)
        (root / f).write_bytes(b"x")
    assert intake.build_output_paths(files) == sorted(f for f in files if is_build_output_path(f, root))


def test_accept_keeps_src_target_files_and_the_note_names_only_real_output(tmp_path):
    files = {"pom.xml": POM, "src/main/java/com/example/A.java": b"class A {}",
             "src/test/resources/proj/pom.xml": POM, "src/test/resources/proj/target/classes/X.class": CLS,
             "target/classes/com/example/A.class": CLS}
    res, paths = accept(tmp_path, files)
    listed = intake.manifest_paths(paths.manifest)
    assert "src/test/resources/proj/target/classes/X.class" in listed
    assert (paths.accepted / "src/test/resources/proj/target/classes/X.class").read_bytes() == CLS
    assert "target/classes/com/example/A.class" not in listed
    assert res.build_output_files == 1
    assert res.notes == ["build output left out: 1 file(s) under target"]


# ---------------------------------------------------------------------------- helpers
def _tree(root: Path, files: dict[str, bytes]) -> Path:
    for rel, data in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    return root


# ---------------------------------------------------------------------------- v2.1z-bk: sync_tree_table refusals
# (v2.1z-bs: `sync_tree` was removed; the refusals it shared with `sync_tree_table` are pinned on the table form)
def test_sync_tree_table_refuses_symlinks_on_either_side(tmp_path):
    from sbmig.remediations.models import RemediationError
    outside = tmp_path / "outside"
    outside.mkdir()
    acc = _tree(tmp_path / "accepted", {"a.txt": b"orig", "b.txt": b"orig"})
    table = intake.file_table(acc, ["a.txt", "b.txt"])
    cand = _tree(tmp_path / "cand", {"b.txt": b"orig"})
    (cand / "a.txt").symlink_to(outside / "x")
    with pytest.raises(RemediationError) as e:
        intake.sync_tree_table(cand, lambda rel: acc / rel, table, ["a.txt"])
    assert e.value.code == "symlink" and not (outside / "x").exists()
    (acc / "b.txt").unlink()
    (acc / "b.txt").symlink_to(outside / "secret")
    (outside / "secret").write_bytes(b"secret")
    (cand / "b.txt").write_bytes(b"built")                    # differs from the row: the expected file is opened
    with pytest.raises(RemediationError) as e:
        intake.sync_tree_table(cand, lambda rel: acc / rel, table, ["b.txt"])
    assert e.value.code == "symlink" and (cand / "b.txt").read_bytes() == b"built"


def test_sync_tree_table_refuses_unsafe_paths(tmp_path):
    from sbmig.remediations.models import RemediationError
    acc = _tree(tmp_path / "accepted", {"a.txt": b"x"})
    cand = tmp_path / "cand"
    cand.mkdir()
    for bad in ("../a.txt", "/etc/passwd", "a/"):
        with pytest.raises(RemediationError):
            intake.sync_tree_table(cand, lambda rel: acc / rel, {}, [bad])


# ---------------------------------------------------------------------------- v2.1z-bs: a path the table does not list
# What the removed `sync_tree` tests proved about the code it shared with `sync_tree_table` (the `row is None` branch):
# every call below passes a table without rows, so every path takes that branch.
def _unlisted_case(tmp_path):
    acc = _tree(tmp_path / "accepted", {"pom.xml": b"<project>old</project>", "src/A.java": b"class A {}",
                                         "src/B.java": b"class B {}", "mvnw": b"#!/bin/sh\n", "run.sh": b"#!/bin/sh\n",
                                         "README": b"same"})
    (acc / "mvnw").chmod(0o755)
    (acc / "run.sh").chmod(0o755)
    applied = _tree(tmp_path / "applied", {"pom.xml": b"<project>new</project>"})
    cand = _tree(tmp_path / "cand", {"pom.xml": b"<project>sorted by the build</project>",
                                     "src/A.java": b"class A {} // reformatted", "src/B.java": b"class X {}",
                                     "run.sh": b"#!/bin/sh\n", "README": b"same", "only-here.txt": b"kept"})
    (cand / "run.sh").chmod(0o644)                     # same bytes, the build dropped the exec bit
    calls: list[str] = []

    def expected(rel):
        calls.append(rel)
        return applied / rel if (applied / rel).is_file() else acc / rel
    paths = ["pom.xml", "src/A.java", "src/B.java", "mvnw", "run.sh", "README", "only-here.txt", "gone/everywhere.txt"]
    return acc, cand, expected, paths, calls


def test_sync_tree_table_puts_back_an_unlisted_path_from_its_expected_file(tmp_path):
    _acc, cand, expected, paths, calls = _unlisted_case(tmp_path)
    missing, changed = intake.sync_tree_table(cand, expected, {}, paths)
    assert missing == ["mvnw"] and changed == ["pom.xml", "run.sh", "src/A.java", "src/B.java"]
    assert (cand / "pom.xml").read_bytes() == b"<project>new</project>"         # the applied bytes, not accepted
    assert (cand / "src/A.java").read_bytes() == b"class A {}"
    assert (cand / "src/B.java").read_bytes() == b"class B {}"                   # same size, other bytes
    assert sorted(calls) == sorted(paths)
    assert not list(cand.rglob(".*sync-tmp"))                                     # no temporary file left


def test_sync_tree_table_leaves_an_unlisted_path_without_expected_file_alone(tmp_path):
    _acc, cand, expected, _paths, _calls = _unlisted_case(tmp_path)
    assert intake.sync_tree_table(cand, expected, {}, ["only-here.txt", "gone/everywhere.txt"]) == ([], [])
    assert (cand / "only-here.txt").read_bytes() == b"kept"                      # present in the tree only: untouched
    assert not (cand / "gone").exists()                                           # absent on both sides: not created


def test_sync_tree_table_takes_the_exec_bit_of_an_unlisted_path_from_its_expected_file(tmp_path):
    import stat
    _acc, cand, expected, paths, _calls = _unlisted_case(tmp_path)
    intake.sync_tree_table(cand, expected, {}, paths)
    mode = {p: stat.S_IMODE((cand / p).stat().st_mode) for p in ("mvnw", "run.sh", "src/A.java", "README")}
    assert mode == {"mvnw": 0o755, "run.sh": 0o755, "src/A.java": 0o644, "README": 0o644}


def test_sync_tree_table_on_unlisted_paths_is_idempotent(tmp_path):
    _acc, cand, expected, paths, _calls = _unlisted_case(tmp_path)
    intake.sync_tree_table(cand, expected, {}, paths)
    before = {p.relative_to(cand).as_posix(): (p.read_bytes(), p.stat().st_mode) for p in cand.rglob("*") if p.is_file()}
    assert intake.sync_tree_table(cand, expected, {}, paths) == ([], [])
    after = {p.relative_to(cand).as_posix(): (p.read_bytes(), p.stat().st_mode) for p in cand.rglob("*") if p.is_file()}
    assert after == before and not list(cand.rglob(".*sync-tmp"))


def test_sync_tree_table_compares_sizes_of_an_unlisted_path_before_reading_bytes(tmp_path, monkeypatch):
    acc = _tree(tmp_path / "accepted", {"a.txt": b"x" * 10, "b.txt": b"y" * 10})
    cand = _tree(tmp_path / "cand", {"a.txt": b"x" * 11, "b.txt": b"y" * 10})
    opened: list[str] = []
    real_open = open

    def spy(path, *a, **k):
        opened.append(Path(path).parent.name + "/" + Path(path).name)
        return real_open(path, *a, **k)
    monkeypatch.setattr("builtins.open", spy)
    missing, changed = intake.sync_tree_table(cand, lambda rel: acc / rel, {}, ["a.txt", "b.txt"])
    assert (missing, changed) == ([], ["a.txt"])
    # a.txt differs in size: the candidate's bytes are never read, the expected file is read once to copy it;
    # b.txt has the same size: read on both sides to compare
    assert opened.count("cand/a.txt") == 0 and opened.count("accepted/a.txt") == 1
    assert opened.count("cand/b.txt") == 1 and opened.count("accepted/b.txt") == 1
    assert (cand / "a.txt").read_bytes() == b"x" * 10
