"""v2.1z-cj: a library that exists only as a jar with a classifier, without a pom, stops OpenRewrite.

Detection runs on REAL rewrite-maven-plugin 6.46.1 output (Maven 3.9.10) captured against a repository that holds only
`<a>-1.0-RELEASE.jar` files: `tests/build/logs/real-rewrite-classified-pomless.log` (paths and the repository host
replaced by neutral ones). The proof and the stub writer run against fakes from `test_pomless.py`; every test that may
write a stub points `build.local_repo` at a tmp folder."""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

from sbmig.build import pomless
from sbmig.core.config import DEFAULT_CONFIG_PATH, Config
from tests.build.test_pomless import FakeLookup, _cfg, _failed, _log, _passed, _run, _stub, _workspace

LOG = Path(__file__).parent / "logs" / "real-rewrite-classified-pomless.log"
G = "com.example.fw"
CORE, UTIL = "example-fw-core", "example-fw-util"


def _text() -> str:
    return LOG.read_text(encoding="utf-8")


def _folder(tmp_path, a=CORE, v="1.0", g=G) -> Path:
    return tmp_path / "m2" / Path(*g.split(".")) / a / v


def _jar(tmp_path, a=CORE, v="1.0", classifier="RELEASE", data=b"PK\x03\x04") -> Path:
    f = _folder(tmp_path, a, v)
    f.mkdir(parents=True, exist_ok=True)
    p = f / f"{a}-{v}-{classifier}.jar"
    p.write_bytes(data)
    return p


def _lookup():
    return FakeLookup(poms=["missing", "missing"], jars=["missing", "missing"])


# ---------------------------------------------------------------------------- detection
def test_detects_the_captured_openrewrite_failure_in_log_order():
    assert pomless.detect_classified(_text()) == [(G, CORE, "1.0"), (G, UTIL, "1.0")]
    assert pomless.detect(_text()) == []                       # the first form never reads this text


def test_the_unresolved_placeholder_sentence_is_another_failure():
    text = "[ERROR]     <!--~~(Unable to download POM com.example:lib:${lib.version}. Version contains unresolved " \
           "property placeholder.)~~>-->"
    assert pomless.detect_classified(text) == []


def test_the_same_coordinate_in_two_blocks_once_and_a_did_not_attempt_block():
    block1 = ("[ERROR]     <!--~~(Unable to download POM: com.example.fw:example-fw-core:1.0. Tried repositories:\n"
              "[ERROR] https://nexus.example.invalid/repository/releases/: HTTP 404)~~>--><dependency/>\n")
    block2 = ("[ERROR]     <!--~~(Unable to download POM: com.example.fw:example-fw-core:1.0. Tried repositories:\n"
              "[ERROR] https://nexus.example.invalid/repository/releases/: Did not attempt to download because of a "
              "previous failure to retrieve from this repository.)~~>--><dependency/>\n")
    block3 = ("[ERROR]     <!--~~(Unable to download POM: com.example.fw:example-fw-web:2.3.1.RELEASE-b7. Tried "
              "repositories:\n[ERROR] https://nexus.example.invalid/: Did not attempt to download because of a "
              "previous failure)~~>-->\n")
    assert pomless.detect_classified(block1 + block2 + block3) == [
        (G, CORE, "1.0"), (G, "example-fw-web", "2.3.1.RELEASE-b7")]


# ---------------------------------------------------------------------------- proof
def test_proof_succeeds_with_the_classified_jar_in_the_local_repository(tmp_path):
    _jar(tmp_path)
    lookup = _lookup()
    assert pomless.prove_classified(lookup, tmp_path / "m2", G, CORE, "1.0") == ("example-fw-core-1.0-RELEASE.jar", "")
    assert lookup.asked == ["pom", "jar"]


def test_proof_pom_present_before_any_repository_request(tmp_path):
    _jar(tmp_path)
    (_folder(tmp_path) / f"{CORE}-1.0.pom").write_text("<project/>", encoding="utf-8")
    lookup = _lookup()
    assert pomless.prove_classified(lookup, tmp_path / "m2", G, CORE, "1.0") == (None, "pom-present")
    assert lookup.asked == []


def test_proof_main_jar_in_the_local_repository(tmp_path):
    _jar(tmp_path)
    (_folder(tmp_path) / f"{CORE}-1.0.jar").write_bytes(b"PK")
    lookup = _lookup()
    assert pomless.prove_classified(lookup, tmp_path / "m2", G, CORE, "1.0") == (None, "main-jar-available")
    assert lookup.asked == []


@pytest.mark.parametrize("names", [[], ["example-fw-core-1.0-sources.jar"], ["example-fw-core-1.0-javadoc.jar"],
                                   ["example-fw-core-1.0--x.jar"], ["example-fw-core-1.0-a b.jar"],
                                   ["example-fw-core-1.0-RELEASE.jar.sha1"], ["example-fw-core-1.0-.jar"]])
def test_proof_jar_unavailable(tmp_path, names):
    f = _folder(tmp_path)
    f.mkdir(parents=True)
    for n in names:
        (f / n).write_bytes(b"PK")
    lookup = _lookup()
    assert pomless.prove_classified(lookup, tmp_path / "m2", G, CORE, "1.0") == (None, "jar-unavailable")
    assert lookup.asked == []


def test_proof_jar_unavailable_for_an_empty_jar_a_directory_and_no_folder(tmp_path):
    _jar(tmp_path, data=b"")
    (_folder(tmp_path) / f"{CORE}-1.0-dir.jar").mkdir()
    assert pomless.prove_classified(_lookup(), tmp_path / "m2", G, CORE, "1.0") == (None, "jar-unavailable")
    assert pomless.prove_classified(_lookup(), tmp_path / "m2", G, "absent", "1.0") == (None, "jar-unavailable")


def test_proof_first_classified_jar_by_name(tmp_path):
    _jar(tmp_path, classifier="RELEASE")
    _jar(tmp_path, classifier="BUILD")
    _jar(tmp_path, classifier="sources")
    assert pomless.prove_classified(_lookup(), tmp_path / "m2", G, CORE, "1.0") == ("example-fw-core-1.0-BUILD.jar", "")


def test_proof_no_repository(tmp_path):
    _jar(tmp_path)
    no_urls = FakeLookup(poms=[], jars=[], urls=())
    assert pomless.prove_classified(no_urls, tmp_path / "m2", G, CORE, "1.0") == (None, "no-repository")

    class NoAnswers:
        def repository_urls(self):
            return [("https://nexus.example.invalid/", None, "test")]
    assert pomless.prove_classified(NoAnswers(), tmp_path / "m2", G, CORE, "1.0") == (None, "no-repository")


def test_proof_pom_available_and_main_jar_available_in_a_repository(tmp_path):
    _jar(tmp_path)
    lookup = FakeLookup(poms=["missing", "ok"], jars=["missing", "missing"])
    assert pomless.prove_classified(lookup, tmp_path / "m2", G, CORE, "1.0") == (None, "pom-available")
    assert lookup.asked == ["pom"]
    lookup = FakeLookup(poms=["missing", "failed"], jars=["denied", "ok"])
    assert pomless.prove_classified(lookup, tmp_path / "m2", G, CORE, "1.0") == (None, "main-jar-available")
    # a denied / failed answer for the main jar does not count as "available"
    lookup = FakeLookup(poms=["denied", "failed"], jars=["denied", "failed"])
    assert pomless.prove_classified(lookup, tmp_path / "m2", G, CORE, "1.0")[0] == "example-fw-core-1.0-RELEASE.jar"


# ---------------------------------------------------------------------------- the stub text
def test_the_classified_stub_text(tmp_path):
    text = pomless.classified_stub_text(G, CORE, "1.0", "example-fw-core-1.0-RELEASE.jar")
    head = text.encode("utf-8")[:pomless.HEAD_BYTES]
    assert f"{pomless.STUB_MARKER} {G}:{CORE}:1.0".encode() in head
    root = ET.fromstring(text.encode("utf-8"))
    ns = "{http://maven.apache.org/POM/4.0.0}"
    assert root.findtext(f"{ns}packaging") == "pom"
    assert (root.findtext(f"{ns}groupId"), root.findtext(f"{ns}artifactId"), root.findtext(f"{ns}version")) == (
        G, CORE, "1.0")
    assert root.find(f"{ns}dependencies") is None
    comment = text.split("<!--", 1)[1].split("-->", 1)[0]
    assert "--" not in comment
    for part in ("example-fw-core-1.0-RELEASE.jar", "OpenRewrite needs a pom", "no main artifact",
                 "Delete this file when the real pom is uploaded"):
        assert part in comment.replace("\n     ", " "), part
    path = _folder(tmp_path) / f"{CORE}-1.0.pom"
    assert pomless.write_stub(path, G, CORE, "1.0", text)
    assert path.read_text(encoding="utf-8") == text and pomless.is_stub(path)
    assert not pomless.write_stub(path, G, CORE, "1.0", "<project/>")           # never over an existing file
    assert path.read_text(encoding="utf-8") == text


# ---------------------------------------------------------------------------- the stub loop
def _stub_events(journal):
    return [e for e in journal if e["event"] == "pomless_stub"]


def test_both_libraries_get_a_stub_and_the_command_runs_again(tmp_path, caplog):
    cfg = _cfg(tmp_path)
    _jar(tmp_path, CORE)
    _jar(tmp_path, UTIL)
    with caplog.at_level("INFO", logger="sbmig.build.pomless"):
        out, calls, journal, memo = _run(cfg, _lookup(), [_failed(_text()), _passed()])
    assert out.exit_code == 0 and calls == [0, 1]
    core, util = (_folder(tmp_path, a) / f"{a}-1.0.pom" for a in (CORE, UTIL))
    assert journal == [
        {"event": "pomless_stub", "ga": f"{G}:{CORE}", "version": "1.0", "path": str(core),
         "jar_from": "local repository", "created": True, "form": "classified", "jar": "example-fw-core-1.0-RELEASE.jar"},
        {"event": "pomless_stub", "ga": f"{G}:{UTIL}", "version": "1.0", "path": str(util),
         "jar_from": "local repository", "created": True, "form": "classified", "jar": "example-fw-util-1.0-RELEASE.jar"},
    ]
    assert "<packaging>pom</packaging>" in core.read_text(encoding="utf-8") and pomless.is_stub(core)
    assert [s["ga"] for s in memo.stubs] == [f"{G}:{CORE}", f"{G}:{UTIL}"]
    assert "has only a jar with a classifier (example-fw-core-1.0-RELEASE.jar) and no pom" in caplog.text
    assert "running the same command again" in caplog.text
    assert not list(core.parent.glob("*.tmp"))


def test_a_second_failure_naming_the_same_library_does_not_loop(tmp_path):
    cfg = _cfg(tmp_path)
    _jar(tmp_path, CORE)
    _jar(tmp_path, UTIL)
    out, calls, journal, _ = _run(cfg, _lookup(), [_failed(_text()), _failed(_text()), _passed()])
    assert out.exit_code == 1 and calls == [0, 1]
    assert len(_stub_events(journal)) == 2 and len(journal) == 2


def test_a_pom_already_present_is_not_ready_and_does_not_rerun(tmp_path):
    cfg = _cfg(tmp_path)
    _jar(tmp_path, CORE)
    (_folder(tmp_path) / f"{CORE}-1.0.pom").write_text("<project>real</project>", encoding="utf-8")
    text = _text().replace(UTIL, "absent-lib")
    lookup = _lookup()
    out, calls, journal, memo = _run(cfg, lookup, [_failed(text), _passed()])
    assert out.exit_code == 1 and calls == [0] and memo.stubs == []
    assert journal == [{"event": "pomless_check", "ga": f"{G}:{CORE}", "version": "1.0", "reason": "pom-present"},
                       {"event": "pomless_check", "ga": f"{G}:absent-lib", "version": "1.0", "reason": "jar-unavailable"}]
    assert lookup.asked == []
    assert (_folder(tmp_path) / f"{CORE}-1.0.pom").read_text(encoding="utf-8") == "<project>real</project>"


def test_a_file_another_pod_wrote_in_between_counts_as_ready(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _jar(tmp_path, CORE)
    _jar(tmp_path, UTIL)
    real_write = pomless.write_stub

    def racing(path, g, a, v, text=None):
        path.write_text("<project>other pod</project>", encoding="utf-8")
        return real_write(path, g, a, v, text)
    monkeypatch.setattr(pomless, "write_stub", racing)
    _out, calls, journal, _ = _run(cfg, _lookup(), [_failed(_text()), _passed()])
    assert calls == [0, 1]
    assert [e["created"] for e in _stub_events(journal)] == [False, False]


def test_switch_off(tmp_path):
    cfg = _cfg(tmp_path, pomless_stub=False)
    _jar(tmp_path, CORE)
    lookup = _lookup()
    _out, calls, journal, _ = _run(cfg, lookup, [_failed(_text()), _passed()])
    assert calls == [0] and lookup.asked == [] and [e["reason"] for e in journal] == ["off", "off"]
    assert not (_folder(tmp_path) / f"{CORE}-1.0.pom").exists()


def test_the_cap_is_shared_with_the_first_form(tmp_path):
    cfg = _cfg(tmp_path, pomless_stub_max=2)
    _jar(tmp_path, CORE)
    _jar(tmp_path, UTIL)
    first = _log("direct")                                 # one stub of the first form, its jar already local
    _stub(tmp_path).parent.mkdir(parents=True)
    _stub(tmp_path).with_suffix(".jar").write_bytes(b"PK")
    _out, calls, journal, _m = _run(cfg, FakeLookup(poms=["missing", "failed"], jars=["missing", "failed"]),
                                    [_failed(first + "\n" + _text()), _passed()])
    assert calls == [0, 1]
    assert [e.get("form") for e in _stub_events(journal)] == [None, "classified"]
    assert journal[-1] == {"event": "pomless_check", "ga": f"{G}:{UTIL}", "version": "1.0", "reason": "cap"}
    assert not (_folder(tmp_path, UTIL) / f"{UTIL}-1.0.pom").exists()


def test_bad_coordinate_and_unknown_local_repository(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    bad = _text().replace(f"{G}:{CORE}", f"{G}:ex..core")
    _o, calls, journal, _m = _run(cfg, _lookup(), [_failed(bad)])
    assert journal[0]["reason"] == "bad-coordinate"
    monkeypatch.setattr(pomless, "maven_local_repo", lambda cfg: None)
    _o, calls, journal, _m = _run(cfg, _lookup(), [_failed(_text())])
    assert calls == [0] and [e["reason"] for e in journal] == ["no-repository", "no-repository"]


def test_the_first_form_keeps_its_journal_keys(tmp_path):
    cfg = _cfg(tmp_path)
    _out, _calls, journal, _m = _run(cfg, FakeLookup(poms=["missing", "failed"], jars=["ok"]),
                                     [_failed(_log("direct")), _passed()])
    [ev] = _stub_events(journal)
    assert set(ev) == {"event", "ga", "version", "path", "jar_from", "created"}
    assert "<packaging>jar</packaging>" in _stub(tmp_path).read_text(encoding="utf-8")


def test_known_coordinates_include_a_classified_jar_dependency(tmp_path):
    ws = _workspace(tmp_path)
    logs = ws.logs_dir
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "deptree-root.txt").write_text(
        "com.example:root:pom:1.0\n+- com.example.fw:example-fw-core:jar:RELEASE:1.0:compile\n"
        "+- com.example:zipped:zip:1.0:compile\n", encoding="utf-8")
    (logs / "deptree-root.meta.json").write_text(json.dumps({"poms": "x"}), encoding="utf-8")
    pom = ws.root / "mod" / "pom.xml"
    pom.write_text(pom.read_text(encoding="utf-8").replace(
        "</dependencies>", "<dependency><groupId>com.example.fw</groupId><artifactId>example-fw-util</artifactId>"
        "<version>1.0</version><classifier>RELEASE</classifier></dependency>"
        "<dependency><groupId>com.example</groupId><artifactId>war-dep</artifactId><version>1.0</version>"
        "<type>war</type></dependency></dependencies>"), encoding="utf-8")
    got = pomless.known_coordinates(ws)
    assert (G, CORE, "1.0") in got and (G, UTIL, "1.0") in got
    assert ("com.example", "zipped", "1.0") not in got and ("com.example", "war-dep", "1.0") not in got
    # a later run finds the stub through these coordinates
    cfg = _cfg(tmp_path)
    path = _folder(tmp_path) / f"{CORE}-1.0.pom"
    assert pomless.write_stub(path, G, CORE, "1.0", pomless.classified_stub_text(G, CORE, "1.0", "x-RELEASE.jar"))
    assert pomless.stubs_used(cfg, got) == [{"ga": f"{G}:{CORE}", "version": "1.0", "path": str(path)}]


# ---------------------------------------------------------------------------- the default cap
def test_the_default_cap_is_60_in_the_model_and_in_default_yml():
    assert Config.model_validate({"artifacts": {"parent": {"group_id": "g", "artifact_id": "a"}}}
                                 ).build.pomless_stub_max == 60
    data = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    assert data["build"]["pomless_stub_max"] == 60


def test_journal_is_optional(tmp_path):
    cfg = _cfg(tmp_path)
    _jar(tmp_path, CORE)
    assert pomless._stub_round(cfg, _text(), journal=None, memo=pomless.PomlessMemo(), project_root=None,
                               lookup=_lookup()) == 1


# ---------------------------------------------------------------------------- Maven's warnings as further candidates
SENTENCE = ("[ERROR]     <!--~~(Unable to download POM: com.example.fw:example-fw-core:1.0. Tried repositories:\n"
            "[ERROR] https://nexus.example.invalid/repository/releases/: HTTP 404)~~>-->\n")


def _warning(coord: str) -> str:
    return f"[WARNING] The POM for {coord} is missing, no dependency information available\n"


def test_the_sentence_and_the_warnings_are_read_apart():
    text = (_warning("com.example.fw:example-fw-web:jar:RELEASE:1.0") + _warning("com.example.fw:example-fw-util:jar:RELEASE:1.0")
            + _warning("com.example.fw:example-fw-core:jar:RELEASE:1.0") + SENTENCE
            + _warning("com.example.fw:example-fw-web:jar:RELEASE:1.0"))
    assert pomless.detect_classified(text) == [(G, CORE, "1.0")]
    assert pomless.classified_warnings(text) == [(G, "example-fw-web", "1.0"), (G, UTIL, "1.0"), (G, CORE, "1.0")]


def test_warnings_are_read_without_any_condition_but_start_nothing_alone(tmp_path):
    text = _warning("com.example.fw:example-fw-web:jar:RELEASE:1.0") + "[INFO] BUILD FAILURE\n"
    assert pomless.detect_classified(text) == []
    assert pomless.classified_warnings(text) == [(G, "example-fw-web", "1.0")]
    _jar(tmp_path, "example-fw-web")
    _out, calls, journal, _m = _run(_cfg(tmp_path), _lookup(), [_failed(text), _passed()])
    assert calls == [0] and journal == []


@pytest.mark.parametrize("coord", ["com.example.fw:example-fw-web:jar:1.0", "com.example.fw:example-fw-web:zip:RELEASE:1.0",
                                   "com.example.fw:example-fw-web:jar:RELEASE:1.0:compile"])
def test_a_warning_that_is_not_a_classified_jar_is_not_taken(coord):
    assert pomless.classified_warnings(SENTENCE + _warning(coord)) == []


def test_the_captured_log_names_each_library_once_with_its_warnings(tmp_path):
    assert pomless.detect_classified(_text()) == [(G, CORE, "1.0"), (G, UTIL, "1.0")]
    assert pomless.classified_warnings(_text()) == [(G, CORE, "1.0"), (G, UTIL, "1.0")]
    for a in (CORE, UTIL):
        _jar(tmp_path, a)
    _out, calls, journal, _m = _run(_cfg(tmp_path), _lookup(), [_failed(_text()), _passed()])
    assert calls == [0, 1] and len(journal) == 2                 # sentence and warning name a library once


def _healthy_warnings(tmp_path) -> str:
    for a in (UTIL, "example-fw-web"):
        _jar(tmp_path, a)
    return (_warning("com.example.fw:example-fw-util:jar:RELEASE:1.0")
            + _warning("com.example.fw:example-fw-web:jar:RELEASE:1.0"))


def test_a_refused_snapshot_in_the_sentence_leaves_the_warnings_alone(tmp_path):
    cfg = _cfg(tmp_path)
    text = SENTENCE.replace("example-fw-core:1.0.", "example-fw-core:1.0-SNAPSHOT.") + _healthy_warnings(tmp_path)
    out, calls, journal, memo = _run(cfg, _lookup(), [_failed(text), _passed()])
    assert out.exit_code == 1 and calls == [0] and memo.stubs == []
    assert journal == [{"event": "pomless_check", "ga": f"{G}:{CORE}", "version": "1.0-SNAPSHOT", "reason": "snapshot"}]
    assert not (_folder(tmp_path, UTIL) / f"{UTIL}-1.0.pom").exists()


def test_a_sentence_library_whose_pom_is_available_leaves_the_warnings_alone(tmp_path):
    cfg = _cfg(tmp_path)
    _jar(tmp_path, CORE)
    text = SENTENCE + _healthy_warnings(tmp_path)
    lookup = FakeLookup(poms=["missing", "ok"], jars=["missing", "missing"])
    _out, calls, journal, memo = _run(cfg, lookup, [_failed(text), _passed()])
    assert calls == [0] and memo.stubs == []
    assert [e["reason"] for e in journal] == ["pom-available"] and journal[0]["ga"] == f"{G}:{CORE}"
    assert "example-fw-util:1.0" not in " ".join(memo.handled)   # not handled: a later round may still take them


def test_a_sentence_library_handled_earlier_does_not_open_the_warnings(tmp_path):
    cfg = _cfg(tmp_path)
    memo = pomless.PomlessMemo()
    memo.handled.add(f"{G}:{CORE}:1.0")
    text = SENTENCE + _healthy_warnings(tmp_path)
    _out, calls, journal, _m = _run(cfg, _lookup(), [_failed(text), _passed()], memo=memo)
    assert calls == [0] and journal == [] and memo.stubs == []


def test_one_rerun_for_every_library_of_the_log(tmp_path):
    cfg = _cfg(tmp_path)
    for a in (CORE, UTIL, "example-fw-web"):
        _jar(tmp_path, a)
    text = SENTENCE + _warning("com.example.fw:example-fw-util:jar:RELEASE:1.0") \
        + _warning("com.example.fw:example-fw-web:jar:RELEASE:1.0")
    out, calls, journal, _m = _run(cfg, _lookup(), [_failed(text), _passed()])
    assert out.exit_code == 0 and calls == [0, 1]
    assert [e["ga"] for e in _stub_events(journal)] == [f"{G}:{CORE}", f"{G}:{UTIL}", f"{G}:example-fw-web"]


# ---------------------------------------------------------------------------- snapshots
@pytest.mark.parametrize("version", ["1.0-SNAPSHOT", "1.0-20260101.120000-1"])
def test_a_snapshot_gets_no_stub(tmp_path, version):
    cfg = _cfg(tmp_path)
    _jar(tmp_path, CORE, v=version)
    lookup = _lookup()
    assert pomless.prove_classified(lookup, tmp_path / "m2", G, CORE, version) == (None, "snapshot")
    text = SENTENCE.replace("example-fw-core:1.0.", f"example-fw-core:{version}.")
    out, calls, journal, memo = _run(cfg, lookup, [_failed(text), _passed()])
    assert out.exit_code == 1 and calls == [0] and memo.stubs == [] and lookup.asked == []
    assert journal == [{"event": "pomless_check", "ga": f"{G}:{CORE}", "version": version, "reason": "snapshot"}]
    assert not (_folder(tmp_path, v=version) / f"{CORE}-{version}.pom").exists()


# ---------------------------------------------------------------------------- links and folders
def test_a_link_named_like_a_classified_jar_is_not_counted(tmp_path):
    target = tmp_path / "real.jar"
    target.write_bytes(b"PK")
    f = _folder(tmp_path)
    f.mkdir(parents=True)
    try:
        (f / f"{CORE}-1.0-RELEASE.jar").symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("this platform cannot make a symbolic link")
    assert pomless.prove_classified(_lookup(), tmp_path / "m2", G, CORE, "1.0") == (None, "jar-unavailable")


def test_a_folder_that_cannot_be_listed_ends_at_jar_unavailable(tmp_path, monkeypatch):
    _jar(tmp_path)
    real_iterdir = Path.iterdir

    def refused(self):
        if self == _folder(tmp_path):
            raise PermissionError("denied")
        return real_iterdir(self)
    monkeypatch.setattr(Path, "iterdir", refused)
    assert pomless.prove_classified(_lookup(), tmp_path / "m2", G, CORE, "1.0") == (None, "jar-unavailable")
    # the version folder is a file, not a folder
    f = _folder(tmp_path, a="filelib")
    f.parent.mkdir(parents=True)
    f.write_text("x", encoding="utf-8")
    assert pomless.prove_classified(_lookup(), tmp_path / "m2", G, "filelib", "1.0") == (None, "jar-unavailable")
