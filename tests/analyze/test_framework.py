"""Framework projects (v2.1z-ak): static detection, the dependency:tree check, the scope decision, the Korean
Handover line. Placeholder coordinates only."""
from __future__ import annotations

from pathlib import Path

import pytest

from sbmig.analyze import analyze_path
from sbmig.analyze.framework import (FLAG, decide_scope, detect_framework, framework_followup, summary_text,
                                     transitive_evidence)
from sbmig.analyze.pom_parser import parse_poms
from sbmig.analyze.analyzer import PathView
from sbmig.build import deptree
from sbmig.core.models import (CaseType, FrameworkEvidence, FrameworkUse, JarCoordinate, PomInfo, ProjectProfile,
                               ServiceInfo)
from tests.analyze.helpers import dep, pom, write

FW = "com.example.fw:example-fw-core"


def _fw(cfg, *patterns, scope="jdk-jakarta", transitive="apply"):
    cfg.framework.artifacts = list(patterns) or [FW]
    cfg.framework.scope = scope
    cfg.framework.transitive = transitive
    return cfg


def _poms(root: Path):
    return parse_poms(PathView(root))[0]


def _single(tmp_path: Path, deps=(), managed=()):
    write(tmp_path, "pom.xml", pom("app", deps=deps, managed=managed))
    return tmp_path


# ------------------------------------------------------------------------------------------------- detection
def test_flag_name():
    assert FLAG == "framework-project"


def test_disabled_gives_none(tmp_path, cfg):
    cfg.framework.artifacts = []
    _single(tmp_path, deps=(dep("com.example.fw", "example-fw-core", "1.2.0"),))
    assert detect_framework(PathView(tmp_path), cfg, _poms(tmp_path), []) is None
    assert decide_scope(None, cfg) is None
    assert analyze_path(tmp_path, cfg).framework is None


def test_direct_in_root(tmp_path, cfg):
    _fw(cfg)
    _single(tmp_path, deps=(dep("com.example.fw", "example-fw-core", "1.2.0"), dep("org.x", "y", "1")))
    use = detect_framework(PathView(tmp_path), cfg, _poms(tmp_path), [], effective={})
    assert [(e.source, e.artifact, e.version, e.where) for e in use.evidence] == [
        ("direct", FW, "1.2.0", "pom.xml")]
    assert use.patterns == [FW] and use.keys() == [FW]
    assert "parent: no as-is effective pom" in use.unchecked


def test_direct_in_module(tmp_path, cfg):
    _fw(cfg, "example-fw-*")
    write(tmp_path, "pom.xml", pom("root", packaging="pom", modules=("core",)))
    write(tmp_path, "core/pom.xml", pom("core", parent=("com.acme", "root", "1.0.0"),
                                         deps=(dep("com.example.fw", "example-fw-core", "1.2.0"),)))
    use = detect_framework(PathView(tmp_path), cfg, _poms(tmp_path), [], effective={})
    assert [(e.source, e.where) for e in use.evidence] == [("direct", "core/pom.xml")]


def test_managed_only_is_not_evidence(tmp_path, cfg):
    _fw(cfg)
    _single(tmp_path, managed=(dep("com.example.fw", "example-fw-core", "1.2.0"),))
    use = detect_framework(PathView(tmp_path), cfg, _poms(tmp_path), [], effective={})
    assert use is not None and not use.found
    assert decide_scope(use, cfg).scope == "full"


@pytest.mark.parametrize("pattern,hit", [
    ("example-fw-core", True), (FW, True), ("com.example.fw:*", True), ("*:example-fw-*", True),
    ("example-fw-web", False), ("org.other:example-fw-core", False)])
def test_patterns(tmp_path, cfg, pattern, hit):
    _fw(cfg, pattern)
    _single(tmp_path, deps=(dep("com.example.fw", "example-fw-core", "1.2.0"),))
    use = detect_framework(PathView(tmp_path), cfg, _poms(tmp_path), [], effective={})
    assert use.found is hit


def _capture(by_module=True, declared_root="com.acme:app"):
    own = {"managed": {}, "direct": {FW: "1.2.0", "org.x:y": "1"},
           "parent": "com.example.fw:example-fw-parent:1.0.0"}
    data = {"managed": {}, "direct": dict(own["direct"]), "plugins": {}, "properties": {},
            "parent": own["parent"], "modules": [declared_root], "by_module": {}}
    if by_module:
        data["by_module"] = {declared_root: own}
    return data


@pytest.mark.parametrize("by_module", [True, False])
def test_parent_from_capture(tmp_path, cfg, by_module):
    _fw(cfg)
    write(tmp_path, "pom.xml", pom("app", group="com.acme", parent=("com.example.fw", "example-fw-parent", "1.0.0")))
    use = detect_framework(PathView(tmp_path), cfg, _poms(tmp_path), [], effective=_capture(by_module))
    assert [(e.source, e.artifact, e.version, e.where, e.parent) for e in use.evidence] == [
        ("parent", FW, "1.2.0", "pom.xml", "com.example.fw:example-fw-parent:1.0.0")]
    assert use.unchecked == []
    assert decide_scope(use, cfg).reduced


def test_parent_not_reported_when_a_project_pom_declares_it(tmp_path, cfg):
    _fw(cfg)
    _single(tmp_path, deps=(dep("com.example.fw", "example-fw-core", "1.2.0"),))
    use = detect_framework(PathView(tmp_path), cfg, _poms(tmp_path), [], effective=_capture())
    assert [e.source for e in use.evidence] == ["direct"]


def test_parent_without_capture_is_unchecked(tmp_path, cfg):
    _fw(cfg)
    _single(tmp_path)
    use = detect_framework(PathView(tmp_path), cfg, _poms(tmp_path), [])     # PathView: no run dir
    assert not use.found
    assert use.unchecked == ["parent: no as-is effective pom"]


def test_parent_reads_the_run_capture(tmp_path, cfg):
    import json

    _fw(cfg)
    proj = _single(tmp_path / "p")
    run = tmp_path / "run"
    (run / "logs").mkdir(parents=True)
    (run / "logs" / "effective-depmgmt.json").write_text(json.dumps(_capture()), encoding="utf-8")

    class View(PathView):
        run_dir = run

    use = detect_framework(View(proj), cfg, _poms(proj), [])
    assert [e.source for e in use.evidence] == ["parent"]


def test_bundled_by_coordinates_and_by_name(tmp_path, cfg):
    _fw(cfg)
    jars = [JarCoordinate(path="src/main/webapp/WEB-INF/lib/fw.jar", group_id="com.example.fw",
                          artifact_id="example-fw-core", version="1.2.0", source="pom_properties"),
            JarCoordinate(path="WEB-INF/lib/example-fw-core-1.2.0.jar"),
            JarCoordinate(path="WEB-INF/lib/other-1.0.jar")]
    use = detect_framework(PathView(tmp_path), cfg, [], jars, effective={})
    assert [(e.source, e.artifact, e.where) for e in use.evidence] == [
        ("bundled", FW, "src/main/webapp/WEB-INF/lib/fw.jar"),
        ("bundled", "example-fw-core-1.2.0.jar", "WEB-INF/lib/example-fw-core-1.2.0.jar")]
    assert use.keys() == [FW]                       # a jar without coordinates has no g:a


def test_evidence_order_and_dedup(tmp_path, cfg):
    _fw(cfg)
    write(tmp_path, "pom.xml", pom("app", deps=(dep("com.example.fw", "example-fw-core", "1.2.0"),
                                                dep("com.example.fw", "example-fw-core", "1.2.0"))))
    jars = [JarCoordinate(path="lib/example-fw-core.jar")]
    use = detect_framework(PathView(tmp_path), cfg, _poms(tmp_path), jars, effective={})
    assert [e.source for e in use.evidence] == ["direct", "bundled"]


def test_analyze_project_attaches_the_decision(tmp_path, cfg):
    _fw(cfg)
    _single(tmp_path, deps=(dep("com.example.fw", "example-fw-core", "1.2.0"),))
    prof = analyze_path(tmp_path, cfg)
    assert prof.framework is not None and prof.reduced_scope
    assert "scope jdk-jakarta" in prof.framework.reason


# ------------------------------------------------------------------------------------------------- decide_scope
def _use(*sources):
    ev = [FrameworkEvidence(source=s, artifact=FW, version="1.2.0", where="pom.xml",
                            via=["com.example:lib-a:1.0", f"{FW}:1.2.0"] if s == "transitive" else [])
          for s in sources]
    return FrameworkUse(patterns=[FW], evidence=ev)


@pytest.mark.parametrize("scope", ["jdk-jakarta", "full"])
@pytest.mark.parametrize("transitive", ["apply", "note", "off"])
@pytest.mark.parametrize("sources", [(), ("direct",), ("parent",), ("bundled",), ("transitive",),
                                     ("direct", "transitive")])
def test_decide_scope_matrix(cfg, scope, transitive, sources):
    _fw(cfg, scope=scope, transitive=transitive)
    out = decide_scope(_use(*sources), cfg)
    applies = any(s != "transitive" or transitive == "apply" for s in sources)
    expected = "jdk-jakarta" if scope == "jdk-jakarta" and applies else "full"
    assert out.scope == expected
    assert decide_scope(out, cfg) == out                       # idempotent
    if not sources:
        assert out.reason.startswith("no framework artifact found")
    elif scope == "full":
        assert "framework.scope=full" in out.reason
    elif expected == "jdk-jakarta":
        assert out.reason.endswith("→ scope jdk-jakarta")
    else:
        assert f"only as a transitive dependency and framework.transitive={transitive}" in out.reason


def test_framework_fix_loop_defaults_to_false(cfg):
    """Framework projects get no fix loop unless the yml says `framework.fix_loop: true`."""
    from sbmig.core.config import FrameworkConfig
    assert FrameworkConfig().fix_loop is False
    assert cfg.framework.fix_loop is False                         # config/default.yml + the test config
    assert decide_scope(_use("direct"), _fw(cfg)).fix_loop is False


def test_decide_scope_names_the_framework_and_records_the_mode(cfg):
    """v2.1z-aw: `name` = display_name of the evidence found, `fix_loop` = framework.fix_loop."""
    _fw(cfg, "example-*")                                          # a glob pattern names nothing: the evidence does
    cfg.framework.fix_loop = False
    out = decide_scope(_use("direct"), cfg)
    assert out.name == "Example" and out.fix_loop is False
    cfg.framework.name, cfg.framework.fix_loop = "Acme", True
    out = decide_scope(_use("direct"), cfg)
    assert out.name == "Acme" and out.fix_loop is True
    assert decide_scope(out, cfg) == out
    cfg.framework.name = ""
    assert decide_scope(_use(), cfg).name == ""                    # nothing found: no name
    bundled = FrameworkUse(patterns=["example-*"], evidence=[FrameworkEvidence(
        source="bundled", artifact="example-fw-core-1.2.0.jar", where="WEB-INF/lib/example-fw-core-1.2.0.jar")])
    assert decide_scope(bundled, cfg).name == ""                   # a jar name is not an artifact id


def test_summary_text():
    assert summary_text(_use("direct")) == f"{FW} 1.2.0 (direct: pom.xml)"
    assert summary_text(_use("direct", "bundled")) == f"{FW} 1.2.0 (direct: pom.xml) and 1 more"
    assert summary_text(_use("transitive")) == f"{FW} 1.2.0 (transitive: pom.xml via com.example:lib-a:1.0)"


# ------------------------------------------------------------------------------------------------- transitive
TREE = """com.acme:app:jar:1.0.0
+- com.example:lib-a:jar:1.0:compile
|  \\- com.example.fw:example-fw-core:jar:1.2.0:compile
\\- com.example.fw:example-fw-web:jar:1.2.0:compile
"""


class _WS:
    def __init__(self, has_root=True):
        self.has_root = has_root

    def exists(self, rel):
        return self.has_root and rel == "pom.xml"


def _profile(**kw):
    p = ProjectProfile(root="/x", name="app", case_type=kw.pop("case", CaseType.SPRING_LEGACY),
                       poms=[PomInfo(path="pom.xml", group_id="com.acme", artifact_id="app")], **kw)
    p.root_pom = p.poms[0]
    return p


def test_transitive_evidence_from_tree(cfg, monkeypatch):
    _fw(cfg, "example-fw-*")
    calls = []

    def fake(ws, cfg_, module=None, includes=(), *, standalone=False):
        calls.append((module, list(includes), standalone))
        return deptree.parse_tree(TREE), None

    monkeypatch.setattr(deptree, "dependency_tree_status", fake)
    ev, why = transitive_evidence(_WS(), cfg, _profile())
    assert why is None
    assert calls == [(None, ["*:example-fw-*"], False)]
    assert [(e.source, e.artifact, e.where, e.via) for e in ev] == [
        ("direct", "com.example.fw:example-fw-web", "pom.xml", []),
        ("transitive", FW, "pom.xml", ["com.example:lib-a:1.0", f"{FW}:1.2.0"])]


def test_transitive_nothing_matched_vs_failed(cfg, monkeypatch):
    _fw(cfg)
    monkeypatch.setattr(deptree, "dependency_tree_status",
                        lambda *a, **k: (deptree.parse_tree("com.acme:app:jar:1.0.0\n"), None))
    assert transitive_evidence(_WS(), cfg, _profile()) == ([], None)
    monkeypatch.setattr(deptree, "dependency_tree_status", lambda *a, **k: ([], "dependency:tree failed for root"))
    assert transitive_evidence(_WS(), cfg, _profile()) == ([], "dependency:tree failed for root")
    monkeypatch.setattr(deptree, "dependency_tree_status", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    ev, why = transitive_evidence(_WS(), cfg, _profile())
    assert ev == [] and "RuntimeError" in why


def test_transitive_no_root_pom(cfg):
    _fw(cfg)
    prof = ProjectProfile(root="/x", name="app", case_type=CaseType.PURE_JAVA)
    assert transitive_evidence(_WS(has_root=False), cfg, prof) == ([], "no root pom")


def test_transitive_msa_without_root_aggregator_runs_per_service(cfg, monkeypatch):
    _fw(cfg)
    calls = []
    monkeypatch.setattr(deptree, "dependency_tree_status",
                        lambda ws, c, module=None, includes=(), *, standalone=False:
                        (calls.append((module, standalone)), ([], None))[1])
    svcs = [ServiceInfo(name="a", root_pom="a/pom.xml", case_type=CaseType.SPRING_BOOT),
            ServiceInfo(name="b", root_pom="b/pom.xml", case_type=CaseType.SPRING_BOOT)]
    prof = ProjectProfile(root="/x", name="x", case_type=CaseType.MSA, services=svcs)
    assert transitive_evidence(_WS(has_root=False), cfg, prof) == ([], None)
    assert calls == [("a", True), ("b", True)]


# ------------------------------------------------------------------------------------------------- Korean line
def _decided(cfg, *ev):
    return decide_scope(FrameworkUse(patterns=[FW], evidence=list(ev)), cfg)


def test_followup_samples(cfg):
    _fw(cfg)
    tail = "JDK 21과 Jakarta만 적용했고 구조 정리와 구조 검토는 하지 않았습니다. 부모 pom과 프레임워크 버전은 그대로입니다."
    direct = FrameworkEvidence(source="direct", artifact=FW, version="1.2.0", where="pom.xml")
    assert framework_followup(_decided(cfg, direct)) == (
        f"Example 프레임워크 프로젝트입니다({FW} 1.2.0, pom.xml에 직접 선언). " + tail)
    parent = FrameworkEvidence(source="parent", artifact=FW, version="1.2.0", where="pom.xml",
                               parent="com.example.fw:example-fw-parent:1.0.0")
    assert framework_followup(_decided(cfg, parent)) == (
        f"Example 프레임워크 프로젝트입니다({FW} 1.2.0, 부모 pom com.example.fw:example-fw-parent:1.0.0에서 상속). " + tail)
    bundled = FrameworkEvidence(source="bundled", artifact="example-fw-core-1.2.0.jar",
                                where="WEB-INF/lib/example-fw-core-1.2.0.jar")
    _fw(cfg, "example-fw-*")                     # no artifact id to derive a name from
    assert framework_followup(_decided(cfg, bundled)) == (
        "사내 프레임워크 프로젝트입니다(example-fw-core-1.2.0.jar, WEB-INF/lib에 jar로 포함). " + tail)
    _fw(cfg)
    trans = FrameworkEvidence(source="transitive", artifact=FW, version="1.2.0", where="pom.xml",
                              via=["com.example:lib-a:1.0", f"{FW}:1.2.0"])
    assert framework_followup(_decided(cfg, trans)) == (
        f"Example 프레임워크 프로젝트입니다({FW} 1.2.0, com.example:lib-a:1.0의 의존성). " + tail)
    assert framework_followup(_decided(cfg, direct, parent, bundled)) == (
        f"Example 프레임워크 프로젝트입니다({FW} 1.2.0, pom.xml에 직접 선언 외 2곳). " + tail)

    _fw(cfg, scope="full")
    assert framework_followup(_decided(cfg, direct)) == (
        f"사내 프레임워크 아티팩트를 사용합니다({FW} 1.2.0, pom.xml에 직접 선언). "
        "설정(framework.scope: full)에 따라 전체 이관을 진행했습니다.")
    _fw(cfg, transitive="note")
    assert framework_followup(_decided(cfg, trans)) == (
        f"다른 라이브러리가 사내 프레임워크 아티팩트를 가져옵니다({FW} 1.2.0, com.example:lib-a:1.0의 의존성). "
        "설정(framework.transitive: note)에 따라 전체 이관을 진행했습니다.")
    assert framework_followup(_decided(cfg)) == ""


def test_followup_has_no_em_dash_or_arrow(cfg):
    _fw(cfg)
    text = framework_followup(_decided(cfg, FrameworkEvidence(source="direct", artifact=FW, where="pom.xml")))
    assert "—" not in text and "→" not in text
    assert f"({FW}, pom.xml에 직접 선언)" in text           # no version → no dangling space


# ------------------------------------------------------------------------------------------------- deptree status
def test_dependency_tree_status_tells_failure_from_no_match(tmp_path, cfg, monkeypatch):
    from sbmig.build.mvn import ProcessResult
    from sbmig.core.workspace import Workspace

    root = tmp_path / "project"
    (root / "svc").mkdir(parents=True)
    (root / "pom.xml").write_text("<project><groupId>com.acme</groupId><artifactId>app</artifactId></project>")
    (root / "svc" / "pom.xml").write_text("<project><groupId>com.acme</groupId><artifactId>svc</artifactId></project>")
    ws = Workspace(root, tmp_path / "run", "t")
    calls: list[list[str]] = []

    def ok(cmd, *, cwd, env, log_path, timeout_s):
        calls.append(list(cmd))
        out = next(c.split("=", 1)[1] for c in cmd if c.startswith("-DoutputFile="))
        Path(out).write_text("com.acme:app:jar:1.0.0\n", encoding="utf-8")
        return ProcessResult(exit_code=0, text="", duration_s=0.1)

    monkeypatch.setattr(deptree, "run_logged", ok)
    roots, why = deptree.dependency_tree_status(ws, cfg, None, ["*:example-fw-core"])
    assert why is None and len(roots) == 1 and not roots[0].children
    roots, why = deptree.dependency_tree_status(ws, cfg, "svc", ["*:example-fw-core"], standalone=True)
    assert why is None and calls[-1][calls[-1].index("-f") + 1] == "svc/pom.xml" and "-pl" not in calls[-1]

    def fail(cmd, *, cwd, env, log_path, timeout_s):
        return ProcessResult(exit_code=1, text="[ERROR] boom", duration_s=0.1)

    monkeypatch.setattr(deptree, "run_logged", fail)
    roots, why = deptree.dependency_tree_status(ws, cfg, None, ["*:other"])
    assert roots == [] and why.startswith("dependency:tree failed for root (exit 1")
    assert deptree.dependency_tree(ws, cfg, None, ["*:other"]) == []
    assert deptree.dependency_tree_status(ws, cfg, "nope")[1] == "unknown module nope"
