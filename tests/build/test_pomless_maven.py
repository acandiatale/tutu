"""v2.1z-ax with real Maven: the cluster's case on loopback.

Repository A (a tiny HTTP server started here) serves the jar of `com.example:pomless:1.0` and answers 404 for its
pom; repository B answers 502 for everything (a Central proxy in a closed network that never cached the path). A tiny
project depends on the library; `build.local_repo` is a tmp folder (`maven.repo.local.tail` reads the plugins already
in the developer's `~/.m2` without writing there). Needs `mvn`, a JDK 21 (`JAVA21_HOME`) and network to Maven
Central (the lookup asks Central for the pom too). Run with `-m integration`."""
from __future__ import annotations

import http.server
import io
import json
import os
import shutil
import threading
import zipfile
from pathlib import Path

import pytest

from sbmig.build import MavenRunner, pomless
from sbmig.build.repo_versions import RepositoryLookup as RealLookup
from sbmig.core.config import load_config
from sbmig.core.workspace import Workspace

pytestmark = pytest.mark.integration


def _jar() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
        z.writestr("com/example/pomless/marker.txt", "x")
    return buf.getvalue()


JAR = _jar()
JAR_PATH = "/com/example/pomless/1.0/pomless-1.0.jar"
POM_PATH = "/com/example/pomless/1.0/pomless-1.0.pom"
REAL_POM = (b"<project><modelVersion>4.0.0</modelVersion><groupId>com.example</groupId><artifactId>pomless</artifactId>"
            b"<version>1.0</version></project>")


class Repo:
    """One loopback repository: `files` (path → bytes) are served, anything else is 404; `fail` answers 502."""

    def __init__(self, files: dict[str, bytes] | None = None, fail: bool = False):
        self.files, self.fail, self.seen = dict(files or {}), fail, []
        repo = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _answer(self, body: bool):
                repo.seen.append((self.command, self.path))
                data = None if repo.fail else repo.files.get(self.path)
                code = 502 if repo.fail else (200 if data is not None else 404)
                self.send_response(code)
                self.send_header("Content-Length", str(len(data) if data is not None else 0))
                self.end_headers()
                if body and data is not None:
                    self.wfile.write(data)

            def do_GET(self):
                self._answer(True)

            def do_HEAD(self):
                self._answer(False)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.timeout = 5
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)


@pytest.fixture()
def repos():
    made: list[Repo] = []

    def make(files=None, fail=False) -> Repo:
        r = Repo(files, fail)
        made.append(r)
        return r
    try:
        yield make
    finally:
        for r in made:
            r.close()


@pytest.fixture(autouse=True)
def real_lookup(monkeypatch):
    if shutil.which("mvn") is None or not os.environ.get("JAVA21_HOME"):
        pytest.skip("needs mvn and JAVA21_HOME")
    monkeypatch.setattr("sbmig.build.repo_versions.RepositoryLookup", RealLookup)


def _project(root: Path, a: Repo, b: Repo, *, reactor: bool = False) -> Path:
    repos = (f"<repositories><repository><id>repo-a</id><url>{a.url}</url></repository>"
             f"<repository><id>maven-central-proxy</id><url>{b.url}</url></repository></repositories>")
    dep = ("<dependencies><dependency><groupId>com.example</groupId><artifactId>pomless</artifactId>"
           "<version>1.0</version></dependency></dependencies>")
    head = ("<project xmlns='http://maven.apache.org/POM/4.0.0'><modelVersion>4.0.0</modelVersion>"
            "<properties><maven.compiler.release>21</maven.compiler.release>"
            "<project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties>")
    root.mkdir(parents=True)
    if not reactor:
        (root / "pom.xml").write_text(head + "<groupId>com.example</groupId><artifactId>app</artifactId>"
                                      f"<version>1.0</version>{repos}{dep}</project>", encoding="utf-8")
    else:
        (root / "pom.xml").write_text(head + "<groupId>com.example</groupId><artifactId>root</artifactId>"
                                      "<version>1.0</version><packaging>pom</packaging><modules><module>mod-a</module>"
                                      f"</modules>{repos}</project>", encoding="utf-8")
        (root / "mod-a").mkdir()
        (root / "mod-a" / "pom.xml").write_text(head + "<parent><groupId>com.example</groupId><artifactId>root</artifactId>"
                                                "<version>1.0</version></parent><artifactId>mod-a</artifactId>"
                                                f"{dep}</project>", encoding="utf-8")
    return root


def _cfg(tmp_path: Path, **build):
    settings = tmp_path / "settings.xml"
    settings.write_text("<settings/>\n", encoding="utf-8")
    opts = {"mvn": "mvn", "java_home": os.environ["JAVA21_HOME"], "local_repo": str(tmp_path / "m2"),
            "settings_file": str(settings), "timeout_minutes": 5, "repository_timeout_seconds": 10,
            "extra_args": ["-B", f"-Dmaven.repo.local.tail={Path.home() / '.m2' / 'repository'}"]}
    opts.update(build)
    return load_config(overrides={"artifacts": {"parent": {"group_id": "g", "artifact_id": "a"}}, "build": opts})


def _events(ws: Workspace) -> list[dict]:
    return [json.loads(x) for x in ws.journal_path.read_text(encoding="utf-8").splitlines()]


def _stub(tmp_path: Path) -> Path:
    return tmp_path / "m2" / "com" / "example" / "pomless" / "1.0" / "pomless-1.0.pom"


def _show(title: str, res, ws: Workspace) -> None:
    print(f"\n--- {title}: ok={res.ok} log={Path(res.log_path).name}")
    for e in _events(ws):
        if e["event"] in ("build", "pomless_stub", "pomless_check", "build_negative_cache_retry"):
            print("   ", {k: v for k, v in e.items() if k in ("event", "ok", "label", "ga", "version", "reason",
                                                                 "jar_from", "created", "path")})


def test_without_the_feature_the_build_stops_on_the_descriptor(tmp_path, repos):
    a, b = repos({JAR_PATH: JAR}), repos(fail=True)
    ws = Workspace(_project(tmp_path / "proj", a, b), tmp_path / "run", "off")
    res = MavenRunner(ws, _cfg(tmp_path, pomless_stub=False)).run("package", skip_test_compile=True)
    _show("pomless_stub: false", res, ws)
    text = Path(res.log_path).read_text(encoding="utf-8")
    assert not res.ok
    assert "Failed to read artifact descriptor for com.example:pomless:jar:1.0" in text
    assert "Could not transfer artifact com.example:pomless:pom:1.0 from/to maven-central-proxy" in text
    assert not _stub(tmp_path).exists()
    assert [e["reason"] for e in _events(ws) if e["event"] == "pomless_check"] == ["off"]


@pytest.mark.parametrize("reactor", [False, True])
def test_with_the_feature_the_stub_is_written_and_the_build_passes(tmp_path, repos, reactor):
    a, b = repos({JAR_PATH: JAR}), repos(fail=True)
    ws = Workspace(_project(tmp_path / "proj", a, b, reactor=reactor), tmp_path / "run", "on")
    res = MavenRunner(ws, _cfg(tmp_path)).run("package", skip_test_compile=True)
    _show(f"pomless_stub: true (reactor={reactor})", res, ws)
    assert res.ok, res.summary
    assert res.log_path.endswith("-pomless1.log")
    stub = _stub(tmp_path)
    print(stub.read_text(encoding="utf-8"))
    assert "<artifactId>pomless</artifactId>" in stub.read_text(encoding="utf-8")
    [ev] = [e for e in _events(ws) if e["event"] == "pomless_stub"]
    assert {k: v for k, v in ev.items() if k not in ("ts", "run_id")} == {
        "event": "pomless_stub", "ga": "com.example:pomless", "version": "1.0", "path": str(stub), "jar_from": a.url,
        "created": True}
    assert (stub.parent / "pomless-1.0.jar").is_file()           # Maven took the jar from repository A
    assert ("HEAD", JAR_PATH) in a.seen and ("GET", JAR_PATH) in a.seen      # sbmig HEAD, Maven's own download
    assert not [e for e in _events(ws) if e["event"] == "build_negative_cache_retry"]


def test_pom_served_by_a_while_b_fails_needs_nothing(tmp_path, repos):
    """Maven itself passes (the pom comes from A; B's 502 does not matter once a repository returned it) — and the
    proof would refuse anyway (`pom-available`)."""
    a, b = repos({JAR_PATH: JAR, POM_PATH: REAL_POM}), repos(fail=True)
    root = _project(tmp_path / "proj", a, b)
    ws = Workspace(root, tmp_path / "run", "pom")
    cfg = _cfg(tmp_path)
    res = MavenRunner(ws, cfg).run("package", skip_test_compile=True)
    _show("pom served by A", res, ws)
    assert res.ok and not [e for e in _events(ws) if e["event"].startswith("pomless")]
    lookup = RealLookup(cfg, project_root=root)
    assert pomless.prove(lookup, tmp_path / "m2-empty", "com.example", "pomless", "1.0") == (None, "pom-available")


def test_jar_served_by_nobody_writes_nothing(tmp_path, repos):
    a, b = repos({}), repos(fail=True)
    ws = Workspace(_project(tmp_path / "proj", a, b), tmp_path / "run", "nojar")
    res = MavenRunner(ws, _cfg(tmp_path)).run("package", skip_test_compile=True)
    _show("jar nowhere", res, ws)
    assert not res.ok and not _stub(tmp_path).exists()
    assert [e["reason"] for e in _events(ws) if e["event"] == "pomless_check"] == ["jar-unavailable"]


def test_a_later_run_builds_on_the_stub_and_finds_it(tmp_path, repos):
    """The second project builds silently on the first one's stub (no pomless event from Maven), and the finalize check
    (`stubs_used(known_coordinates(ws))`, no Maven call) still names the library."""
    a, b = repos({JAR_PATH: JAR}), repos(fail=True)
    cfg = _cfg(tmp_path)
    ws1 = Workspace(_project(tmp_path / "one", a, b), tmp_path / "run1", "one")
    assert MavenRunner(ws1, cfg).run("package", skip_test_compile=True).ok
    ws2 = Workspace(_project(tmp_path / "two", a, b), tmp_path / "run2", "two")
    res = MavenRunner(ws2, cfg).run("package", skip_test_compile=True)
    _show("later run", res, ws2)
    assert res.ok and not [e for e in _events(ws2) if e["event"].startswith("pomless")]
    found = pomless.stubs_used(cfg, pomless.known_coordinates(ws2))
    print("    stubs_used:", found)
    assert found == [{"ga": "com.example:pomless", "version": "1.0", "path": str(_stub(tmp_path))}]
