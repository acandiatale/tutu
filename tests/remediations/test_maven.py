"""maven.py: server command lines, readiness, allowed hosts, dependency:tree resolution, packaged jars (fake runner only)."""
from __future__ import annotations

import io
import types
import zipfile
from pathlib import Path

import pytest

from sbmig.build.mvn import ProcessResult
from sbmig.core.config import BuildConfig, RemediationConfig
from sbmig.remediations.interfaces import MavenGateway
from sbmig.remediations.maven import TEST_SKIP, ServerMaven, java_home_major, strip_unsafe_args, summarize

SETTINGS = """<settings>
  <mirrors>
    <mirror><id>nexus</id><url>https://Nexus.invalid:8443/repository/public/</url><mirrorOf>*</mirrorOf></mirror>
    <mirror><id>blocker</id><url>http://0.0.0.0/</url><mirrorOf>external:http:*</mirrorOf><blocked>true</blocked></mirror>
  </mirrors>
  <profiles>
    <profile><id>p</id>
      <repositories><repository><id>r</id><url>https://repo.nexus.invalid/maven</url></repository></repositories>
      <pluginRepositories><pluginRepository><id>pr</id><url>https://plugins.nexus.invalid/m2</url></pluginRepository>
      </pluginRepositories>
    </profile>
    <profile><id>inactive</id>
      <repositories><repository><id>x</id><url>https://other.invalid/maven</url></repository></repositories>
    </profile>
  </profiles>
  <activeProfiles><activeProfile>p</activeProfile></activeProfiles>
</settings>
"""

ROOT_POM = """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>example</groupId>
  <artifactId>root</artifactId>
  <version>1.0.0</version>
  <packaging>pom</packaging>
  <modules>
    <module>core</module>
  </modules>
  <profiles>
    <profile><id>web</id><modules><module>web</module></modules></profile>
  </profiles>
</project>
"""

CORE_POM = """<project>
  <parent><groupId>example</groupId><artifactId>root</artifactId><version>1.0.0</version></parent>
  <artifactId>core</artifactId>
  <properties><plugin.dep.version>9.9</plugin.dep.version></properties>
  <build>
    <plugins>
      <plugin>
        <artifactId>maven-compiler-plugin</artifactId>
        <version>3.13.0</version>
        <dependencies>
          <dependency>
            <groupId>example</groupId><artifactId>plugdep</artifactId><version>${plugin.dep.version}</version>
          </dependency>
        </dependencies>
      </plugin>
    </plugins>
    <pluginManagement><plugins>
      <plugin><groupId>org.example</groupId><artifactId>managed-plugin</artifactId><version>1.0</version></plugin>
    </plugins></pluginManagement>
  </build>
</project>
"""

WEB_POM = """<project>
  <parent><groupId>example</groupId><artifactId>root</artifactId><version>1.0.0</version></parent>
  <artifactId>web</artifactId>
  <packaging>war</packaging>
</project>
"""

TREE = """example:root:pom:1.0.0
example:core:jar:1.0.0
+- example:lib:jar:1.2.3:compile
|  \\- example:deep:jar:0.5:compile
\\- example:other:jar:2.0:test
example:web:war:1.0.0
+- example:core:jar:1.0.0:compile
|  \\- example:lib:jar:1.2.3:compile
\\- example:extra:jar:3.1:runtime
"""


class FakeRunner:
    def __init__(self, *, text: str = "[INFO] BUILD SUCCESS\n", exit_code: int = 0, tree: str | None = None,
                 tree_exit: int = 0, timed_out: bool = False):
        self.calls: list[dict] = []
        self.text, self.exit_code, self.tree, self.tree_exit, self.timed_out = text, exit_code, tree, tree_exit, timed_out

    def __call__(self, cmd, *, cwd, env, log_path, timeout_s):
        self.calls.append({"cmd": list(cmd), "cwd": cwd, "timeout_s": timeout_s, "log_path": log_path, "env": env})
        out = next((a.split("=", 1)[1] for a in cmd if a.startswith("-DoutputFile=")), None)
        if out is not None:
            if self.tree is not None and self.tree_exit == 0:
                Path(out).write_text(self.tree, encoding="utf-8")
            return ProcessResult(exit_code=self.tree_exit, text="", duration_s=0.1)
        return ProcessResult(exit_code=self.exit_code, text=self.text, duration_s=1.5, timed_out=self.timed_out)


def make_cfg(tmp_path: Path, **build) -> types.SimpleNamespace:
    settings = tmp_path / "server-settings.xml"
    settings.write_text(SETTINGS, encoding="utf-8")
    jdk = tmp_path / "jdk"
    jdk.mkdir(exist_ok=True)
    (jdk / "release").write_text('JAVA_VERSION="21.0.2"\n', encoding="utf-8")
    opts = {"mvn": "/opt/server-maven/bin/mvn", "settings_file": str(settings), "java_home": str(jdk),
            "local_repo": str(tmp_path / "m2")}
    opts.update(build)
    return types.SimpleNamespace(build=BuildConfig(**opts), remediation=RemediationConfig())


def ok_probe(cfg):
    return True, "Apache Maven 3.9.9; Java version: 21.0.2, runtime: /jdk"


def write_project(root: Path) -> Path:
    (root / "core").mkdir(parents=True)
    (root / "web").mkdir()
    (root / "pom.xml").write_text(ROOT_POM, encoding="utf-8")
    (root / "core" / "pom.xml").write_text(CORE_POM, encoding="utf-8")
    (root / "web" / "pom.xml").write_text(WEB_POM, encoding="utf-8")
    return root


INSECURE_MARKERS = ("ssl.insecure", "ssl.allowall", "ignore.validity", "securityMode")


def assert_safe(cmd: list[str], settings: str):
    assert not any(m in a for a in cmd for m in INSECURE_MARKERS), cmd
    assert "-k" not in cmd and "--insecure" not in cmd
    assert "mvnw" not in " ".join(cmd)
    assert cmd[0] == "/opt/server-maven/bin/mvn"
    i = cmd.index("-s")
    assert cmd[i + 1] == str(Path(settings).absolute())
    assert cmd.count("-s") == 1
    assert TEST_SKIP in cmd


# ---------------------------------------------------------------------------- protocol / package
def test_server_maven_is_a_gateway(tmp_path):
    assert isinstance(ServerMaven(make_cfg(tmp_path), FakeRunner(), maven_probe=ok_probe), MavenGateway)


def test_package_command_and_success(tmp_path):
    cfg = make_cfg(tmp_path)
    runner = FakeRunner()
    project = write_project(tmp_path / "p")
    out = ServerMaven(cfg, runner, maven_probe=ok_probe).package(project, tmp_path / "logs" / "build.log")
    assert out.ok and out.exit_code == 0 and not out.timed_out
    assert out.log == (tmp_path / "logs" / "build.log").as_posix()
    assert out.seconds == 1.5 and out.summary == ""
    (call,) = runner.calls
    cmd = call["cmd"]
    assert_safe(cmd, cfg.build.settings_file)
    assert cmd.index("clean") < cmd.index("package")
    assert cmd.count(TEST_SKIP) == 1
    assert call["cwd"] == project
    assert call["timeout_s"] == cfg.remediation.build_timeout_minutes * 60


def test_package_refuses_insecure_and_settings_overrides_in_extra_args(tmp_path):
    # real-Maven review 2026-11-11: dropping these silently made the remediation build differently from the migration
    # (surfacing as "artifact not found"); now it is a readiness problem naming the arguments, and nothing runs
    cfg = make_cfg(tmp_path, extra_args=["-B", "-Dmaven.wagon.http.ssl.insecure=true", "-Dmaven.wagon.http.ssl.allowall=true",
                                         "-s", "/elsewhere/settings.xml", "--file=other.xml",
                                         "-Daether.connector.https.securityMode=insecure", TEST_SKIP])
    runner = FakeRunner()
    gw = ServerMaven(cfg, runner, maven_probe=ok_probe)
    out = gw.package(write_project(tmp_path / "p"), tmp_path / "b.log")
    assert runner.calls == [] and not out.ok
    for name in ("-Dmaven.wagon.http.ssl.insecure", "-Dmaven.wagon.http.ssl.allowall", "-s", "--file",
                 "-Daether.connector.https.securityMode"):
        assert name in out.summary
    assert "=true" not in out.summary and "/elsewhere" not in out.summary          # names only, never values
    problems = gw.readiness()
    assert any("build.extra_args contains argument(s) the remediation never passes" in p for p in problems)
    model = gw.resolve(tmp_path / "p", tmp_path / "w")
    assert not model.complete and runner.calls == []


def test_strip_unsafe_args():
    kept, dropped = strip_unsafe_args(["-B", "-f", "x/pom.xml", "-Dmaven.wagon.https.ssl=0", "-ntp"])
    assert kept == ["-B", "-ntp"] and dropped == ["-f", "x/pom.xml", "-Dmaven.wagon.https.ssl=0"]


def test_package_needs_success_text_and_exit_zero(tmp_path):
    cfg = make_cfg(tmp_path)
    project = write_project(tmp_path / "p")
    no_text = ServerMaven(cfg, FakeRunner(text="[INFO] done\n"), maven_probe=ok_probe).package(project, tmp_path / "a.log")
    assert not no_text.ok and no_text.exit_code == 0
    failed = ServerMaven(cfg, FakeRunner(text="[ERROR] Failed to execute goal x\n[INFO] BUILD SUCCESS\n", exit_code=1),
                         maven_probe=ok_probe).package(project, tmp_path / "b.log")
    assert not failed.ok and failed.exit_code == 1 and "[ERROR] Failed to execute goal x" in failed.summary


def test_package_timeout_and_secret_scrub(tmp_path):
    cfg = make_cfg(tmp_path)
    text = "[ERROR] Could not transfer from https://deploy:s3cret@nexus.invalid/repo\n"
    out = ServerMaven(cfg, FakeRunner(text=text, exit_code=-1, timed_out=True), maven_probe=ok_probe).package(
        write_project(tmp_path / "p"), tmp_path / "a.log")
    assert out.timed_out and not out.ok
    assert "s3cret" not in out.summary and "https://***@nexus.invalid" in out.summary
    assert out.summary.startswith("timed out after")


def test_package_refuses_without_settings_and_with_wrapper(tmp_path):
    runner = FakeRunner()
    project = write_project(tmp_path / "p")
    out = ServerMaven(make_cfg(tmp_path, settings_file=None), runner, maven_probe=ok_probe).package(project, tmp_path / "a.log")
    assert not out.ok and "settings" in out.summary and runner.calls == []
    out = ServerMaven(make_cfg(tmp_path, mvn=str(project / "mvnw")), runner, maven_probe=ok_probe).package(
        project, tmp_path / "a.log")
    assert not out.ok and "wrapper" in out.summary and runner.calls == []


def test_package_honours_stop_before_start(tmp_path):
    runner = FakeRunner()
    out = ServerMaven(make_cfg(tmp_path), runner, maven_probe=ok_probe).package(
        write_project(tmp_path / "p"), tmp_path / "a.log", stop=lambda: True)
    assert not out.ok and runner.calls == [] and "stop" in out.summary


def test_project_mvn_config_is_journaled(tmp_path):
    project = write_project(tmp_path / "p")
    (project / ".mvn").mkdir()
    (project / ".mvn" / "maven.config").write_text("-s /tmp/evil.xml\n", encoding="utf-8")
    events: list[dict] = []
    cfg = make_cfg(tmp_path)
    cfg.remediation = RemediationConfig(project_maven_config="ignore")
    ServerMaven(cfg, FakeRunner(), maven_probe=ok_probe, journal=events.append).package(project, tmp_path / "a.log")
    assert events and events[0]["event"] == "maven_project_config" and events[0]["files"] == [".mvn/maven.config"]
    assert events[0]["policy"] == "ignore"


# ---------------------------------------------------------------------------- readiness / hosts
def test_readiness_ok(tmp_path):
    assert ServerMaven(make_cfg(tmp_path), FakeRunner(), maven_probe=ok_probe).readiness() == []


def test_readiness_problems(tmp_path):
    cfg = make_cfg(tmp_path, settings_file=str(tmp_path / "missing.xml"), java_home=str(tmp_path / "nojdk"))
    probs = ServerMaven(cfg, FakeRunner(), maven_probe=lambda c: (False, "mvn not found (mvn): x")).readiness()
    joined = "\n".join(probs)
    assert "Maven not available" in joined and "JDK 21 not found" in joined and "settings.xml not found" in joined
    cfg = make_cfg(tmp_path, settings_file=None)
    probs = ServerMaven(cfg, FakeRunner(), maven_probe=ok_probe).readiness()
    assert probs == ["server settings.xml not configured (build.settings_file)"]


def test_readiness_wrong_jdk_from_release_file_and_from_probe(tmp_path):
    cfg = make_cfg(tmp_path)
    (Path(cfg.build.java_home) / "release").write_text('JAVA_VERSION="17.0.9"\n', encoding="utf-8")
    assert ServerMaven(cfg, FakeRunner(), maven_probe=ok_probe).readiness() == ["JDK 21 required, found Java 17"]
    cfg2 = make_cfg(tmp_path, java_home=str(tmp_path / "nojdk"))
    probe = lambda c: (True, "Apache Maven 3.9.9; Java version: 1.8.0_392, runtime: /j")
    assert ServerMaven(cfg2, FakeRunner(), maven_probe=probe).readiness() == ["JDK 21 required, found Java 8"]


def test_readiness_wrapper_configured(tmp_path):
    probs = ServerMaven(make_cfg(tmp_path, mvn="./mvnw"), FakeRunner(), maven_probe=ok_probe).readiness()
    assert any("wrapper" in p for p in probs)


def test_java_home_major(tmp_path):
    (tmp_path / "release").write_text('IMPLEMENTOR="x"\nJAVA_VERSION="21"\n', encoding="utf-8")
    assert java_home_major(str(tmp_path)) == 21
    assert java_home_major(str(tmp_path / "none")) is None


def test_allowed_hosts_from_settings(tmp_path):
    hosts = ServerMaven(make_cfg(tmp_path), FakeRunner(), maven_probe=ok_probe).allowed_hosts()
    assert hosts == ["nexus.invalid", "plugins.nexus.invalid", "repo.nexus.invalid"]


def test_allowed_hosts_from_config(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.remediation = RemediationConfig(allowed_repository_hosts=["Nexus.Invalid", " ", "mirror.invalid"])
    assert ServerMaven(cfg, FakeRunner(), maven_probe=ok_probe).allowed_hosts() == ["mirror.invalid", "nexus.invalid"]


def test_summarize_strips_project_path(tmp_path):
    text = f"# cmd: mvn\n[ERROR] {tmp_path}/src/A.java:[3,1] boom\n[INFO] BUILD FAILURE\n"
    s = summarize(text, tmp_path)
    assert str(tmp_path) not in s and "src/A.java" in s and "# cmd" not in s


# ---------------------------------------------------------------------------- resolve
def test_resolve_parses_tree_per_module_with_paths_and_plugins(tmp_path):
    project = write_project(tmp_path / "p")
    runner = FakeRunner(tree=TREE)
    model = ServerMaven(make_cfg(tmp_path), runner, maven_probe=ok_probe).resolve(project, tmp_path / "work")
    assert model.complete, model.notes
    assert [m.dir for m in model.modules] == [".", "core", "web"]
    core = model.modules[1]
    assert (core.group_id, core.artifact_id, core.version, core.packaging) == ("example", "core", "1.0.0", "jar")
    deps = {d.ga: d for d in core.dependencies}
    assert deps["example:lib"].direct and deps["example:lib"].path == []
    assert not deps["example:deep"].direct and deps["example:deep"].path == ["example:lib:1.2.3"]
    assert deps["example:other"].scope == "test"
    web = {d.ga: d for d in model.modules[2].dependencies}
    assert web["example:lib"].path == ["example:core:1.0.0"] and not web["example:lib"].direct
    assert model.modules[2].packaging == "war"
    plugins = {(p.ga, p.version): p for p in core.plugins}
    assert ("org.apache.maven.plugins:maven-compiler-plugin", "3.13.0") in plugins
    assert ("org.example:managed-plugin", "1.0") in plugins
    pd = plugins[("example:plugdep", "9.9")]
    assert pd.path == ["org.apache.maven.plugins:maven-compiler-plugin:3.13.0"]
    assert any("declared" in n for n in model.notes)
    (call,) = runner.calls
    cmd = call["cmd"]
    assert_safe(cmd, make_cfg(tmp_path).build.settings_file)
    assert any(a.endswith(":tree") for a in cmd) and "-DappendOutput=true" in cmd
    assert "package" not in cmd


def test_resolve_skips_omitted_nodes(tmp_path):
    project = write_project(tmp_path / "p")
    tree = TREE.replace("\\- example:extra:jar:3.1:runtime",
                        "+- example:extra:jar:3.1:runtime\n\\- (example:lib:jar:1.0.0:compile - omitted for conflict with 1.2.3)")
    model = ServerMaven(make_cfg(tmp_path), FakeRunner(tree=tree), maven_probe=ok_probe).resolve(project, tmp_path / "w")
    web = model.modules[2]
    assert [d.version for d in web.dependencies if d.ga == "example:lib"] == ["1.2.3"]


def test_resolve_missing_module_tree_is_incomplete(tmp_path):
    project = write_project(tmp_path / "p")
    tree = TREE.split("example:web:war:1.0.0")[0]
    model = ServerMaven(make_cfg(tmp_path), FakeRunner(tree=tree), maven_probe=ok_probe).resolve(project, tmp_path / "w")
    assert not model.complete
    assert any("module web" in n for n in model.notes)
    assert model.modules[2].dependencies == []


def test_resolve_failure_retries_with_package_then_incomplete(tmp_path):
    project = write_project(tmp_path / "p")
    runner = FakeRunner(tree=None, tree_exit=1)
    model = ServerMaven(make_cfg(tmp_path), runner, maven_probe=ok_probe).resolve(project, tmp_path / "w")
    assert not model.complete and len(runner.calls) == 2
    assert "package" in runner.calls[1]["cmd"] and runner.calls[1]["cmd"].count(TEST_SKIP) == 1
    assert {n for n in model.notes if n.startswith("module ")} == {
        "module . (example:root): no dependency tree", "module core (example:core): no dependency tree",
        "module web (example:web): no dependency tree"}


def test_resolve_unparsable_module_pom_is_incomplete(tmp_path):
    project = write_project(tmp_path / "p")
    (project / "web" / "pom.xml").write_text("<project><broken>", encoding="utf-8")
    model = ServerMaven(make_cfg(tmp_path), FakeRunner(tree=TREE), maven_probe=ok_probe).resolve(project, tmp_path / "w")
    assert not model.complete and any("web/pom.xml" in n for n in model.notes)


# ---------------------------------------------------------------------------- packaged
def _jar_bytes(props: dict[str, str] | None, extra: dict[str, bytes] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
        if props:
            body = "".join(f"{k}={v}\n" for k, v in props.items())
            zf.writestr(f"META-INF/maven/{props['groupId']}/{props['artifactId']}/pom.properties", "#c\n" + body)
        for k, v in (extra or {}).items():
            zf.writestr(k, v)
    return buf.getvalue()


def test_packaged_lists_war_and_boot_jar_libs(tmp_path):
    project = write_project(tmp_path / "p")
    (project / "web" / "target").mkdir()
    with zipfile.ZipFile(project / "web" / "target" / "web.war", "w") as zf:
        zf.writestr("WEB-INF/web.xml", "<web-app/>")
        zf.writestr("WEB-INF/lib/lib-1.2.3.jar",
                    _jar_bytes({"groupId": "example", "artifactId": "lib", "version": "1.2.3"}))
        zf.writestr("WEB-INF/lib/vendor.jar", _jar_bytes(None))
        zf.writestr("WEB-INF/lib/broken.jar", b"not a zip")
        zf.writestr("WEB-INF/classes/A.class", b"\xca\xfe")
    (project / "core" / "target").mkdir()
    with zipfile.ZipFile(project / "core" / "target" / "core.jar", "w") as zf:      # plain jar: no lib folder
        zf.writestr("A.class", b"\xca\xfe")
    (project / "target").mkdir()
    with zipfile.ZipFile(project / "target" / "app.jar", "w") as zf:
        zf.writestr("BOOT-INF/lib/deep-0.5.jar",
                    _jar_bytes({"groupId": "example", "artifactId": "deep", "version": "0.5"}))
    libs = ServerMaven(make_cfg(tmp_path), FakeRunner(), maven_probe=ok_probe).packaged(project)
    got = [(x.artifact, x.entry, x.ga, x.version, x.file_name) for x in libs]
    assert got == [
        ("target/app.jar", "BOOT-INF/lib/deep-0.5.jar", "example:deep", "0.5", "deep-0.5.jar"),
        ("web/target/web.war", "WEB-INF/lib/broken.jar", "", "", "broken.jar"),
        ("web/target/web.war", "WEB-INF/lib/lib-1.2.3.jar", "example:lib", "1.2.3", "lib-1.2.3.jar"),
        ("web/target/web.war", "WEB-INF/lib/vendor.jar", "", "", "vendor.jar"),
    ]


def test_packaged_shaded_jar_picks_matching_pom_properties(tmp_path):
    project = write_project(tmp_path / "p")
    shaded = _jar_bytes({"groupId": "example", "artifactId": "lib", "version": "1.2.3"},
                        {"META-INF/maven/other/inner/pom.properties": b"groupId=other\nartifactId=inner\nversion=4\n"})
    (project / "web" / "target").mkdir()
    with zipfile.ZipFile(project / "web" / "target" / "web.war", "w") as zf:
        zf.writestr("WEB-INF/lib/lib-1.2.3.jar", shaded)
        zf.writestr("WEB-INF/lib/renamed.jar", shaded)
    libs = ServerMaven(make_cfg(tmp_path), FakeRunner(), maven_probe=ok_probe).packaged(project)
    assert [(x.file_name, x.ga) for x in libs] == [("lib-1.2.3.jar", "example:lib"), ("renamed.jar", "")]


@pytest.mark.parametrize("mvn", ["mvnw", "./mvnw.cmd"])
def test_never_runs_wrapper(tmp_path, mvn):
    runner = FakeRunner(tree=TREE)
    project = write_project(tmp_path / "p")
    model = ServerMaven(make_cfg(tmp_path, mvn=mvn), runner, maven_probe=ok_probe).resolve(project, tmp_path / "w")
    assert runner.calls == [] and not model.complete


def test_build_env_withholds_this_servers_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("IVAS_PASSWORD", "pw-must-not-reach-maven")
    monkeypatch.setenv("IVAS_USER_ID", "svc")
    monkeypatch.setenv("LLM_API_KEY", "key-must-not-reach-maven")
    monkeypatch.setenv("NEXUS_PASSWORD", "used-by-server-settings")       # ${env.…} in the server settings.xml
    cfg = make_cfg(tmp_path)
    runner = FakeRunner()
    ServerMaven(cfg, runner, maven_probe=ok_probe).package(write_project(tmp_path / "p"), tmp_path / "b.log")
    env = runner.calls[0]["env"]
    assert "IVAS_PASSWORD" not in env and "IVAS_USER_ID" not in env and "LLM_API_KEY" not in env
    assert env["NEXUS_PASSWORD"] == "used-by-server-settings" and "MAVEN_OPTS" in env
