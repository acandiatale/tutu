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
    """Without the intake rule the same run ends `internal_error` (proves the graph test above can fail)."""
    monkeypatch.setattr(intake, "build_output_paths", lambda files: [])
    env = _start_with_build_output(tmp_path)
    out = env.run()
    assert out.status == FAILED and out.reason != "candidate_build_failed"
    assert "missing" in (out.message or "")


def test_report_without_build_output_has_no_note(tmp_path):
    env = start(tmp_path, {"pom.xml": war_pom(), **APP}, vulns=LIB_VULN, versions=LIB_VERSIONS,
                fail_on={"example:lib:1.2.4"})
    out = env.run()
    md = build_report_md(out, env.paths)
    assert "빌드 산출물" not in md
