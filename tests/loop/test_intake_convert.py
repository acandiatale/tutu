"""The conversion step at intake (out/scratch/fwconvert/ROUND2.md section E and its amendment): a run in flow `full`
with the framework mode `convert` replaces the working tree by the generated Spring Boot project right after the as-is
snapshot and before the build-tool check, commits it as the second commit, and the run goes on as an ordinary project.

`sbmig.fwconvert.pipeline.convert_project` is faked: a stub module is put into `sys.modules` (the orchestrator imports
it with `importlib.import_module` on every call). The fake writes a small generated tree into `out_root`. No Maven, no
network: the repository lookup for empty `convert.versions` is a fake too."""
from __future__ import annotations

import json
import os
import stat
import sys
import types
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sbmig.cli import run_exit_code
from sbmig.core.models import FRAMEWORK_MODES, CaseType, Phase, RunReport, utcnow
from sbmig.core.workspace import Workspace
from sbmig.fwconvert.model import ConvertResult, DatasourceInfo, ModuleNames, ProjectIdentity, QueueItem
from sbmig.loop.convert import (
    CONVERT_COMMIT,
    DUPLICATE_LINE_HEAD,
    FLAG_CONVERTED,
    FLAG_FAILED,
    FLAG_UNAVAILABLE,
    SKIPPED_LINE,
    parse_framework_mode,
    project_domain,
    root_pom_names,
)
from sbmig.report.i18n import tr_line

from .fakes import FakeBuildRunner, FakeRewriteRunner, journal_events, make_profile, ok_build
from .test_orchestrator import make_orch

MYBATIS = "org.mybatis.spring.boot:mybatis-spring-boot-starter"
ROOT_POM = ("<project><modelVersion>4.0.0</modelVersion><groupId>com.acme.shop</groupId>"
            "<artifactId>my-shop</artifactId><version>1.0</version><packaging>war</packaging></project>\n")
CHILD_POM = ("<project><modelVersion>4.0.0</modelVersion><parent><groupId>com.acme.parent</groupId>"
             "<artifactId>base</artifactId><version>3</version></parent><artifactId>order-web</artifactId></project>\n")
_PARENT = "<parent><groupId>com.acme.shop</groupId><artifactId>myShop</artifactId><version>1.0.0</version></parent>"
GENERATED = {
    "pom.xml": ("<project><modelVersion>4.0.0</modelVersion><groupId>com.acme.shop</groupId><artifactId>myShop</artifactId>"
                "<version>1.0.0</version><packaging>pom</packaging><modules><module>myShop-boot</module>"
                "<module>myShop-domain</module></modules></project>\n"),
    "myShop-boot/pom.xml": f"<project><modelVersion>4.0.0</modelVersion>{_PARENT}<artifactId>myShop-boot</artifactId></project>\n",
    "myShop-boot/src/main/java/com/acme/shop/myShop/App.java": "package com.acme.shop.myShop;\nclass App {}\n",
    "myShop-domain/pom.xml": (f"<project><modelVersion>4.0.0</modelVersion>{_PARENT}<artifactId>myShop-domain</artifactId>"
                              "</project>\n"),
    "myShop-boot/bin/start.sh": "#!/bin/sh\necho start\n",
}
EXECUTABLE = "myShop-boot/bin/start.sh"
DUP = "GET /api/order: claimed by OrderController.list and ShopController.list"


# ---------------------------------------------------------------- fixtures and fakes


class FakePipeline:
    """Stands in for `sbmig.fwconvert.pipeline`: `convert_project` writes `files` into `out_root` and returns
    `result`, or raises `result` when it is an exception."""

    def __init__(self, result=None, files=None, convertible=True):
        self.result = result
        self.files = GENERATED if files is None else files
        self.calls: list[tuple] = []
        self.convertible = convertible
        self.checked: list[Path] = []

    def has_convertible(self, src_root, rules):
        self.checked.append(Path(src_root))
        return self.convertible

    def convertible_summary(self, src_root, rules):
        self.checked.append(Path(src_root))
        if isinstance(self.convertible, tuple):
            return self.convertible
        return (bool(self.convertible), 1 if self.convertible else 0)

    def convert_project(self, src_root, out_root, rules, options):
        out_root = Path(out_root)
        assert out_root.is_dir() and not any(out_root.iterdir()), "out_root must be new and empty"
        self.calls.append((Path(src_root), out_root, rules, options))
        if isinstance(self.result, BaseException):
            raise self.result
        res = self.result if self.result is not None else converted_result()
        if res.converted:
            for rel, text in self.files.items():
                p = out_root / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text, encoding="utf-8")
            if EXECUTABLE in self.files:
                os.chmod(out_root / EXECUTABLE, 0o755)
        return res


def converted_result(queue: int = 3, errors: list[str] | None = None) -> ConvertResult:
    ident = ProjectIdentity(project_name="myShop", domain="", package_base="com.acme.shop.myShop",
                            modules=ModuleNames(boot="myShop-boot", domain="myShop-domain", service="myShop-service",
                                                store="myShop-store"), source="override")
    items = [QueueItem(component=f"svc{i}", reason="UNSUPPORTED_ACTIVITY", source_path=f"svc/{i}.xml",
                       last_error="unknown tag", excerpt=f"<service id='{i}'/>") for i in range(queue)]
    return ConvertResult(converted=True, identity=ident,
                         counts={"services": 2, "mappers": 1, "statements": 4, "resources": 2, "java_files": 9,
                                 "jsp": 1, "static": 3, "js_rewritten": 1, "pages": 1, "queue": queue},
                         datasource=DatasourceInfo(configured=True, vendor="oracle"), queue=items,
                         unexposed=["/api/x"], skipped=["a.bin: binary"], errors=list(errors or []))


@pytest.fixture()
def pipeline(monkeypatch):
    def install(result=None, files=None, convertible=True) -> FakePipeline:
        fake = FakePipeline(result, files, convertible)
        mod = types.ModuleType("sbmig.fwconvert.pipeline")
        mod.convert_project = fake.convert_project  # type: ignore[attr-defined]
        mod.has_convertible = fake.has_convertible  # type: ignore[attr-defined]
        mod.convertible_summary = fake.convertible_summary  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "sbmig.fwconvert.pipeline", mod)
        return fake
    return install


@pytest.fixture()
def cfg(cfg):
    """The shared config with a configured framework, mode `convert` and pinned versions (no lookup needed)."""
    c = cfg.model_copy(deep=True)
    c.framework.artifacts = ["example-fw-core"]
    c.framework.mode = "convert"
    c.convert.versions = {MYBATIS: "3.0.5"}
    return c


def _project(tmp_path: Path, name: str = "shop-app", pom: str | None = ROOT_POM) -> Path:
    src = tmp_path / "src" / name
    files = {"svc/order.xml": "<service id='order'/>\n", "sql/order.example_sql": "select 1\n",
             "WebContent/index.jsp": "<html/>\n", ".gitignore": "*.log\n"}
    if pom is not None:
        files["pom.xml"] = pom
    for rel, text in files.items():
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return src


def _ws(tmp_path: Path, **kw) -> Workspace:
    # git is initialised by the run itself, so the as-is snapshot is exactly the first commit
    return Workspace.from_input(_project(tmp_path, **kw), tmp_path / "out", run_id="t-conv")


def _orch(cfg, ws, **kw):
    kw.setdefault("build", FakeBuildRunner(ws, fn=lambda w, c: ok_build(c.get("goal") or "compile")))
    return make_orch(cfg, ws, **kw)


def _generated_profile(ws, cfg):
    """The profile of the generated tree, its poms read by the real parser (the analysis after the conversion)."""
    from sbmig.analyze.pom_parser import parse_poms
    poms, _ = parse_poms(ws)
    p = make_profile()
    p.poms = poms
    p.root_pom = next(x for x in poms if x.path == "pom.xml")
    return p


def _subjects(ws: Workspace) -> list[str]:
    return ws._git("log", "--reverse", "--format=%s").stdout.splitlines()


def _tree(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        rel = Path(dirpath).relative_to(root)
        if rel.parts[:1] == (".git",):
            continue
        dirnames[:] = [d for d in dirnames if not (rel == Path(".") and d == ".git")]
        for f in filenames:
            p = Path(dirpath) / f
            out[(rel / f).as_posix()] = "<link>" if p.is_symlink() else p.read_text(encoding="utf-8")
    return out


def _asis_kept(ws, before: dict[str, str]) -> bool:
    """The as-is tree is back: HEAD is the as-is snapshot, nothing uncommitted, every uploaded file as it was
    (`.gitignore` gets sbmig's ignore lines at the snapshot)."""
    after = _tree(ws.root)
    same = {k: v for k, v in after.items() if k != ".gitignore"} == {k: v for k, v in before.items() if k != ".gitignore"}
    return same and ws.head() == ws.first_commit() and not ws.is_dirty()


def _events(ws, name):
    return [e for e in journal_events(ws) if e.get("event") == name]


# ---------------------------------------------------------------- the run value


def test_parse_framework_mode():
    assert FRAMEWORK_MODES == ("reduced", "convert")
    assert parse_framework_mode(None, "convert") == "convert"
    assert parse_framework_mode("", "reduced") == "reduced"
    assert parse_framework_mode(" Convert ", "reduced") == "convert"
    assert parse_framework_mode(None, None) == "reduced"
    with pytest.raises(ValueError, match="framework_mode must be one of: reduced, convert"):
        parse_framework_mode("full")


def test_mode_defaults_to_the_config_and_an_explicit_value_wins(cfg, tmp_path, pipeline):
    fake = pipeline()
    ws = _ws(tmp_path)
    report = _orch(cfg, ws).run(stop_after=Phase.INTAKE)               # cfg.framework.mode = convert
    assert report.framework_mode == "convert" and len(fake.calls) == 1
    assert _events(ws, "framework_mode")[0]["framework_mode"] == "convert"

    fake2 = pipeline()
    ws2 = Workspace.from_input(_project(tmp_path / "b"), tmp_path / "out", run_id="t-conv-2")
    report2 = _orch(cfg, ws2).run(stop_after=Phase.INTAKE, framework_mode="reduced")
    assert report2.framework_mode == "reduced" and fake2.calls == [] and report2.conversion is None
    assert _subjects(ws2) == ["sbmig: as-is snapshot"]

    with pytest.raises(ValueError):
        _orch(cfg, ws2).run(framework_mode="bogus")


def test_reduced_default_journals_and_converts_nothing(cfg, tmp_path, pipeline):
    c = cfg.model_copy(deep=True)
    c.framework.mode = "reduced"
    fake = pipeline()
    ws = _ws(tmp_path)
    report = _orch(c, ws).run(stop_after=Phase.INTAKE)
    assert report.framework_mode == "reduced" and fake.calls == [] and FLAG_CONVERTED not in report.flags


def test_remediation_flow_ignores_the_mode(cfg, tmp_path, pipeline):
    fake = pipeline()
    ws = _ws(tmp_path)
    orch = _orch(cfg, ws)
    with orch._run_guard(False, flow="remediation", framework_mode="convert") as report:
        assert orch.framework_mode == ""
        assert orch._intake_convert(report) is True
    assert report.framework_mode == "" and fake.calls == []
    assert not _events(ws, "framework_mode")


def test_stream_graph_takes_the_keyword(cfg, tmp_path, pipeline):
    from sbmig.graph.engine import stream_graph
    c = cfg.model_copy(deep=True)
    c.framework.mode = "reduced"
    c.loop.engine = "graph"
    fake = pipeline()
    ws = _ws(tmp_path)
    orch = _orch(c, ws)
    list(stream_graph(orch, stop_after=Phase.INTAKE, framework_mode="convert"))
    assert orch.report.framework_mode == "convert" and len(fake.calls) == 1
    with pytest.raises(ValueError):
        list(stream_graph(_orch(c, ws), framework_mode="nope"))


def test_cli_option_reaches_the_orchestrator(tmp_path, monkeypatch, repo_root):
    import sbmig.loop
    from sbmig.cli import app

    seen: dict = {}

    class FakeOrch:
        def __init__(self, cfg, ws, **kw):
            pass

        def run(self, stop_after=None, dry_run=False, **kw):
            seen.update(kw)
            return RunReport(run_id="r", project_name="p", case_type=CaseType.UNKNOWN, started_at=utcnow(),
                             success=True)

    monkeypatch.setattr(sbmig.loop, "Orchestrator", FakeOrch)
    src = tmp_path / "proj"
    src.mkdir()
    (src / "pom.xml").write_text("<project/>")
    cfg_file = str(repo_root / "config" / "artifacts.local-e2e.yml")
    runner = CliRunner()
    res = runner.invoke(app, ["run", str(src), "--config", cfg_file, "--out", str(tmp_path / "out"), "--run-id", "c1",
                              "--framework-mode", "convert"])
    assert res.exit_code == 0, res.output
    assert seen == {"framework_mode": "convert"}
    seen.clear()
    res = runner.invoke(app, ["run", str(src), "--config", cfg_file, "--out", str(tmp_path / "out"), "--run-id", "c2"])
    assert res.exit_code == 0 and seen == {}                    # not given: not passed, framework.mode decides
    res = runner.invoke(app, ["run", str(src), "--config", cfg_file, "--out", str(tmp_path / "out"), "--run-id", "c3",
                              "--framework-mode", "full"])
    assert res.exit_code != 0 and "--framework-mode" in res.output


# ---------------------------------------------------------------- not available


def _assert_refused(report, ws, fake, korean: str):
    assert FLAG_UNAVAILABLE in report.flags and "aborted" in report.flags
    assert run_exit_code(report) == 3
    assert fake.calls == []
    reason = _events(ws, "abort")[0]["reason"]
    assert tr_line("ko", reason) == korean
    assert report.manual_followups[0] == f"run aborted: {reason}"
    assert _subjects(ws) == ["sbmig: as-is snapshot"]
    assert not _events(ws, "build_tool")                    # refused before the build-tool check


def test_refused_when_convert_is_switched_off(cfg, tmp_path, pipeline):
    cfg.convert.enabled = False
    fake = pipeline()
    ws = _ws(tmp_path)
    report = _orch(cfg, ws).run(framework_mode="convert")
    _assert_refused(report, ws, fake, "사내 규정 전환이 꺼져 있습니다(convert.enabled). 다른 처리 방법으로 다시 신청하거나 "
                                      "설정을 켜 주세요.")


def test_refused_without_a_framework_name(cfg, tmp_path, pipeline):
    cfg.framework.artifacts = []
    cfg.framework.name = ""
    fake = pipeline()
    ws = _ws(tmp_path)
    report = _orch(cfg, ws).run(framework_mode="convert")
    _assert_refused(report, ws, fake, "사내 규정 전환에 쓸 프레임워크 이름이 없습니다. framework.name이나 "
                                      "framework.artifacts를 설정해 주세요.")


def test_refused_without_root_pom_and_package_root(cfg, tmp_path, pipeline):
    cfg.convert.package_root = ""
    fake = pipeline()
    ws = _ws(tmp_path, pom=None)
    report = _orch(cfg, ws).run()
    _assert_refused(report, ws, fake, "패키지 이름을 가져올 루트 pom.xml이 프로젝트에 없고 convert.package_root도 설정되어 "
                                      "있지 않습니다. convert.package_root를 설정해 주세요.")


def test_empty_version_resolved_through_the_lookup(cfg, tmp_path, pipeline):
    cfg.convert.versions = {MYBATIS: ""}
    fake = pipeline()
    ws = _ws(tmp_path)
    orch = _orch(cfg, ws)
    asked: list[tuple[str, str]] = []

    class Lookup:
        def versions(self, g, a):
            asked.append((g, a))
            return ["3.0.3", "3.0.4", "4.0.0-M1"]

    orch._convert_version_lookup = lambda: Lookup()
    report = orch.run(stop_after=Phase.INTAKE)
    assert asked == [tuple(MYBATIS.split(":"))]
    assert fake.calls[0][3].versions == {MYBATIS: "3.0.4"}            # the newest release, not the milestone
    assert FLAG_CONVERTED in report.flags


def test_refused_when_the_lookup_finds_no_version(cfg, tmp_path, pipeline):
    cfg.convert.versions = {MYBATIS: ""}
    fake = pipeline()
    ws = _ws(tmp_path)
    orch = _orch(cfg, ws)

    class Lookup:
        def versions(self, g, a):
            return ["4.0.0-RC1"]

    orch._convert_version_lookup = lambda: Lookup()
    report = orch.run()
    _assert_refused(report, ws, fake, f"{MYBATIS}의 버전을 저장소에서 찾지 못했습니다. convert.versions에 버전을 "
                                      "지정해 주세요.")


# ---------------------------------------------------------------- names


def test_root_pom_names(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "pom.xml").write_text(ROOT_POM)
    assert root_pom_names(tmp_path / "a").group_id == "com.acme.shop"
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "pom.xml").write_text(CHILD_POM)
    n = root_pom_names(tmp_path / "b")
    assert (n.group_id, n.artifact_id) == ("com.acme.parent", "order-web")
    (tmp_path / "c").mkdir()
    (tmp_path / "c" / "pom.xml").write_text("<project><groupId>${g}</groupId><artifactId>x</artifactId></project>")
    assert root_pom_names(tmp_path / "c") is None                     # an unresolved property is no package
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "pom.xml").write_text("<project><groupId>${g}</groupId><properties><g>com.p</g></properties>"
                                            "<artifactId>x</artifactId></project>")
    assert root_pom_names(tmp_path / "d").group_id == "com.p"
    (tmp_path / "e").mkdir()
    (tmp_path / "e" / "pom.xml").write_text("<project><artifactId>x</artifactId></project>")
    assert root_pom_names(tmp_path / "e") is None
    assert root_pom_names(tmp_path / "missing") is None
    assert project_domain("d1-shop", ["D1", "d2"]) == "D1"
    assert project_domain("shop", ["shop"]) == "" and project_domain("x-shop", ["d1"]) == ""


def test_names_come_from_the_root_pom(cfg, tmp_path, pipeline):
    cfg.convert.package_root = ""                      # not needed: the pom gives the package
    cfg.convert.domains = ["d1"]
    cfg.convert.default_domain = "d1"
    fake = pipeline()
    ws = _ws(tmp_path)
    _orch(cfg, ws).run(stop_after=Phase.INTAKE)
    _src, _out, rules, options = fake.calls[0]
    assert (options.group_id, options.source_name, options.override, options.domain) == (
        "com.acme.shop", "my-shop", "myShop", "")
    assert rules.get("naming.package_root") == "com.acme.shop"
    assert rules.get("naming.domains") == [] and rules.get("naming.default_domain") == ""
    assert options.boot_version == cfg.target.spring_boot_version and options.java_version == "21"


def test_names_from_a_root_pom_that_inherits_its_group(cfg, tmp_path, pipeline):
    fake = pipeline()
    ws = _ws(tmp_path, pom=CHILD_POM)
    _orch(cfg, ws).run(stop_after=Phase.INTAKE)
    options = fake.calls[0][3]
    assert (options.group_id, options.source_name, options.override) == ("com.acme.parent", "order-web", "orderWeb")
    assert fake.calls[0][2].get("naming.package_root") == "com.acme.parent"


def test_without_root_pom_the_package_root_and_the_project_name(cfg, tmp_path, pipeline):
    cfg.convert.package_root = "com.example"
    cfg.convert.domains = ["d1"]
    cfg.convert.group_id = "com.example.g"
    fake = pipeline()
    ws = Workspace.from_input(_project(tmp_path, name="d1-order", pom=None), tmp_path / "out", run_id="t-conv")
    _orch(cfg, ws, source=tmp_path / "src" / "d1-order").run(stop_after=Phase.INTAKE)
    _src, _out, rules, options = fake.calls[0]
    assert (options.source_name, options.domain, options.override, options.group_id) == (
        "d1-order", "d1", "", "com.example.g")
    assert rules.get("naming.package_root") == "com.example"


# ---------------------------------------------------------------- converted


def test_converted_run_replaces_the_tree_and_commits(cfg, tmp_path, pipeline):
    fake = pipeline(converted_result(queue=205))
    ws = _ws(tmp_path)
    # an ignored leftover (as-is build output) and a symlink leading out of the tree
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("not ours")
    (ws.root / "target" / "classes").mkdir(parents=True)
    (ws.root / "target" / "classes" / "A.class").write_bytes(b"\xca\xfe")
    (ws.root / "server.log").write_text("ignored by the project's .gitignore")
    os.symlink(outside, ws.root / "shared")
    report = _orch(cfg, ws).run(stop_after=Phase.INTAKE)

    assert "aborted" not in report.flags and FLAG_CONVERTED in report.flags
    assert fake.calls[0][0] == ws.root and fake.calls[0][1] == ws.run_dir / "convert" / "out"
    # the tree is exactly the generated tree plus .git
    assert _tree(ws.root) == GENERATED
    assert sorted(p.name for p in ws.root.iterdir()) == sorted({".git", *(r.split("/")[0] for r in GENERATED)})
    assert (outside / "keep.txt").read_text() == "not ours"           # the link was removed, not followed
    assert stat.S_IMODE((ws.root / EXECUTABLE).stat().st_mode) & 0o111
    assert not ws.is_dirty()
    # the conversion commit is the second commit, before the build-tool check
    assert _subjects(ws)[:2] == ["sbmig: as-is snapshot", CONVERT_COMMIT]
    sha = ws.head()
    assert report.conversion is not None and report.conversion.commit == sha
    names = [e["event"] for e in journal_events(ws)]
    assert names.index("framework_converted") < names.index("build_tool")
    # report.conversion: queue without excerpts, capped at 200
    conv = report.conversion
    assert conv.converted and conv.framework == "Example" and conv.project == "myShop"
    assert conv.package == "com.acme.shop.myShop" and conv.modules[0] == "myShop-boot" and len(conv.modules) == 4
    assert conv.datasource == "configured:oracle" and conv.counts["services"] == 2
    assert len(conv.queue) == 200 and conv.queue_total == 205
    assert set(conv.queue[0]) == {"component", "reason", "source_path"}
    # journal
    ev = _events(ws, "framework_converted")[0]
    assert {k: ev[k] for k in ("framework", "project", "package", "queue", "errors", "datasource", "commit")} == {
        "framework": "Example", "project": "myShop", "package": "com.acme.shop.myShop", "queue": 205, "errors": 0,
        "datasource": "configured:oracle", "commit": sha}
    assert ev["modules"] == conv.modules and ev["counts"]["java_files"] == 9
    assert ev["removed"]["tracked"] >= 5 and ev["removed"]["ignored"] >= 1
    # convert/queue.json (every item with its excerpt) and summary.json
    queue = json.loads((ws.run_dir / "convert" / "queue.json").read_text(encoding="utf-8"))
    assert len(queue) == 205 and queue[0]["excerpt"] == "<service id='0'/>" and queue[0]["last_error"]
    summary = json.loads((ws.run_dir / "convert" / "summary.json").read_text(encoding="utf-8"))
    assert summary["project"] == "myShop" and summary["queue_total"] == 205 and summary["commit"] == sha
    assert not (ws.run_dir / "convert" / "out").exists()
    # sbmig's ignore lines live in .git/info/exclude, not in a .gitignore of the generated tree
    assert "target/" in (ws.root / ".git" / "info" / "exclude").read_text(encoding="utf-8").splitlines()
    assert ws.change_base_commit == sha and ws.base_commit() == sha


def test_converted_run_goes_on_and_the_code_change_base_is_the_conversion(cfg, tmp_path, pipeline):
    pipeline()
    ws = _ws(tmp_path)
    rw = FakeRewriteRunner(ws, effect=lambda w, refs: (w.write("myShop-boot/pom.xml", GENERATED["myShop-boot/pom.xml"].replace(
        "</project>", "<!-- rewritten --></project>"))
                                                       or ["myShop-boot/pom.xml"]))
    report = _orch(cfg, ws, rewrite=rw, analyzer=_generated_profile).run()
    assert report.success and run_exit_code(report) == 0, (report.flags, report.manual_followups[:3])
    assert _subjects(ws)[:2] == ["sbmig: as-is snapshot", CONVERT_COMMIT]
    patch = Path(report.outputs["diff"]).read_text(encoding="utf-8")
    assert "myShop-boot/pom.xml" in patch and "rewritten" in patch
    touched = sorted(line.split(" b/", 1)[1] for line in patch.splitlines() if line.startswith("diff --git "))
    assert touched == ["module/boot-jar/pom.xml", "myShop-boot/pom.xml"], touched      # not every generated file
    index = json.loads((ws.run_dir / "codediff" / "index.json").read_text(encoding="utf-8"))
    paths = {f["path"]: f["status"] for f in index["files"]}
    assert paths == {"myShop-boot/pom.xml": "modified", "module/boot-jar/pom.xml": "added"}, paths


def test_duplicate_mapping_is_the_first_handover_line(cfg, tmp_path, pipeline):
    pipeline(converted_result(errors=["datasource: something else", DUP]))
    ws = _ws(tmp_path)
    report = _orch(cfg, ws).run(stop_after=Phase.INTAKE)
    first = report.manual_followups[0]
    assert first.startswith(DUPLICATE_LINE_HEAD + " (1)") and DUP in first
    assert "datasource: something else" not in first
    assert report.conversion.errors == ["datasource: something else", DUP]


def test_nothing_to_convert_leaves_the_tree_and_goes_on(cfg, tmp_path, pipeline):
    fake = pipeline(ConvertResult(converted=False, counts={"services": 0}))
    ws = _ws(tmp_path)
    before = _tree(ws.root)
    report = _orch(cfg, ws).run(stop_after=Phase.INTAKE)
    assert len(fake.calls) == 1 and "aborted" not in report.flags and FLAG_CONVERTED not in report.flags
    assert "stopped-after-intake" in report.flags
    assert _asis_kept(ws, before) and _subjects(ws) == ["sbmig: as-is snapshot"]
    assert _events(ws, "framework_convert_skipped")[0]["reason"] == "nothing to convert"
    assert SKIPPED_LINE in report.manual_followups
    assert report.conversion is not None and not report.conversion.converted
    assert _events(ws, "build_tool")[0]["ok"] is True                 # the run went on to the build-tool check
    assert ws.base_commit() == ws.first_commit()


def test_dry_run_never_writes_the_tree(cfg, tmp_path, pipeline):
    fake = pipeline()
    ws = _ws(tmp_path)
    before = _tree(ws.root)
    report = _orch(cfg, ws).run(stop_after=Phase.INTAKE, dry_run=True)
    assert fake.calls == [] and "aborted" not in report.flags
    assert _asis_kept(ws, before)
    assert _events(ws, "framework_convert_skipped")[0]["reason"] == "dry_run"


def test_a_failing_converter_aborts_cleanly(cfg, tmp_path, pipeline):
    pipeline(RuntimeError("converter broke"))
    ws = _ws(tmp_path)
    before = _tree(ws.root)
    report = _orch(cfg, ws).run()
    assert FLAG_FAILED in report.flags and "aborted" in report.flags and run_exit_code(report) == 3
    reason = _events(ws, "abort")[0]["reason"]
    assert reason == "framework conversion failed: RuntimeError: converter broke — the project was left as uploaded"
    assert tr_line("ko", reason) == "사내 규정 전환 중 오류가 나서 멈췄습니다(RuntimeError: converter broke). 프로젝트는 올린 그대로입니다."
    assert _asis_kept(ws, before) and _subjects(ws) == ["sbmig: as-is snapshot"]
    assert report.conversion is None and not _events(ws, "build_tool")


def test_an_unsafe_generated_path_aborts_with_the_as_is_tree(cfg, tmp_path, pipeline):
    pipeline(files={"pom.xml": "<project/>\n", ".git/config": "[core]\n"})
    ws = _ws(tmp_path)
    before = _tree(ws.root)
    report = _orch(cfg, ws).run()
    assert FLAG_FAILED in report.flags and run_exit_code(report) == 3
    assert _asis_kept(ws, before) and _subjects(ws) == ["sbmig: as-is snapshot"]


def test_a_result_without_poms_is_a_failed_conversion(cfg, tmp_path, pipeline):
    """The pipeline writes every file except the poms when a version is missing and says so first in `errors`."""
    files = {k: v for k, v in GENERATED.items() if not k.endswith("pom.xml")}
    pipeline(converted_result(errors=["skeleton: the Spring Boot version (ConvertOptions.boot_version) is empty, no "
                                      "pom written"]), files=files)
    ws = _ws(tmp_path)
    before = _tree(ws.root)
    report = _orch(cfg, ws).run()
    assert FLAG_FAILED in report.flags and FLAG_CONVERTED not in report.flags and run_exit_code(report) == 3
    reason = _events(ws, "abort")[0]["reason"]
    assert reason.startswith("framework conversion failed: the generated project has no poms (skeleton: ")
    assert reason.endswith(" — the project was left as uploaded")
    assert tr_line("ko", reason).startswith("사내 규정 전환 중 오류가 나서 멈췄습니다(")
    assert _asis_kept(ws, before) and not _events(ws, "framework_converted")


def test_a_refused_output_folder_is_a_failed_conversion(cfg, tmp_path, pipeline):
    pipeline(ConvertResult(converted=False, errors=["/x/convert/out: the output folder is not empty, nothing written"]))
    ws = _ws(tmp_path)
    report = _orch(cfg, ws).run()
    assert FLAG_FAILED in report.flags and run_exit_code(report) == 3
    assert not _events(ws, "framework_convert_skipped")


def test_a_generated_tree_without_root_pom_is_a_failed_conversion(cfg, tmp_path, pipeline):
    pipeline(files={"myShop-boot/pom.xml": GENERATED["myShop-boot/pom.xml"]})
    ws = _ws(tmp_path)
    before = _tree(ws.root)
    report = _orch(cfg, ws).run()
    assert FLAG_FAILED in report.flags and _asis_kept(ws, before)


# ---------------------------------------------------------------- review follow-up: order of the checks


def test_a_project_without_the_framework_is_never_stopped(cfg, tmp_path, pipeline):
    """Nothing to convert: a root pom whose groupId is no package, no package root, versions to look up, an
    unreachable repository: the run goes on untouched, no name or version check runs."""
    cfg.convert.package_root = ""
    cfg.convert.versions = {MYBATIS: ""}
    fake = pipeline(convertible=False)
    ws = _ws(tmp_path, pom="<project><groupId>${g}</groupId><artifactId>plain</artifactId></project>\n")
    before = _tree(ws.root)
    orch = _orch(cfg, ws)

    def no_lookup():
        raise AssertionError("no version lookup for a project with nothing to convert")

    orch._convert_version_lookup = no_lookup
    report = orch.run(stop_after=Phase.INTAKE, framework_mode="convert")
    assert not any(f.startswith("aborted") for f in report.flags), report.flags
    assert fake.checked == [ws.root] and fake.calls == []
    assert _asis_kept(ws, before) and SKIPPED_LINE in report.manual_followups
    assert _events(ws, "framework_convert_skipped")[0]["reason"] == "nothing to convert"
    assert not (ws.run_dir / "convert").exists()


def test_an_unavailable_server_default_runs_as_reduced(cfg, tmp_path, pipeline):
    cfg.convert.enabled = False                      # framework.mode is convert in the fixture
    fake = pipeline()
    ws = _ws(tmp_path)
    report = _orch(cfg, ws).run(stop_after=Phase.INTAKE)
    assert report.framework_mode == "reduced" and "aborted" not in report.flags
    assert fake.calls == [] and fake.checked == []
    ev = _events(ws, "framework_mode")[0]
    assert ev["framework_mode"] == "reduced" and ev["chosen"] is False and "not available" in ev["fallback"]


def test_no_framework_name_and_an_unchosen_default_runs_as_reduced(cfg, tmp_path, pipeline):
    cfg.framework.artifacts = []
    fake = pipeline()
    ws = _ws(tmp_path)
    report = _orch(cfg, ws).run(stop_after=Phase.INTAKE)
    assert report.framework_mode == "reduced" and fake.checked == [] and "aborted" not in report.flags


@pytest.mark.parametrize("pom,named", [
    ("<project><groupId>${g}</groupId><artifactId>shop</artifactId></project>\n",
     "the root pom's groupId '${g}' cannot be used as a package"),
    ("<project><groupId>kr.co.my-corp</groupId><artifactId>shop</artifactId></project>\n",
     "the root pom's groupId 'kr.co.my-corp' cannot be used as a package"),
    ("<project><groupId>com.example.new</groupId><artifactId>shop</artifactId></project>\n",
     "the root pom's groupId 'com.example.new' cannot be used as a package"),
    ("<project><artifactId>shop</artifactId></project>\n", "the root pom has no groupId"),
    ("<project><groupId>com.acme</groupId></project>\n", "the root pom has no artifactId"),
    ("<project><groupId>com.acme</groupId><artifactId>${app}</artifactId></project>\n",
     "the root pom's artifactId '${app}' cannot be used as a project name"),
    ("<project><groupId>", "the root pom cannot be read (not well-formed XML)"),
])
def test_the_refusal_names_what_the_root_pom_lacks(cfg, tmp_path, pipeline, pom, named):
    cfg.convert.package_root = ""
    fake = pipeline()
    ws = _ws(tmp_path, pom=pom)
    report = _orch(cfg, ws).run()
    reason = _events(ws, "abort")[0]["reason"]
    assert FLAG_UNAVAILABLE in report.flags and fake.calls == []
    assert reason == (f"framework conversion is not available: {named} and convert.package_root is not set — set "
                      "convert.package_root")
    assert tr_line("ko", reason) != reason and "루트 pom.xml" in tr_line("ko", reason)


def test_an_unusable_root_pom_falls_back_to_the_package_root(cfg, tmp_path, pipeline):
    cfg.convert.package_root = "com.example"
    fake = pipeline()
    ws = _ws(tmp_path, pom="<project><groupId>kr.co.my-corp</groupId><artifactId>shop</artifactId></project>\n")
    report = _orch(cfg, ws, source=tmp_path / "src" / "shop-app").run(stop_after=Phase.INTAKE)
    assert FLAG_CONVERTED in report.flags
    assert fake.calls[0][2].get("naming.package_root") == "com.example" and fake.calls[0][3].source_name == "shop-app"


# ---------------------------------------------------------------- review follow-up: a failure inside the swap


def _rich_project(tmp_path: Path) -> Workspace:
    """A project whose own .gitignore hides files (a properties file, a notes folder) beside build output."""
    src = _project(tmp_path)
    (src / ".gitignore").write_text("*.properties\nlocal/\n", encoding="utf-8")
    (src / "src/main/resources").mkdir(parents=True)
    (src / "src/main/resources/app.properties").write_text("db.url=jdbc:h2:mem:x\n", encoding="utf-8")
    (src / "local").mkdir()
    (src / "local/notes.txt").write_text("hand notes of the owners\n", encoding="utf-8")
    (src / "bin").mkdir()
    (src / "bin/run.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    os.chmod(src / "bin/run.sh", 0o755)
    ws = Workspace.from_input(src, tmp_path / "out", run_id="t-conv")
    ws.git_init()                                    # the .gitignore the run sees; the run's own init adds nothing
    (ws.root / "target" / "classes").mkdir(parents=True)
    (ws.root / "target" / "classes" / "A.class").write_bytes(b"\xca\xfe\xba\xbe")
    return ws


def _bytes_tree(root: Path) -> dict[str, tuple[bytes, int]]:
    out: dict[str, tuple[bytes, int]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        rel = Path(dirpath).relative_to(root)
        if rel == Path("."):
            dirnames[:] = [d for d in dirnames if d != ".git"]
        for d in dirnames:
            out[(rel / d).as_posix() + "/"] = (b"", stat.S_IMODE(os.lstat(Path(dirpath) / d).st_mode))
        for f in filenames:
            p = Path(dirpath) / f
            out[(rel / f).as_posix()] = (p.read_bytes(), stat.S_IMODE(os.lstat(p).st_mode))
    return out


def _move_failing_after(n: int):
    import shutil as _sh
    real = _sh.move
    count = {"n": 0}

    def move(a, b, *k, **kw):
        count["n"] += 1
        if count["n"] > n:
            raise OSError(28, "No space left on device")
        return real(a, b, *k, **kw)
    return move


def _assert_put_back(ws, before, report):
    assert FLAG_FAILED in report.flags and run_exit_code(report) == 3
    assert _bytes_tree(ws.root) == before
    reason = _events(ws, "abort")[0]["reason"]
    assert reason.endswith(" — the project was put back as uploaded"), reason
    assert ws._git("status", "--porcelain").stdout == "" and ws.head() == ws.first_commit() or \
        _subjects(ws) == ["sbmig: as-is snapshot", "sbmig: as-is snapshot"]
    assert CONVERT_COMMIT not in _subjects(ws)
    assert not (ws.run_dir / "convert" / "asis").exists()


def test_a_move_failing_after_three_files_puts_everything_back(cfg, tmp_path, pipeline, monkeypatch):
    from sbmig.loop import convert as cv
    pipeline()
    ws = _rich_project(tmp_path)
    before = _bytes_tree(ws.root)
    monkeypatch.setattr(cv.shutil, "move", _move_failing_after(3))
    report = _orch(cfg, ws).run()
    _assert_put_back(ws, before, report)


def test_a_failing_commit_puts_everything_back(cfg, tmp_path, pipeline, monkeypatch):
    pipeline()
    ws = _rich_project(tmp_path)
    before = _bytes_tree(ws.root)
    real = ws.commit

    def commit(message, *a, **kw):
        if message == CONVERT_COMMIT:
            real(message, *a, **kw)                  # the commit object exists, then the call fails
            raise RuntimeError("commit broke")
        return real(message, *a, **kw)

    monkeypatch.setattr(ws, "commit", commit)
    report = _orch(cfg, ws).run()
    _assert_put_back(ws, before, report)
    assert ws._git("diff", "--cached", "--quiet", check=False).returncode == 0


def test_a_failed_move_back_says_where_the_files_are(cfg, tmp_path, pipeline, monkeypatch):
    from sbmig.loop import convert as cv
    pipeline()
    ws = _rich_project(tmp_path)
    aside = ws.run_dir / "convert" / "asis"
    monkeypatch.setattr(cv.shutil, "move", _move_failing_after(1))
    real = cv.rename_entry

    def rename(src, dst):
        if Path(src).parent == aside and Path(src).name == "local":
            raise OSError(13, "Permission denied")
        return real(src, dst)

    monkeypatch.setattr(cv, "rename_entry", rename)
    report = _orch(cfg, ws).run()
    reason = _events(ws, "abort")[0]["reason"]
    assert FLAG_FAILED in report.flags
    assert reason.endswith(f" — the as-is files could not all be put back; what is missing is in {aside} (local)"), \
        reason
    assert (aside / "local" / "notes.txt").read_text() == "hand notes of the owners\n"
    assert (ws.root / "src/main/resources/app.properties").is_file()     # the rest is back
    assert tr_line("ko", reason).endswith(f"빠진 파일(local)은 {aside}에 있습니다.")


def test_a_read_only_folder_is_converted_and_put_back_with_its_mode(cfg, tmp_path, pipeline, monkeypatch):
    from sbmig.loop import convert as cv
    pipeline()
    ws = _rich_project(tmp_path)
    ro = ws.root / "docs-ro"
    ro.mkdir()
    (ro / "a.txt").write_text("x", encoding="utf-8")
    deep = ws.root / "local" / "ro"
    deep.mkdir()
    (deep / "b.txt").write_text("y", encoding="utf-8")
    os.chmod(ro, 0o555)
    os.chmod(deep, 0o555)
    before = _bytes_tree(ws.root)
    try:
        monkeypatch.setattr(cv.shutil, "move", _move_failing_after(2))
        report = _orch(cfg, ws).run()
        _assert_put_back(ws, before, report)
        assert stat.S_IMODE(ro.stat().st_mode) == 0o555 and stat.S_IMODE(deep.stat().st_mode) == 0o555
    finally:
        for d in (ro, deep):
            if d.exists():
                os.chmod(d, 0o755)


def test_a_read_only_folder_does_not_stop_the_conversion(cfg, tmp_path, pipeline):
    pipeline()
    ws = _rich_project(tmp_path)
    ro = ws.root / "docs-ro"
    ro.mkdir()
    (ro / "a.txt").write_text("x", encoding="utf-8")
    os.chmod(ro, 0o555)
    try:
        report = _orch(cfg, ws).run(stop_after=Phase.INTAKE)
    finally:
        if ro.exists():
            os.chmod(ro, 0o755)
    assert FLAG_CONVERTED in report.flags, report.manual_followups[:2]
    assert _tree(ws.root) == GENERATED and not (ws.run_dir / "convert" / "asis").exists()


# ---------------------------------------------------------------- review follow-up: secrets in excerpts


def test_queue_excerpts_are_scrubbed(cfg, tmp_path, pipeline):
    pw = "Zq9-FakePassw0rd-77"
    res = converted_result(queue=1)
    res.queue[0].excerpt = (f'public class DbConfig {{\n    private static final String password = "{pw}";\n'
                            f'    String url = "jdbc:oracle:thin:scott/{pw}@db:1521:ORCL";\n}}\n')
    pipeline(res)
    ws = _ws(tmp_path)
    report = _orch(cfg, ws).run(stop_after=Phase.INTAKE)
    assert FLAG_CONVERTED in report.flags
    text = (ws.run_dir / "convert" / "queue.json").read_text(encoding="utf-8")
    assert pw not in text and "class DbConfig" in text and "***" in text
    for p in ws.run_dir.rglob("*"):
        if p.is_file() and ".git" not in p.relative_to(ws.run_dir).parts:
            assert pw.encode() not in p.read_bytes(), p



# ---------------------------------------------------------------- second review follow-up


def test_an_entry_whose_mode_cannot_be_given_back_after_the_rename_is_put_back(cfg, tmp_path, pipeline, monkeypatch):
    """The rename into convert/asis succeeds, the chmod giving a read-only folder its mode back raises: the entry
    counts as moved, the roll back renames it back with its mode, and only then the reason says 'put back'."""
    from sbmig.loop import convert as cv
    pipeline()
    ws = _rich_project(tmp_path)
    ro = ws.root / "docs-ro"
    ro.mkdir()
    (ro / "a.txt").write_text("x", encoding="utf-8")
    os.chmod(ro, 0o555)
    before = _bytes_tree(ws.root)
    aside = ws.run_dir / "convert" / "asis"
    real = os.chmod
    hit = {"n": 0}

    def chmod(p, m, *k, **kw):
        if Path(p) == aside / "docs-ro" and m == 0o555 and hit["n"] < 2:
            hit["n"] += 1                            # the retry fails too
            raise OSError(5, "Input/output error")
        return real(p, m, *k, **kw)

    monkeypatch.setattr(cv.os, "chmod", chmod)
    try:
        report = _orch(cfg, ws).run(stop_after=Phase.INTAKE)
        assert hit["n"] == 2
        _assert_put_back(ws, before, report)
    finally:
        for d in (ro, aside / "docs-ro"):
            if d.exists():
                real(d, 0o755)


def test_a_failed_rename_out_gives_the_folder_its_mode_back(cfg, tmp_path, pipeline, monkeypatch):
    from sbmig.loop import convert as cv
    pipeline()
    ws = _rich_project(tmp_path)
    ro = ws.root / "docs-ro"
    ro.mkdir()
    (ro / "a.txt").write_text("x", encoding="utf-8")
    os.chmod(ro, 0o555)
    before = _bytes_tree(ws.root)
    real = os.rename

    def rename(a, b, *k, **kw):
        if Path(a).name == "docs-ro" and Path(b).parent.name == "asis":
            raise OSError(18, "Invalid cross-device link")
        return real(a, b, *k, **kw)

    monkeypatch.setattr(cv.os, "rename", rename)
    try:
        report = _orch(cfg, ws).run(stop_after=Phase.INTAKE)
        assert stat.S_IMODE(ro.stat().st_mode) == 0o555
        _assert_put_back(ws, before, report)
    finally:
        if ro.exists():
            os.chmod(ro, 0o755)


def test_put_back_is_said_only_when_the_aside_folder_is_empty(tmp_path, monkeypatch):
    """A roll back whose own list misses an entry still finds it in the aside folder and moves it back."""
    from sbmig.loop import convert as cv
    root, aside = tmp_path / "root", tmp_path / "asis"
    root.mkdir()
    (root / "a.txt").write_text("a")
    (root / "b").mkdir()
    (root / "b" / "c.txt").write_text("c")
    swap = cv.TreeSwap(root, aside)
    swap.move_aside()
    swap.moved.remove("b")                   # the list lost an entry
    problems, left = swap.rollback()
    assert (problems, left) == ("", []) and (root / "b" / "c.txt").read_text() == "c" and not aside.exists()
    # an entry that cannot go back is named, and the folder stays
    swap = cv.TreeSwap(root, aside)
    swap.move_aside()
    real = cv.rename_entry

    def rename(src, dst):
        if Path(src).name == "b":
            raise OSError(13, "Permission denied")
        return real(src, dst)

    monkeypatch.setattr(cv, "rename_entry", rename)
    problems, left = swap.rollback()
    assert left == ["b"] and problems and (aside / "b" / "c.txt").read_text() == "c"


PW2 = "Zq9Scrub77"


@pytest.mark.parametrize("text", [
    f'<property name="password">\n  <value>{PW2}</value>\n</property>',
    f'ds.setPassword("{PW2}");',
    f'String 비밀번호 = "{PW2}";',
    f'<property name="smtp-pass">\n\n  <value>{PW2}</value>',
    f'String token = "{PW2}"; // api token',
])
def test_excerpts_hide_the_blunt_forms(cfg, text):
    from sbmig.loop import convert as cv
    out = cv.scrub_excerpt(text, cfg)
    assert PW2 not in out and "***" in out, out


def test_ordinary_excerpts_of_the_fixture_stay_readable(cfg, tmp_path):
    """The real pipeline on the fixture: every excerpt without a secret word comes out unchanged."""
    import re

    from sbmig.fwconvert.model import ConvertOptions
    from sbmig.fwconvert.pipeline import convert_project
    from sbmig.fwconvert.rules import load_rules
    from sbmig.loop import convert as cv
    src = Path(__file__).resolve().parents[2] / "fixtures" / "projects" / "fw-convert-basic"
    rules = load_rules("Example", {"naming": {"package_root": "com.example"}})
    res = convert_project(src, tmp_path / "out", rules,
                          ConvertOptions(source_name="shop", boot_version="4.1.0", versions={MYBATIS: "3.0.5"}))
    excerpts = [q.excerpt for q in res.queue if q.excerpt]
    assert excerpts, "the fixture yields no excerpt to check"
    word = re.compile(r"(?i)pass|pwd|secret|credential|token|apikey|api_key|비밀번호|암호|인증키")
    plain = [e for e in excerpts if not word.search(e)]
    assert plain and all(cv.scrub_excerpt(e, cfg) == e for e in plain)


def test_nothing_readable_among_name_candidates_says_so(cfg, tmp_path, pipeline):
    from sbmig.loop import convert as cv
    fake = pipeline(convertible=(False, 3))
    ws = _ws(tmp_path)
    report = _orch(cfg, ws).run(stop_after=Phase.INTAKE)
    line = cv.unreadable_line(3)
    assert line in report.manual_followups and cv.SKIPPED_LINE not in report.manual_followups
    assert line.startswith("framework conversion found 3 files that look like framework files by name")
    ev = _events(ws, "framework_convert_skipped")[0]
    assert ev["reason"] == "nothing readable" and ev["candidates"] == 3 and fake.calls == []
    assert tr_line("ko", line) != line


def test_no_candidate_keeps_the_nothing_to_convert_line(cfg, tmp_path, pipeline):
    pipeline(convertible=(False, 0))
    ws = _ws(tmp_path)
    report = _orch(cfg, ws).run(stop_after=Phase.INTAKE)
    assert SKIPPED_LINE in report.manual_followups
    assert _events(ws, "framework_convert_skipped")[0]["reason"] == "nothing to convert"


# ---------------------------------------------------------------- final review follow-up


def test_rollback_survives_a_failing_listing_of_the_project_root(tmp_path, monkeypatch):
    from sbmig.loop import convert as cv
    root, aside = tmp_path / "root", tmp_path / "asis"
    root.mkdir()
    (root / "a.txt").write_text("a")
    (root / "b").mkdir()
    (root / "b" / "c.txt").write_text("c")
    swap = cv.TreeSwap(root, aside)
    swap.move_aside()
    (root / "gen-boot").mkdir()                       # a half-generated module
    real = Path.iterdir
    hit = {"n": 0}

    def it(self):
        if self == root:
            hit["n"] += 1                            # every listing of the root fails, the retry too
            raise OSError(5, "injected listing failure")
        return real(self)

    monkeypatch.setattr(Path, "iterdir", it)
    problems, left = swap.rollback()
    assert hit["n"] == 2 and left == [] and "injected listing failure" in problems
    assert (root / "a.txt").read_text() == "a" and (root / "b" / "c.txt").read_text() == "c"


def test_a_failing_root_listing_in_the_roll_back_gives_a_true_reason(cfg, tmp_path, pipeline, monkeypatch):
    from sbmig.loop import convert as cv
    pipeline()
    ws = _rich_project(tmp_path)
    before = _bytes_tree(ws.root)
    monkeypatch.setattr(cv.shutil, "move", _move_failing_after(2))
    real = Path.iterdir
    state = {"left": 2}

    def it(self):
        # the roll back's listing of the project root and its retry fail
        if self == ws.root and state["left"] and (ws.run_dir / "convert" / "asis").is_dir() \
                and any(real(ws.run_dir / "convert" / "asis")) and (ws.root / "myShop-boot").exists():
            state["left"] -= 1
            raise OSError(5, "injected listing failure")
        return real(self)

    monkeypatch.setattr(Path, "iterdir", it)
    report = _orch(cfg, ws).run()
    reason = _events(ws, "abort")[0]["reason"]
    assert FLAG_FAILED in report.flags and not reason.startswith("internal error"), reason
    assert "were put back, but not everything was cleaned up" in reason and "injected listing failure" in reason
    after = _bytes_tree(ws.root)
    assert all(after.get(k) == v for k, v in before.items())           # nothing of the as-is tree is missing


def test_keyboard_interrupt_during_the_swap_rolls_back_records_and_reraises(cfg, tmp_path, pipeline, monkeypatch):
    from sbmig.loop import convert as cv
    pipeline()
    ws = _rich_project(tmp_path)
    before = _bytes_tree(ws.root)

    def move(a, b, *k, **kw):
        raise KeyboardInterrupt()

    monkeypatch.setattr(cv.shutil, "move", move)
    orch = _orch(cfg, ws)
    with pytest.raises(KeyboardInterrupt):
        orch.run(stop_after=Phase.INTAKE)
    assert _bytes_tree(ws.root) == before
    assert FLAG_FAILED in orch.report.flags
    reason = _events(ws, "abort")[0]["reason"]
    assert reason == "framework conversion failed: KeyboardInterrupt — the project was put back as uploaded"
    assert not (ws.run_dir / "convert" / "swap.json").exists()


def test_the_swap_marker_says_what_is_moving(cfg, tmp_path, pipeline, monkeypatch):
    pipeline()
    ws = _rich_project(tmp_path)
    marker = ws.run_dir / "convert" / "swap.json"
    seen: dict = {}
    real = ws.commit

    def commit(message, *a, **kw):
        if message == CONVERT_COMMIT:
            seen.update(json.loads(marker.read_text(encoding="utf-8")))
        return real(message, *a, **kw)

    monkeypatch.setattr(ws, "commit", commit)
    report = _orch(cfg, ws).run(stop_after=Phase.INTAKE)
    assert FLAG_CONVERTED in report.flags
    assert seen["state"] == "moving" and seen["asis_dir"] == str(ws.run_dir / "convert" / "asis")
    assert seen["project_dir"] == str(ws.root) and "src" in seen["entries"] and ".git" not in seen["entries"]
    assert not marker.exists()


def test_the_swap_marker_stays_when_the_roll_back_is_incomplete(cfg, tmp_path, pipeline, monkeypatch):
    from sbmig.loop import convert as cv
    pipeline()
    ws = _rich_project(tmp_path)
    aside = ws.run_dir / "convert" / "asis"
    monkeypatch.setattr(cv.shutil, "move", _move_failing_after(1))
    real = cv.rename_entry

    def rename(src, dst):
        if Path(src).parent == aside and Path(src).name == "local":
            raise OSError(13, "Permission denied")
        return real(src, dst)

    monkeypatch.setattr(cv, "rename_entry", rename)
    _orch(cfg, ws).run()
    marker = json.loads((ws.run_dir / "convert" / "swap.json").read_text(encoding="utf-8"))
    assert marker["state"] == "incomplete" and marker["left"] == ["local"]
    assert marker["asis_dir"] == str(aside) and "local" in marker["entries"]


def test_last_error_is_scrubbed_and_only_in_the_queue_file(cfg, tmp_path, pipeline):
    pw = "Lq4-FakeSecret-91"
    res = converted_result(queue=1)
    res.queue[0].last_error = f'line 12: ajax data {{ password: "{pw}" }} not mapped'
    res.queue[0].excerpt = "<service id='0'/>"
    pipeline(res)
    ws = _ws(tmp_path)
    report = _orch(cfg, ws).run(stop_after=Phase.INTAKE)
    queue = json.loads((ws.run_dir / "convert" / "queue.json").read_text(encoding="utf-8"))
    assert pw not in queue[0]["last_error"] and "ajax data" in queue[0]["last_error"]
    summary = (ws.run_dir / "convert" / "summary.json").read_text(encoding="utf-8")
    assert "last_error" not in summary and "excerpt" not in summary and "ajax data" not in summary
    assert set(report.conversion.queue[0]) == {"component", "reason", "source_path"}


V3 = "Hq7Zx2Val"


@pytest.mark.parametrize("text", [
    f'    private static final String DB_PASSWORD =\n            "{V3}";',
    f'    // DB password\n    private static final String DB = "{V3}";',
    f'    ds.setPassword(\n        "{V3}");',
    f'    String password = """\n        {V3}\n        more {V3}\n        """;',
    f'    String dbConn = "{V3}"; // 패스워드',
    f'    private static final String AES_KEY = "{V3}";',
    f'    // credential\n\n    String x = "{V3}";',
])
def test_the_blunt_mask_reaches_the_next_lines(cfg, text):
    from sbmig.loop import convert as cv
    out = cv.scrub_excerpt(text, cfg)
    assert V3 not in out, out


def test_key_alone_and_far_lines_stay_readable(cfg):
    from sbmig.loop import convert as cv
    text = ('    String cacheKey = "orders";\n    // password below is read from the vault\n    int a = 1;\n'
            '    int b = 2;\n    String label = "Order list";')
    out = cv.scrub_excerpt(text, cfg)
    assert '"orders"' in out and '"Order list"' in out
