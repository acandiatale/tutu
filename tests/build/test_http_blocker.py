"""v2.1z-ah: Maven's HTTP repository blocker is an environment problem of its own (docs/INTERFACES.md "v2.1z-ah").

Company run 2026-11-11: Maven >= 3.8.1 ships the mirror `maven-default-http-blocker` (mirrorOf `external:http:*`,
blocked) in `<maven home>/conf/settings.xml`; the company repository had an `http://` URL, so Maven refused even the
parent pom — and sbmig said "configured parent is not resolvable". The texts below are what Maven 3.9.10 printed on a
real run (settings with an http repository on `nexus.invalid`, never contacted: Maven blocks before connecting)."""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

import sbmig.build.env_errors as env
import sbmig.build.mvn as mvn_mod
from sbmig.build import MavenRunner
from sbmig.build.env_errors import (
    HTTP_BLOCKED_PREFIX,
    BlockedRepository,
    active_http_blockers,
    http_blocked_followup,
    http_blocked_reason,
    http_blocker_hit,
    strip_userinfo,
)
from sbmig.build.log_parser import parse_maven_log
from sbmig.core.workspace import Workspace
from sbmig.report.i18n import tr_line

# ---- captured from Maven 3.9.10 (scratch project, `mvn -s settings.xml clean package`), only the URLs' hosts are invalid
REPOS_LIST = ("[company-nexus (http://deployer:s3cret@nexus.invalid/repository/maven-public/, default, releases+snapshots), "
              "company-snap (http://nexus.invalid/repository/snapshots/, default, snapshots)]")
PARENT_LOG = (
    "[INFO] Scanning for projects...\n"
    "[WARNING] Could not transfer metadata com.example:base-parent:1.0.0-SNAPSHOT/maven-metadata.xml from/to "
    f"maven-default-http-blocker (http://0.0.0.0/): Blocked mirror for repositories: {REPOS_LIST}\n"
    "[ERROR] [ERROR] Some problems were encountered while processing the POMs:\n"
    "[FATAL] Non-resolvable parent POM for com.example:demo:1.0.0-SNAPSHOT: The following artifacts could not be resolved: "
    "com.example:base-parent:pom:1.0.0-SNAPSHOT (absent): Could not transfer artifact com.example:base-parent:pom:1.0.0-SNAPSHOT "
    f"from/to maven-default-http-blocker (http://0.0.0.0/): Blocked mirror for repositories: {REPOS_LIST} and "
    "'parent.relativePath' points at wrong local POM @ line 2, column 9\n"
    " @ \n"
    "[ERROR] The build could not read 1 project -> [Help 1]\n")
DEP_LOG = (
    "[ERROR] Failed to execute goal on project demo2: Could not collect dependencies for project com.example:demo2:jar:1\n"
    "[ERROR] Failed to read artifact descriptor for com.example:in-house-lib:jar:4.0.0\n"
    "[ERROR] \tCaused by: The following artifacts could not be resolved: com.example:in-house-lib:pom:4.0.0 (absent): Could "
    "not transfer artifact com.example:in-house-lib:pom:4.0.0 from/to maven-default-http-blocker (http://0.0.0.0/): Blocked "
    "mirror for repositories: [pom-nexus (http://nexus.invalid/repository/releases/, default, releases+snapshots)]\n"
    "[ERROR] -> [Help 1]\n")
RELEASES_ONLY = ("Could not transfer artifact a:b:pom:1 from/to maven-default-http-blocker (http://0.0.0.0/): Blocked mirror "
                 "for repositories: [rel (http://nexus.invalid/rel/, default, releases)]")


def ids(hit):
    return [(r.id, r.url) for r in hit.repositories]


# ------------------------------------------------------------------------------------------------ classifier
def test_parent_wrapper_with_several_repositories_and_no_credentials():
    hit = http_blocker_hit(PARENT_LOG)
    assert hit is not None and hit.mirror_id == "maven-default-http-blocker"
    assert ids(hit) == [("company-nexus", "http://nexus.invalid/repository/maven-public/"),
                        ("company-snap", "http://nexus.invalid/repository/snapshots/")]
    text = http_blocked_followup(hit) + http_blocked_reason(hit) + json.dumps(hit.as_json())
    assert "s3cret" not in text and "deployer" not in text


def test_dependency_shape_releases_only_and_crlf():
    assert ids(http_blocker_hit(DEP_LOG)) == [("pom-nexus", "http://nexus.invalid/repository/releases/")]
    assert ids(http_blocker_hit(RELEASES_ONLY)) == [("rel", "http://nexus.invalid/rel/")]
    crlf = PARENT_LOG.replace("\n", "\r\n")
    assert ids(http_blocker_hit(crlf)) == ids(http_blocker_hit(PARENT_LOG))


def test_custom_blocked_mirror_and_bare_blocker_id():
    custom = RELEASES_ONLY.replace("maven-default-http-blocker", "corp-block")
    assert http_blocker_hit(custom).mirror_id == "corp-block"
    bare = "[ERROR] Could not transfer artifact a:b:pom:1 from/to maven-default-http-blocker (http://0.0.0.0/): timeout"
    hit = http_blocker_hit(bare)
    assert hit is not None and hit.repositories == [] and hit.repositories_text() == "(not named in the log)"
    assert http_blocker_hit("Could not find artifact a:b:pom:1 in nexus (https://nexus.invalid/)") is None
    assert http_blocker_hit("") is None


# a settings file that REDEFINES the blocker id as an ordinary mirror (real URL, <blocked>false</blocked>): Maven names
# that id in every plain resolution failure — not a block
COMPANY_SHAPE_MISSING = (
    "[ERROR] Failed to execute goal on project app: Could not resolve dependencies for project com.example:app:jar:1.0: "
    "Could not find artifact org.example:some-lib:jar:9.9 in maven-default-http-blocker "
    "(http://nexus.invalid:8081/repository/x-release/) -> [Help 1]\n")
COMPANY_SHAPE_REFUSED = (
    "[ERROR] Could not transfer artifact org.example:some-lib:pom:9.9 from/to maven-default-http-blocker "
    "(http://nexus.invalid:8081/repository/x-release/): Connection refused\n")
CACHED_BLOCK = (
    "[ERROR] Non-resolvable parent POM: com.example:base-parent:pom:1.0 was not found in http://0.0.0.0/ during a previous "
    "attempt. This failure was cached in the local repository and resolution is not reattempted until the update interval "
    "of maven-default-http-blocker has elapsed or updates are forced\n")


def test_redefined_blocker_id_with_a_real_url_is_not_a_block():
    assert http_blocker_hit(COMPANY_SHAPE_MISSING) is None
    assert http_blocker_hit(COMPANY_SHAPE_REFUSED) is None
    # 0.0.0.0 inside a version / another address is not the blocker's URL
    assert http_blocker_hit(COMPANY_SHAPE_MISSING.replace("9.9", "10.0.0.0.0")) is None
    assert http_blocker_hit(COMPANY_SHAPE_MISSING.replace("nexus.invalid", "10.0.0.0")) is None
    # the whole build log is searched: an address a plugin / an application printed is not Maven's blocker message
    for line in ("[INFO] Tomcat started on port 8080 (http) with context path '/' at http://0.0.0.0:8080/\n",
                 "[INFO] dev server listening on http://0.0.0.0/ (press ctrl-c)\n",
                 "[INFO] proxying (http://0.0.0.0/) to the backend\n"):
        assert http_blocker_hit(line + COMPANY_SHAPE_MISSING) is None


def test_transfer_forms_naming_the_blocker_address_are_blocks():
    # without the phrase: the transfer line and both cached-failure wordings of the resolver
    transfer = "[ERROR] Could not transfer artifact a:b:pom:1 from/to corp-block (http://0.0.0.0/): timeout\n"
    assert http_blocker_hit(transfer).mirror_id == "corp-block"
    found_in = "[ERROR] Could not find artifact a:b:pom:1 in maven-default-http-blocker (http://0.0.0.0/)\n"
    assert http_blocker_hit(found_in).mirror_id == "maven-default-http-blocker"
    cached_transfer = ("[ERROR] a:b:pom:1 failed to transfer from http://0.0.0.0/ during a previous attempt. This failure was "
                       "cached in the local repository and resolution is not reattempted until the update interval of "
                       "maven-default-http-blocker has elapsed or updates are forced\n")
    assert http_blocker_hit(cached_transfer).mirror_id == "maven-default-http-blocker"


def test_cached_failure_of_the_builtin_blocker_is_a_block():
    hit = http_blocker_hit(CACHED_BLOCK)
    assert hit is not None and hit.mirror_id == "maven-default-http-blocker" and hit.repositories == []
    other = CACHED_BLOCK.replace("of maven-default-http-blocker", "of corp-block")
    assert http_blocker_hit(other).mirror_id == "corp-block"
    # a plain failure beside it does not lend its mirror id
    assert http_blocker_hit(COMPANY_SHAPE_MISSING.replace("maven-default-http-blocker", "x-public") + CACHED_BLOCK
                            ).mirror_id == "maven-default-http-blocker"


def test_strip_userinfo():
    assert strip_userinfo("http://u:p@h.invalid:8081/r/") == "http://h.invalid:8081/r/"
    assert strip_userinfo("https://h.invalid/r/") == "https://h.invalid/r/"


def test_log_parser_keeps_the_blocker_text_in_the_error():
    """The loop and the orchestrator read the blocker from the parsed errors (message / raw)."""
    for log, kind in ((PARENT_LOG, "parent_resolution"), (DEP_LOG, "dependency_resolution")):
        [e] = parse_maven_log(log)
        assert e.kind.value == kind and http_blocker_hit(f"{e.message}\n{e.raw}") is not None


def test_followup_wording_and_korean(tmp_path, monkeypatch):
    settings = tmp_path / "settings.xml"
    settings.write_text("<settings/>", encoding="utf-8")
    glob = tmp_path / "maven" / "conf" / "settings.xml"
    glob.parent.mkdir(parents=True)
    glob.write_text("<settings/>", encoding="utf-8")
    monkeypatch.setattr(env, "_global_settings", lambda cfg: glob)
    cfg = type("C", (), {"build": type("B", (), {"settings_file": str(settings), "mvn": "mvn"})()})()
    hit = http_blocker_hit(DEP_LOG)
    line = http_blocked_followup(hit, cfg)
    assert line.startswith(HTTP_BLOCKED_PREFIX)
    assert line == ("Maven (3.8.1+) refuses the HTTP repository `pom-nexus` (http://nexus.invalid/repository/releases/) via "
                    "the blocked mirror `maven-default-http-blocker` — not a project or sbmig problem. Fix the Maven settings: "
                    f"(a) an https URL, or (b) in {settings} (build.settings_file) exempt `pom-nexus` from mirror "
                    "`maven-default-http-blocker` (append `,!<repository id>` to its `<mirrorOf>`), or add a mirror for "
                    "`pom-nexus` (same URL, "
                    f"`<blocked>false</blocked>`); blocker from {glob}")
    ko = tr_line("ko", line)
    assert ko.startswith("Maven(3.8.1 이상)이 HTTP 저장소 `pom-nexus` (http://nexus.invalid/repository/releases/) 를 차단 미러")
    assert f"(b) {settings} (build.settings_file)에서" in ko and ko.endswith(f"차단 미러 출처: {glob}")
    assert "`,!<저장소 ID>` 를 붙여 `pom-nexus` 를 예외로" in ko          # the company's own way: an exception in the blocker's mirrorOf
    # two repositories: plural + every id
    two = http_blocked_followup(http_blocker_hit(PARENT_LOG), cfg)
    assert "HTTP repositories `company-nexus`" in two and "mirror for `company-nexus`, `company-snap`" in two
    assert tr_line("ko", two) != two


# ------------------------------------------------------------------------------------------------ preflight
GLOBAL_BLOCKER = """<settings><mirrors><mirror><id>maven-default-http-blocker</id><mirrorOf>external:http:*</mirrorOf>
<name>Pseudo repository to mirror external repositories initially using HTTP.</name><url>http://0.0.0.0/</url>
<blocked>true</blocked></mirror></mirrors></settings>"""


def user_settings(repo_url: str, mirrors: str = "") -> str:
    return (f"<settings><mirrors>{mirrors}</mirrors><profiles><profile><id>co</id><repositories><repository>"
            f"<id>company-nexus</id><url>{repo_url}</url></repository></repositories></profile></profiles>"
            "<activeProfiles><activeProfile>co</activeProfile></activeProfiles></settings>")


@pytest.fixture()
def pre(tmp_path, monkeypatch):
    glob = tmp_path / "maven" / "conf" / "settings.xml"
    glob.parent.mkdir(parents=True)
    glob.write_text(GLOBAL_BLOCKER, encoding="utf-8")
    monkeypatch.setattr(env, "_global_settings", lambda cfg: glob)
    user = tmp_path / "user-settings.xml"
    cfg = type("C", (), {"build": type("B", (), {"settings_file": str(user), "mvn": "mvn"})()})()

    def check(text: str, root: Path | None = None):
        user.write_text(text, encoding="utf-8")
        return active_http_blockers(cfg, root)
    return check


def test_preflight_blocked_http_repository_warns(pre):
    [b] = pre(user_settings("http://u:p@nexus.invalid/repo/"))
    assert (b.repo_id, b.url, b.mirror_id) == ("company-nexus", "http://nexus.invalid/repo/", "maven-default-http-blocker")
    assert b.mirror_origin.endswith("conf/settings.xml")


def test_preflight_https_repository_is_silent(pre):
    assert pre(user_settings("https://nexus.invalid/repo/")) == []


def test_preflight_user_override_of_the_blocker_id_is_silent(pre):
    override = ("<mirror><id>maven-default-http-blocker</id><mirrorOf>none</mirrorOf><url>http://0.0.0.0/</url>"
                "<blocked>false</blocked></mirror>")
    assert pre(user_settings("http://nexus.invalid/repo/", override)) == []


def test_preflight_user_mirror_for_the_repository_unblocked_is_silent(pre):
    m = ("<mirror><id>nexus-direct</id><mirrorOf>company-nexus</mirrorOf><url>http://nexus.invalid/repo/</url>"
         "<blocked>false</blocked></mirror>")
    assert pre(user_settings("http://nexus.invalid/repo/", m)) == []


def test_preflight_catch_all_user_mirror_is_silent_and_pom_repositories_count(pre, tmp_path):
    star = "<mirror><id>nexus</id><mirrorOf>*</mirrorOf><url>http://nexus.invalid/group/</url></mirror>"
    assert pre(user_settings("http://nexus.invalid/repo/", star)) == []
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pom.xml").write_text("<project><repositories><repository><id>old</id><url>http://old.invalid/r/</url>"
                                  "</repository></repositories></project>", encoding="utf-8")
    [b] = pre(user_settings("https://nexus.invalid/repo/"), proj)
    assert b.repo_id == "old" and b.declared.startswith("pom pom.xml")


# ------------------------------------------------------------------------------------------------ runners: no retry
class SeqPopen:
    outputs: list[tuple[str, int]] = []
    calls: list[list[str]] = []

    def __init__(self, cmd, **kw):
        SeqPopen.calls.append(list(cmd))
        text, self._code = SeqPopen.outputs.pop(0) if SeqPopen.outputs else ("[INFO] BUILD SUCCESS\n", 0)
        self.stdout = io.BytesIO(text.encode("utf-8"))
        self.returncode = None
        self.pid = -424243

    def wait(self, timeout=None):
        self.returncode = self._code
        return self._code

    def poll(self):
        return self.returncode


def test_build_runner_never_retries_a_blocked_build(tmp_path, cfg, monkeypatch):
    """`Failed to read artifact descriptor for …` (DEP_LOG) is a -U candidate on its own; with the blocker it is not."""
    SeqPopen.outputs, SeqPopen.calls = [(DEP_LOG, 1), (PARENT_LOG, 1)], []
    monkeypatch.setattr(mvn_mod, "_popen", SeqPopen)
    root = tmp_path / "proj"
    root.mkdir()
    (root / "pom.xml").write_text("<project/>")
    ws = Workspace(root, tmp_path / "run", "t-run")
    assert not MavenRunner(ws, cfg).run("compile").ok and len(SeqPopen.calls) == 1
    assert not MavenRunner(ws, cfg).run("compile").ok and len(SeqPopen.calls) == 2
    assert all("-U" not in c for c in SeqPopen.calls)


def test_rewrite_runner_never_retries_a_blocked_run(tmp_path, cfg, monkeypatch):
    import sbmig.rewrite.runner as runner_mod
    from sbmig.build.mvn import ProcessResult
    from sbmig.core.models import RecipeRef
    from sbmig.rewrite import OpenRewriteRunner

    calls = []

    def fake_run(cmd, *, cwd, env, log_path, timeout_s):
        text = DEP_LOG.replace("Failed to execute goal on project", "Failed to execute goal "
                               "org.openrewrite.maven:rewrite-maven-plugin:6.46.1:run (default-cli) compile on project")
        log_path.write_text(text, encoding="utf-8")
        calls.append(list(cmd))
        return ProcessResult(exit_code=1, text=text, duration_s=0.1)
    monkeypatch.setattr(runner_mod, "run_logged", fake_run)
    monkeypatch.setattr(runner_mod, "resolve_recipe_artifact_versions",
                        lambda cfg, env, artifacts, cache_path, work_dir: {a: "1.0" for a in artifacts})
    root = tmp_path / "proj"
    root.mkdir()
    (root / "pom.xml").write_text("<project/>\n")
    ws = Workspace(root, tmp_path / "run", "t-run")
    ws.git_init()
    r = OpenRewriteRunner(ws, cfg)
    waits = []
    r._sleep = waits.append
    res = r.run([RecipeRef(id="org.openrewrite.java.migrate.UpgradeToJava21")], label="spring")
    assert not res.ok and len(calls) == 1 and waits == []          # no download wait, no -U, no runNoFork


# ------------------------------------------------------------------------------------------------ remediation
def test_remediation_summary_and_deptree_note_carry_the_same_wording(tmp_path):
    from tests.remediations.test_maven_parity import ROOT_AGG, Runner, gw, msa_tree

    r = Runner(script=[(1, DEP_LOG)])
    g = gw(tmp_path, r)
    out = g.package(msa_tree(tmp_path / "p", ROOT_AGG), tmp_path / "logs" / "build.log")
    assert not out.ok and len(r.calls) == 1                         # no -U retry
    first = out.summary.splitlines()[0]
    assert first == http_blocked_followup(http_blocker_hit(DEP_LOG), g.cfg)

    class TreeFail(Runner):
        def __call__(self, cmd, *, cwd, env, log_path, timeout_s):
            self.calls.append({"cmd": list(cmd)})
            from sbmig.build.mvn import ProcessResult
            return ProcessResult(exit_code=1, text=DEP_LOG, duration_s=0.1)

    t = TreeFail()
    model = gw(tmp_path, t).resolve(msa_tree(tmp_path / "q", ROOT_AGG), tmp_path / "work")
    assert not model.complete and len(t.calls) == 1                 # the package-assisted second attempt is skipped
    assert any(n.startswith(HTTP_BLOCKED_PREFIX) for n in model.notes)


def test_blocked_repository_is_frozen():
    assert BlockedRepository("a", "http://x/") == BlockedRepository("a", "http://x/")
