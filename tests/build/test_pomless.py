"""v2.1z-ax: a library whose repository has the jar but no pom (`sbmig.build.pomless`).

Detection runs on REAL Maven 3.9.10 output captured with two loopback repositories (A serves the jar and answers 404
for the pom, B answers 502 for everything): `tests/build/logs/real-pomless-*.log`. The proof and the stub writer run
against fakes; every test that may write a stub points `build.local_repo` at a tmp folder."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from sbmig.build import pomless
from sbmig.build.mvn import ProcessResult
from sbmig.build.repo_versions import (
    RepositoryLookup as RealLookup,  # the real class (conftest patches the module name)
)
from sbmig.core.config import load_config

LOGS = Path(__file__).parent / "logs"


def _log(name: str) -> str:
    return (LOGS / f"real-pomless-{name}.log").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------- detection
@pytest.mark.parametrize("name", ["direct", "direct-repeat", "transitive", "reactor"])
def test_detects_the_descriptor_failure_caused_by_a_pom_transfer_error(name):
    assert pomless.detect(_log(name)) == [("com.example", "pomless", "1.0")]


@pytest.mark.parametrize("name", ["classifier", "type", "parent", "bom", "plugin", "extension", "missing"])
def test_excluded_forms_are_not_detected(name):
    assert pomless.detect(_log(name)) == []


def test_detects_the_maven_38_one_line_form_and_the_cached_form():
    one_line = ("[ERROR] Failed to execute goal on project app: Could not resolve dependencies for project "
                "com.example:app:jar:1.0: Failed to collect dependencies at com.example:pomless:jar:1.0: Failed to read "
                "artifact descriptor for com.example:pomless:jar:1.0: Could not transfer artifact "
                "com.example:pomless:pom:1.0 from/to maven-central (https://nexus.invalid/repository/maven-central/): "
                "status code: 502, reason phrase: Bad Gateway (502)")
    assert pomless.detect(one_line) == [("com.example", "pomless", "1.0")]
    cached = ("[ERROR] Failed to read artifact descriptor for com.example:lib:jar:2.0\n"
              "[ERROR] \tCaused by: com.example:lib:pom:2.0 failed to transfer from https://nexus.invalid/x/ during a "
              "previous attempt. This failure was cached in the local repository and resolution is not reattempted "
              "until the update interval of maven-central has elapsed or updates are forced. Original error: Could not "
              "transfer artifact com.example:lib:pom:2.0 from/to maven-central (https://nexus.invalid/x/): status code: 502")
    assert pomless.detect(cached) == [("com.example", "lib", "2.0")]


def test_the_http_blocker_is_never_a_pomless_library():
    text = ("[ERROR] Failed to read artifact descriptor for com.example:lib:jar:2.0\n"
            "[ERROR] \tCaused by: The following artifacts could not be resolved: com.example:lib:pom:2.0 (absent): Could "
            "not transfer artifact com.example:lib:pom:2.0 from/to maven-default-http-blocker (http://0.0.0.0/): Blocked "
            "mirror for repositories: [old (http://repo.example.invalid/, default, releases)]")
    assert pomless.detect(text) == []


def test_a_transfer_error_of_another_coordinate_is_not_this_library():
    text = ("[ERROR] Failed to read artifact descriptor for com.example:lib:jar:2.0\n"
            "[ERROR] \tCaused by: The following artifacts could not be resolved: com.example:other:pom:2.0 (absent): "
            "Could not transfer artifact com.example:other:pom:2.0 from/to central (https://nexus.invalid/): 502")
    assert pomless.detect(text) == []


@pytest.mark.parametrize("g,a,v", [("com/../x", "a", "1"), ("com.example", "a\\b", "1"), ("com.example", "a", ".."),
                                   ("com.example", "a", "1\x00"), ("com.example", "a", "1--x"), ("", "a", "1"),
                                   (".hidden", "a", "1")])
def test_coordinates_that_are_not_plain_are_refused(g, a, v):
    assert not pomless.plain_coordinate(g, a, v)


def test_plain_coordinates_pass():
    assert pomless.plain_coordinate("com.example.lib", "my-lib_2", "1.0.0-SNAPSHOT")


# ---------------------------------------------------------------------------- the real lookup's file answers
def _status_map(answers):
    asked: list[tuple[str, str]] = []

    def status(url, creds, timeout, method):
        asked.append((method, url))
        for (prefix, suffix, meth), result in answers.items():
            if url.startswith(prefix) and url.endswith(suffix) and meth in (method, "*"):
                return result
        return 404, "HTTP 404"
    return status, asked


def _cfg(tmp_path, **build):
    opts = {"local_repo": str(tmp_path / "m2"), "settings_file": str(_settings(tmp_path))}
    opts.update(build)
    return load_config(overrides={"artifacts": {"parent": {"group_id": "g", "artifact_id": "a"}}, "build": opts})


def _settings(tmp_path) -> Path:
    p = tmp_path / "settings.xml"
    if not p.exists():
        p.write_text("<settings><mirrors><mirror><id>nexus</id><mirrorOf>central</mirrorOf>"
                     "<url>https://nexus.invalid/central/</url></mirror></mirrors><profiles><profile><id>p</id>"
                     "<repositories><repository><id>releases</id><url>https://nexus.invalid/releases/</url></repository>"
                     "</repositories></profile></profiles><activeProfiles><activeProfile>p</activeProfile>"
                     "</activeProfiles></settings>", encoding="utf-8")
    return p


def test_file_answers_classifies_obtained_missing_denied_and_failed(tmp_path):
    status, asked = _status_map({
        ("https://nexus.invalid/releases/", ".pom", "GET"): (404, "HTTP 404"),
        ("https://nexus.invalid/central/", ".pom", "GET"): (502, "HTTP 502"),
        ("https://nexus.invalid/releases/", ".jar", "HEAD"): (200, ""),
    })
    lk = RealLookup(_cfg(tmp_path), status=status)
    poms = lk.file_answers("com.example", "lib", "1.0", "pom")
    assert [o for _u, o, _d in poms] == ["missing", "failed"]
    jars = lk.file_answers("com.example", "lib", "1.0", "jar")
    assert jars[0][:2] == ("https://nexus.invalid/releases/", "ok")
    assert len(jars) == 1                                       # stops at the first repository that has it
    assert ("HEAD", "https://nexus.invalid/releases/com/example/lib/1.0/lib-1.0.jar") in asked
    assert not any(m == "GET" and u.endswith(".jar") for m, u in asked)    # the jar is never downloaded


def test_file_answers_falls_back_to_a_one_byte_range_when_head_is_refused(tmp_path):
    status, asked = _status_map({
        ("https://nexus.invalid/releases/", ".jar", "HEAD"): (405, "HTTP 405"),
        ("https://nexus.invalid/releases/", ".jar", "RANGE"): (206, ""),
    })
    lk = RealLookup(_cfg(tmp_path), status=status)
    assert lk.file_answers("com.example", "lib", "1.0", "jar")[0][1] == "ok"
    assert ("RANGE", "https://nexus.invalid/releases/com/example/lib/1.0/lib-1.0.jar") in asked


@pytest.mark.parametrize("code,outcome", [(401, "denied"), (403, "denied"), (500, "failed"), (None, "failed"),
                                          (410, "missing"), (200, "ok")])
def test_file_answer_outcomes(tmp_path, code, outcome):
    status, _ = _status_map({("https://nexus.invalid/", ".pom", "GET"): (code, f"HTTP {code}" if code else "refused")})
    lk = RealLookup(_cfg(tmp_path), status=status)
    assert lk.file_answers("com.example", "lib", "1.0", "pom")[0][1] == outcome


# ---------------------------------------------------------------------------- proof + stub
class FakeLookup:
    def __init__(self, poms, jars, urls=("https://nexus.invalid/releases/", "https://nexus.invalid/central/")):
        self.poms, self.jars, self.urls = poms, jars, list(urls)
        self.asked: list[str] = []

    def repository_urls(self):
        return [(u, None, "test") for u in self.urls]

    def file_answers(self, g, a, v, ext):
        self.asked.append(ext)
        table = self.poms if ext == "pom" else self.jars
        out = []
        for u, o in zip(self.urls, table):
            out.append((u, o, ""))
            if o == "ok":
                break
        return out


class Journal(list):
    def __call__(self, ev):
        self.append(ev)


def _failed(text):
    return ProcessResult(exit_code=1, text=text, duration_s=0.1)


def _passed():
    return ProcessResult(exit_code=0, text="[INFO] BUILD SUCCESS\n", duration_s=0.1)


def _run(cfg, lookup, results, *, memo=None, journal=None):
    calls: list[int] = []
    seq = list(results)

    def run(k):
        calls.append(k)
        return seq[min(k, len(seq) - 1)]
    journal = journal if journal is not None else Journal()
    memo = memo if memo is not None else pomless.PomlessMemo()
    out = pomless.run_with_stubs(cfg, run, journal=journal, memo=memo, lookup=lookup)
    return out, calls, journal, memo


def _stub(tmp_path, g="com.example", a="pomless", v="1.0") -> Path:
    return tmp_path / "m2" / Path(*g.split(".")) / a / v / f"{a}-{v}.pom"


def test_stub_written_and_command_run_again(tmp_path):
    cfg = _cfg(tmp_path)
    lookup = FakeLookup(poms=["missing", "failed"], jars=["ok", "failed"])
    out, calls, journal, memo = _run(cfg, lookup, [_failed(_log("direct")), _passed()])
    assert out.exit_code == 0 and calls == [0, 1]
    stub = _stub(tmp_path)
    text = stub.read_text(encoding="utf-8")
    for part in ("<modelVersion>4.0.0</modelVersion>", "<groupId>com.example</groupId>", "<artifactId>pomless</artifactId>",
                 "<version>1.0</version>", "<packaging>jar</packaging>", "<!--", "Written by sbmig", "Delete this file when the real pom is uploaded"):
        assert part in text
    assert "<dependencies>" not in text
    ev = [e for e in journal if e["event"] == "pomless_stub"]
    assert ev == [{"event": "pomless_stub", "ga": "com.example:pomless", "version": "1.0", "path": str(stub),
                   "jar_from": "https://nexus.invalid/releases/", "created": True}]
    assert [s["ga"] for s in memo.stubs] == ["com.example:pomless"]
    assert not list(stub.parent.glob("*.tmp"))


def test_pom_available_writes_nothing(tmp_path):
    cfg = _cfg(tmp_path)
    lookup = FakeLookup(poms=["ok"], jars=["ok"])
    out, calls, journal, _ = _run(cfg, lookup, [_failed(_log("direct"))])
    assert out.exit_code == 1 and calls == [0]
    assert not _stub(tmp_path).exists()
    assert [e for e in journal if e["event"] == "pomless_check"] == [
        {"event": "pomless_check", "ga": "com.example:pomless", "version": "1.0", "reason": "pom-available"}]
    assert lookup.asked == ["pom"]


def test_jar_unavailable_writes_nothing(tmp_path):
    cfg = _cfg(tmp_path)
    lookup = FakeLookup(poms=["missing", "failed"], jars=["missing", "failed"])
    out, calls, journal, _ = _run(cfg, lookup, [_failed(_log("direct"))])
    assert calls == [0] and not _stub(tmp_path).exists()
    assert journal[-1]["reason"] == "jar-unavailable"


def test_denied_pom_counts_as_not_obtained_and_denied_jar_as_unavailable(tmp_path):
    cfg = _cfg(tmp_path)
    out, calls, _j, _m = _run(cfg, FakeLookup(poms=["denied", "failed"], jars=["ok"]),
                              [_failed(_log("direct")), _passed()])
    assert calls == [0, 1] and _stub(tmp_path).is_file()
    _stub(tmp_path).unlink()
    out, calls, journal, _m = _run(cfg, FakeLookup(poms=["missing", "failed"], jars=["denied", "failed"]),
                                   [_failed(_log("direct"))])
    assert calls == [0] and journal[-1]["reason"] == "jar-unavailable"


def test_jar_already_in_the_local_repository_counts(tmp_path):
    cfg = _cfg(tmp_path)
    jar = _stub(tmp_path).with_suffix(".jar")
    jar.parent.mkdir(parents=True)
    jar.write_bytes(b"PK")
    out, calls, journal, _ = _run(cfg, FakeLookup(poms=["missing", "failed"], jars=["failed", "failed"]),
                                  [_failed(_log("direct")), _passed()])
    assert calls == [0, 1]
    assert [e["jar_from"] for e in journal if e["event"] == "pomless_stub"] == ["local repository"]


def test_no_repository(tmp_path):
    cfg = _cfg(tmp_path)
    out, calls, journal, _ = _run(cfg, FakeLookup(poms=[], jars=[], urls=()), [_failed(_log("direct"))])
    assert calls == [0] and journal[-1]["reason"] == "no-repository" and not _stub(tmp_path).exists()


def test_the_offline_test_fake_never_leads_to_a_write(tmp_path, monkeypatch):
    """tests/conftest.py replaces RepositoryLookup with an offline fake: no file answers → nothing written anywhere."""
    import sbmig.build.repo_versions as rv
    assert rv.RepositoryLookup.__name__ == "OfflineRepositoryLookup"
    cfg = _cfg(tmp_path)
    journal = Journal()
    calls: list[int] = []
    pomless.run_with_stubs(cfg, lambda k: calls.append(k) or _failed(_log("direct")), journal=journal,
                           memo=pomless.PomlessMemo(), project_root=tmp_path)
    assert calls == [0] and journal[-1]["reason"] == "no-repository"
    assert not (tmp_path / "m2").exists()


def test_switch_off_behaves_as_before(tmp_path):
    cfg = _cfg(tmp_path, pomless_stub=False)
    lookup = FakeLookup(poms=["missing", "failed"], jars=["ok"])
    out, calls, journal, _ = _run(cfg, lookup, [_failed(_log("direct")), _passed()])
    assert out.exit_code == 1 and calls == [0] and lookup.asked == []
    assert not _stub(tmp_path).exists()
    assert [e["reason"] for e in journal] == ["off"]


def test_a_library_once_per_run_and_the_cap(tmp_path):
    cfg = _cfg(tmp_path, pomless_stub_max=1)
    memo = pomless.PomlessMemo()
    lookup = FakeLookup(poms=["missing", "failed"], jars=["ok"])
    # the stub exists but Maven still fails on it: not written again, not retried again
    _out, calls, _j, _m = _run(cfg, lookup, [_failed(_log("direct")), _failed(_log("direct"))], memo=memo)
    assert calls == [0, 1]
    _out, calls, journal, _m = _run(cfg, lookup, [_failed(_log("direct"))], memo=memo)
    assert calls == [0] and journal == []
    other = _log("direct").replace("pomless", "second")
    _out, calls, journal, _m = _run(cfg, lookup, [_failed(other)], memo=memo)
    assert calls == [0] and journal[-1]["reason"] == "cap"
    assert not _stub(tmp_path, a="second").exists()


def test_rounds_handle_one_library_each(tmp_path):
    cfg = _cfg(tmp_path)
    lookup = FakeLookup(poms=["missing", "failed"], jars=["ok"])
    second = _log("direct").replace("pomless", "second")
    out, calls, journal, memo = _run(cfg, lookup, [_failed(_log("direct")), _failed(second), _passed()])
    assert out.exit_code == 0 and calls == [0, 1, 2]
    assert [s["ga"] for s in memo.stubs] == ["com.example:pomless", "com.example:second"]


def test_an_existing_file_is_never_overwritten_and_counts_as_done(tmp_path):
    cfg = _cfg(tmp_path)
    stub = _stub(tmp_path)
    stub.parent.mkdir(parents=True)
    stub.write_text("<project>another pod</project>", encoding="utf-8")
    lookup = FakeLookup(poms=["missing", "failed"], jars=["ok"])
    out, calls, journal, _ = _run(cfg, lookup, [_failed(_log("direct")), _passed()])
    assert calls == [0, 1] and stub.read_text(encoding="utf-8") == "<project>another pod</project>"
    assert [e["created"] for e in journal if e["event"] == "pomless_stub"] == [False]
    assert lookup.asked == []                                   # nothing to prove: the file is there


def test_a_file_appearing_before_the_rename_counts_as_done(tmp_path, monkeypatch):
    stub = _stub(tmp_path)
    real_link = os.link

    def racing_link(src, dst):
        Path(dst).write_text("<project>other pod</project>", encoding="utf-8")
        return real_link(src, dst)
    monkeypatch.setattr(pomless.os, "link", racing_link)
    assert pomless.write_stub(stub, "com.example", "pomless", "1.0") is False
    assert stub.read_text(encoding="utf-8") == "<project>other pod</project>"
    assert not list(stub.parent.glob("*.tmp"))


def test_a_refused_coordinate_is_journaled(tmp_path):
    cfg = _cfg(tmp_path)
    bad = _log("direct").replace("com.example:pomless", "com.example:po..mless")
    out, calls, journal, _ = _run(cfg, FakeLookup(poms=["missing"], jars=["ok"]), [_failed(bad)])
    assert calls == [0] and journal[-1]["reason"] == "bad-coordinate"


def test_a_timed_out_or_passing_run_is_left_alone(tmp_path):
    cfg = _cfg(tmp_path)
    timed = ProcessResult(exit_code=-9, text=_log("direct"), duration_s=1, timed_out=True)
    _o, calls, journal, _m = _run(cfg, FakeLookup(poms=["missing"], jars=["ok"]), [timed])
    assert calls == [0] and journal == []


# ---------------------------------------------------------------------------- where the stub goes
def test_local_repository_follows_maven(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".m2").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(pomless, "maven_global_settings", lambda cfg: None)
    base = {"artifacts": {"parent": {"group_id": "g", "artifact_id": "a"}}}
    cfg = load_config(overrides={**base, "build": {"local_repo": str(tmp_path / "r1")}})
    assert pomless.maven_local_repo(cfg) == (tmp_path / "r1").absolute()
    cfg = load_config(overrides={**base, "build": {"local_repo": None, "settings_file": None}})
    assert pomless.maven_local_repo(cfg) == home / ".m2" / "repository"
    (home / ".m2" / "settings.xml").write_text("<settings><localRepository>${user.home}/repo2</localRepository>"
                                               "</settings>", encoding="utf-8")
    assert pomless.maven_local_repo(cfg) == home / "repo2"
    # with -s, ~/.m2/settings.xml is not read by Maven
    own = tmp_path / "own.xml"
    own.write_text("<settings/>", encoding="utf-8")
    cfg = load_config(overrides={**base, "build": {"local_repo": None, "settings_file": str(own)}})
    assert pomless.maven_local_repo(cfg) == home / ".m2" / "repository"
    gs = tmp_path / "global.xml"
    gs.write_text("<settings><localRepository>/srv/m2</localRepository></settings>", encoding="utf-8")
    monkeypatch.setattr(pomless, "maven_global_settings", lambda cfg: gs)
    assert pomless.maven_local_repo(cfg) == Path("/srv/m2")
    cfg = load_config(overrides={**base, "build": {"local_repo": None, "settings_file": str(own),
                                                   "extra_args": ["-Dmaven.repo.local=/x/repo"]}})
    assert pomless.maven_local_repo(cfg) == Path("/x/repo")


def test_handover_line_names_the_libraries():
    line = pomless.handover_line(["com.example:a:1.0", "com.example:b:2.0"])
    assert line.startswith("libraries with a jar but no pom in the repository: `com.example:a:1.0`, `com.example:b:2.0`")


# ---------------------------------------------------------------------------- a stub an earlier run wrote
def test_the_marker_is_shared_by_the_writer_and_the_check(tmp_path):
    stub = _stub(tmp_path)
    assert pomless.write_stub(stub, "com.example", "pomless", "1.0")
    assert pomless.STUB_MARKER in stub.read_text(encoding="utf-8")
    assert pomless.is_stub(stub)
    real = tmp_path / "real.pom"
    real.write_text("<project><modelVersion>4.0.0</modelVersion></project>", encoding="utf-8")
    assert not pomless.is_stub(real)
    late = tmp_path / "late.pom"                                   # the marker beyond the head is not read
    late.write_text("<!--" + " " * (pomless.HEAD_BYTES + 10) + pomless.STUB_MARKER + "-->", encoding="utf-8")
    assert not pomless.is_stub(late)
    assert not pomless.is_stub(tmp_path / "missing.pom")
    assert not pomless.is_stub(tmp_path)                           # a directory: unreadable, never raises


def _workspace(tmp_path):
    from sbmig.core.workspace import Workspace
    root = tmp_path / "proj"
    (root / "mod").mkdir(parents=True)
    (root / "pom.xml").write_text(
        "<project><modelVersion>4.0.0</modelVersion><groupId>com.example</groupId><artifactId>root</artifactId>"
        "<version>1.0</version><packaging>pom</packaging><modules><module>mod</module></modules>"
        "<properties><lib.version>3.0</lib.version></properties></project>", encoding="utf-8")
    (root / "mod" / "pom.xml").write_text(
        "<project><modelVersion>4.0.0</modelVersion><parent><groupId>com.example</groupId><artifactId>root</artifactId>"
        "<version>1.0</version></parent><artifactId>mod</artifactId><dependencies>"
        "<dependency><groupId>com.example</groupId><artifactId>declared</artifactId><version>${lib.version}</version>"
        "</dependency></dependencies></project>", encoding="utf-8")
    return Workspace(root, tmp_path / "run", "t-run")


def test_known_coordinates_come_from_trees_effective_poms_and_declarations(tmp_path):
    import json as _json
    ws = _workspace(tmp_path)
    logs = ws.logs_dir
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "deptree-root.txt").write_text(
        "com.example:root:pom:1.0\n+- com.example:direct:jar:1.0:compile\n|  \\- com.example:deep:jar:2.0:compile\n",
        encoding="utf-8")
    (logs / "deptree-root.meta.json").write_text(_json.dumps({"poms": "x", "includes": []}), encoding="utf-8")
    (logs / "deptree-root-abcd1234.txt").write_text("com.example:root:pom:1.0\n+- javax.servlet:servlet-api:jar:2.5"
                                                    ":provided\n", encoding="utf-8")
    (logs / "deptree-root-abcd1234.meta.json").write_text(_json.dumps({"poms": "x", "includes": ["javax.servlet:*"]}),
                                                          encoding="utf-8")
    (logs / "effective-depmgmt.json").write_text(_json.dumps(
        {"direct": {"com.example:eff": "4.0"}, "by_module": {"com.example:mod": {"direct": {"com.example:effmod": "5.0"}}}}),
        encoding="utf-8")
    got = pomless.known_coordinates(ws)
    for c in [("com.example", "direct", "1.0"), ("com.example", "deep", "2.0"), ("com.example", "eff", "4.0"),
              ("com.example", "effmod", "5.0"), ("com.example", "declared", "3.0")]:
        assert c in got, c
    assert ("javax.servlet", "servlet-api", "2.5") not in got        # a filtered (-Dincludes) tree is not the full list
    assert len(got) == len(set(got))


def test_stubs_used_finds_stubs_of_earlier_runs_only(tmp_path):
    cfg = _cfg(tmp_path)
    assert pomless.write_stub(_stub(tmp_path), "com.example", "pomless", "1.0")
    real = _stub(tmp_path, a="real")
    real.parent.mkdir(parents=True)
    real.write_text("<project/>", encoding="utf-8")
    coords = [("com.example", "pomless", "1.0"), ("com.example", "real", "1.0"), ("com.example", "absent", "1.0"),
              ("com/x", "bad", "1")]
    assert pomless.stubs_used(cfg, coords) == [
        {"ga": "com.example:pomless", "version": "1.0", "path": str(_stub(tmp_path))}]
    assert pomless.stubs_used(cfg, coords, skip={"com.example:pomless:1.0"}) == []


def test_stubs_used_is_capped(tmp_path, caplog):
    cfg = _cfg(tmp_path)
    for i in range(5):
        assert pomless.write_stub(_stub(tmp_path, a=f"lib{i}"), "com.example", f"lib{i}", "1.0")
    coords = [("com.example", f"lib{i}", "1.0") for i in range(5)]
    with caplog.at_level("INFO", logger="sbmig.build.pomless"):
        assert len(pomless.stubs_used(cfg, coords, limit=3)) == 3
    assert "only the first 3" in caplog.text


def test_handover_line_is_true_for_this_run_and_an_earlier_one():
    line = pomless.handover_line(["com.example:a:1.0"])
    assert "the build used a minimal pom" in line and "sbmig wrote a minimal pom for each" not in line
