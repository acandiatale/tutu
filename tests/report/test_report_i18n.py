"""report.language (en | ko): Korean report.md, English unchanged, report.json language-independent, table drift."""
from __future__ import annotations

import ast
import json
import re
import string
from pathlib import Path

from sbmig.core.models import BlockedItem, BlockReason, BuildError, ErrorKind, Phase
from sbmig.core.workspace import Workspace
from sbmig.report import build_handover, render_markdown, write_report
from sbmig.report import handover as handover_mod
from sbmig.report import inventory as inventory_mod
from sbmig.report import markdown as markdown_mod
from sbmig.report.i18n import PATTERNS, STRINGS, normalize, tr, tr_line

from .test_blocked_report import blocked_items
from .test_handover import EFFECTIVE, as_is_profile, final_tree, make_report
from .test_merge_conflicts import DIFFERING, IDENTICAL
from .test_properties_handover import inventory
from .test_report import sample_report

KO = STRINGS["ko"]
EN_HEADINGS = ("# Migration report — ", "## Handover — what we replaced, what is yours", "## Error list",
               "## Manual follow-ups", "## Flags",
               "### Versions replaced", "### Libraries needing your attention", "### From here on, your team",
               "### Properties to set before running", "| Run id |", "**Merge conflicts**")
GUIDE_HEADINGS = ("# 마이그레이션 보고서 — ", "## 전환 내용", "### 의존성 버전", "## 확인해 주세요", "## 해결하지 못한 오류",
                  "## 진행하지 않은 항목", "## 실행 전에 설정할 프로퍼티", "| 실행 ID |", "**모듈 병합에서 겹친 파일**",
                  "## 참고")


def _full_report(tmp_path, cfg):
    """Handover with versions, advisories, merge conflicts, properties, blocked, unresolved and flags."""
    blocked = BlockedItem(id="b1", reason=BlockReason.EXTERNAL_JAVAX_API, phase=Phase.SPRING, cluster_id="c")
    report = make_report(blocked=[blocked], flags=["test-compile-skipped", "rewrite-parse-problems"],
                         unresolved=[BuildError(kind=ErrorKind.COMPILE, message="cannot find symbol",
                                                file="src/A.java", line=3, symbol="class Foo")],
                         merge_conflicts=[DIFFERING, IDENTICAL], properties=inventory(), success=False)
    report.handover = build_handover(as_is_profile(), final_tree(tmp_path), report, effective=EFFECTIVE, cfg=cfg)
    return report


def test_korean_headings_replace_english(tmp_path, cfg):
    # v2.1z-as: `ko` renders the guide (sbmig/report/guide_ko.py); the English report keeps its diagnostic layout
    report = _full_report(tmp_path, cfg)
    ko = render_markdown(report, language="ko")
    en = render_markdown(report)
    for e in EN_HEADINGS:
        assert e in en, e
        assert e not in ko, e
    for k in GUIDE_HEADINGS:
        assert k in ko, k
    for gone in ("## 인수인계", "## 에러 목록", "## 수동 후속 조치", "## 플래그", "### 이후 담당 팀이 할 일"):
        assert gone not in ko, gone
    assert "❌ 빌드 실패" in ko and "NOT BUILDING" not in ko
    assert "| 항목 | 변경 전 | 변경 후 | 비고 |" in ko and "새 부모 pom에 버전이 없어 기존 버전을 유지" in ko
    assert "| 키 | 변경 전 설정 위치 | 참조 위치 | 기록한 값 | 설정 방법 |" in ko
    # Handover data lines (English in report.json) are rendered in Korean …
    assert "**진행하지 않은 항목 1건**" in ko and "1 cluster(s) blocked" not in ko
    assert "**기존 버전을 유지한 의존성** — `org.apache.commons:commons-lang3`" in ko
    assert "중복 파일 1개를 하나로 합쳤습니다" in ko and "kept the copy from" not in ko
    # … identifiers, free-text follow-ups and compiler messages stay English
    assert "- 플래그 `test-compile-skipped`: 메인 코드는 빌드되지만" in ko
    assert "web.xml not migrated" in ko and "| `src/A.java` | 3 | cannot find symbol |" in ko


def test_blocked_not_building_in_korean():
    r = sample_report()
    r.blocked = blocked_items()
    ko = render_markdown(r, language="ko")
    assert "## 진행하지 않은 항목" in ko and "## Blocked" not in ko and "## 차단됨" not in ko
    assert "| **차단됨** |" not in ko                       # the head table holds six rows only
    assert "| 구분 | 아티팩트 / 클래스 | 필요한 타입 → 실제 타입 | 파일 | 수정 횟수 | 조치 방법 |" in ko
    assert "(클래스 `com.company.legacy.web.LegacyRequestHelper`)" in ko
    assert "<details><summary><code>blk-1</code> 관련 오류 1건</summary>" in ko
    # no error list was kept: the last build's unresolved errors are listed by file
    sec = ko.split("## 해결하지 못한 오류 — 5건", 1)[1].split("\n## ", 1)[0]
    assert "| `src/A.java` | 3 | cannot find symbol |" in sec and "| `core` | - | weird |" in sec
    assert "| 시작 / 종료 |" in ko and "(2분 05초)" in ko and "핸들러" not in ko
    assert "- plan: plan note" in ko and "- spring: spring note" in ko
    assert "Blocked —" not in ko and "item(s)" not in ko and "error(s)" not in ko


def test_success_in_korean():
    ok = render_markdown(sample_report(success=True).model_copy(update={"timings_s": {}}), language="ko")
    assert "✅ 빌드 성공" in ok and "## 해결하지 못한 오류" not in ok and "지식 베이스" not in ok
    assert "BUILD SUCCESS" not in ok and "## 전환 내용" not in ok


def test_english_default_is_unchanged(tmp_path, cfg):
    for r in (sample_report(), sample_report(success=True), _full_report(tmp_path, cfg)):
        en = render_markdown(r)
        assert render_markdown(r, language="en") == en
        assert render_markdown(r, language="fr") == en and render_markdown(r, language=None) == en  # type: ignore[arg-type]
    assert normalize("KO") == "ko" and normalize("") == "en" and normalize(None) == "en"


def test_writer_uses_cfg_language_and_json_is_language_independent(tmp_path, cfg):
    assert cfg.report.language == "en"
    ko_cfg = cfg.model_copy(update={"report": cfg.report.model_copy(update={"language": "ko"})})
    out: dict[str, tuple[str, str]] = {}
    base = sample_report()                                              # one instance: its timestamps are fixed
    for name, c in (("en", cfg), ("ko", ko_cfg), ("none", None)):
        rep = base.model_copy(deep=True)
        ws = Workspace(tmp_path / "p", tmp_path / "run", "run-1")      # same run dir → same report_* outputs
        paths = write_report(rep, ws, c)
        out[name] = (Path(paths["report_md"]).read_text(encoding="utf-8"),
                     Path(paths["report_json"]).read_text(encoding="utf-8"))
    assert out["ko"][0].startswith("# 마이그레이션 보고서 — demo")
    assert out["en"][0].startswith("# Migration report — demo") and out["none"][0] == out["en"][0]
    assert out["en"][1] == out["ko"][1] == out["none"][1]
    assert json.loads(out["ko"][1])["project_name"] == "demo"
    ws = Workspace(tmp_path / "p2", tmp_path / "run2", "run-2")
    assert Path(write_report(sample_report(), ws, cfg, language="ko")["report_md"]).read_text(
        encoding="utf-8").startswith("# 마이그레이션 보고서")


def test_company_example_config_selects_korean():
    from sbmig.core.config import load_config

    root = Path(__file__).resolve().parents[2]
    c = load_config(root / "config" / "artifacts.example.yml", repo_root=root)
    assert c.report.language == "ko"
    from sbmig.core.config import ReportConfig
    assert ReportConfig().language == "en"


# ---------------------------------------------------------------- table drift
def _tr_literals(path: Path) -> set[str]:
    """Every string literal passed as the text argument of `tr(lang, "…")` in a module."""
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "tr"
                and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)):
            out.add(node.args[1].value)
    return out


def _used_keys() -> set[str]:
    used = _tr_literals(Path(markdown_mod.__file__))
    used |= set(markdown_mod.FLAG_HELP.values()) | set(markdown_mod.FLAG_PREFIX_HELP.values())
    used |= {markdown_mod.BLOCKED_TITLE, markdown_mod.HANDOVER_TITLE, markdown_mod.PROPERTIES_TITLE,
             markdown_mod.JAVAX_JARS_TITLE, markdown_mod.REVIEW_TITLE, markdown_mod.ERROR_LIST_TITLE,
             markdown_mod.FIXES_TITLE, markdown_mod.SEEN_FIX_TITLE, markdown_mod.BUNDLED_TITLE}
    used |= set(markdown_mod.SEEN_TITLES.values())                      # v2.1 error list group headings
    used |= {v for k, v in vars(handover_mod).items() if k.startswith("REASON_")}       # VersionChange.reason
    used |= set(handover_mod.PROPERTY_COLUMNS)
    used |= _tr_literals(Path(inventory_mod.__file__))                    # inventory.md (`sbmig inventory`)
    return used


def test_korean_table_matches_the_strings_the_modules_use():
    used = _used_keys()
    assert len(used) > 80
    missing = sorted(used - set(KO))
    stale = sorted(set(KO) - used)
    assert not missing, f"no Korean translation for: {missing}"
    assert not stale, f"Korean table keys no module uses: {stale}"


def test_korean_values_non_empty_and_same_placeholders():
    def fields(s: str) -> set[str]:
        return {f for _, f, _, _ in string.Formatter().parse(s) if f}

    for en, ko in KO.items():
        assert ko.strip(), en
        assert fields(en) == fields(ko), en
    assert tr("ko", "{n} item(s) — ", n=2) == "2건 — " and tr("en", "{n} item(s) — ", n=2) == "2 item(s) — "
    assert tr("ko", "not in the table") == "not in the table"


def _reactor_lines(root: Path) -> list[str]:
    """v2.1k lines from the real code: every `reactor_followups` sentence and the blocked suggestion."""
    from sbmig.analyze.aggregator import dedupe_reactor, reactor_duplicates, reactor_followups
    from sbmig.analyze.analyzer import PathView
    from sbmig.loop.blocked_registry import reactor_duplicate_suggestion

    def pom(aid: str, mods: tuple[str, ...] = (), profiles: str = "") -> str:
        m = "<modules>" + "".join(f"<module>{x}</module>" for x in mods) + "</modules>" if mods else ""
        return (f'<project xmlns="http://maven.apache.org/POM/4.0.0"><artifactId>{aid}</artifactId>'
                f"<packaging>pom</packaging>{m}{profiles}</project>")

    def prof(pid: str, mod: str, default: bool) -> str:
        act = "<activation><activeByDefault>true</activeByDefault></activation>" if default else ""
        return f"<profiles><profile><id>{pid}</id>{act}<modules><module>{mod}</module></modules></profile></profiles>"

    trees = {
        "company": {"pom.xml": pom("r", ("APP", "DEV")), "APP/pom.xml": pom("app", ("../DEV",)), "DEV/pom.xml": pom("d")},
        "nested": {"pom.xml": pom("r", ("A", "B")), "A/pom.xml": pom("a", ("../LIB",)),
                   "B/pom.xml": pom("b", ("../LIB",)), "LIB/pom.xml": pom("l")},
        "repeat": {"pom.xml": pom("r", ("APP",)), "APP/pom.xml": pom("app", ("./X", "./X")), "APP/X/pom.xml": pom("x")},
        "left": {"pom.xml": pom("r", ("DEV",), prof("all", "./DEV", True)), "DEV/pom.xml": pom("d")},
        "inactive": {"pom.xml": pom("r", ("APP", "DEV")), "APP/pom.xml": pom("app", profiles=prof("full", "../DEV", False)),
                     "DEV/pom.xml": pom("d")},
    }
    out: list[str] = []
    for name, files in trees.items():
        for rel, text in files.items():
            (root / name / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / name / rel).write_text(text, encoding="utf-8")
        ws = PathView(root / name)
        dups = reactor_duplicates(ws)
        dedupe_reactor(ws, duplicates=dups)
        out += reactor_followups(dups)
    assert len(out) == 5, out
    err = BuildError(kind=ErrorKind.UNKNOWN, message="Project 'g:a:1.0' is duplicated in the reactor @")
    out.append(reactor_duplicate_suggestion(BlockedItem(id="b", reason=BlockReason.REACTOR_DUPLICATE,
                                                        phase=Phase.VERIFY, cluster_id="c", errors=[err])))
    return out


def handover_lines(tmp_path, cfg) -> list[str]:
    """Real English lines from the code: one or more per `PATTERNS` entry (also used by the sample renderer)."""
    lines: list[str] = []
    report = _full_report(tmp_path, cfg)
    lines += report.handover.from_here + report.handover.merge_conflicts
    # carried-over summary + cap line
    prof = as_is_profile()
    from sbmig.core.models import GAV
    extra = [GAV(group_id="org.lib", artifact_id=f"lib{i}") for i in range(7)]
    prof.poms[0].dependencies += extra
    eff = {**EFFECTIVE, "managed": {**EFFECTIVE["managed"], **{g.key: f"1.{i}" for i, g in enumerate(extra)}}}
    view = final_tree(tmp_path / "cap")
    comp = view.root / "module/comp-jar/pom.xml"
    comp.write_text(comp.read_text().replace("</dependencies>", "".join(
        f"<dependency><groupId>org.lib</groupId><artifactId>lib{i}</artifactId><version>1.{i}</version>"
        "</dependency>" for i in range(7)) + "</dependencies>"))
    lines += build_handover(prof, view, make_report(manual_followups=[f"follow-up {i}" for i in range(40)]),
                            effective=eff, cfg=cfg).from_here
    # no placeholders written, LLM-rewritten files, missing effective pom, no profile
    fs = [f.model_copy(update={"llm_fixed_files": ["src/main/java/com/x/A.java"]}) if f.advisory_id == "hystrix"
          else f for f in make_report().advisories]
    rep2 = make_report(properties=inventory(placeholders=False), advisories=fs)
    h2 = build_handover(as_is_profile(), final_tree(tmp_path / "np"), rep2, effective=None, cfg=cfg)
    lines += h2.from_here + h2.notes
    lines += build_handover(None, None, make_report()).notes
    # X1b: javax API jars — the pointer (no orchestrator follow-up) and the follow-up itself (pointer suppressed)
    from sbmig.core.models import JavaxApiJar
    jars = [JavaxApiJar(artifact=GAV.parse("javax.persistence:javax.persistence-api:2.2"),
                        via=GAV.parse("org.qlrm:qlrm:2.1.1"))]
    lines += build_handover(as_is_profile(), final_tree(tmp_path / "jx"),
                            make_report(javax_api_jars=jars, manual_followups=[]), effective=EFFECTIVE,
                            cfg=cfg).from_here
    fu = ("1 javax API jar(s) still on the classpath after the migration (see the report section) — the libraries "
          "that drag them in need Jakarta builds: `org.qlrm:qlrm:2.1.1`")
    h3 = build_handover(as_is_profile(), final_tree(tmp_path / "jx2"),
                        make_report(javax_api_jars=jars, manual_followups=[fu]), effective=EFFECTIVE,
                        cfg=cfg).from_here
    assert not any("see that section" in x for x in h3)
    lines += h3
    # v2 structure review: script changed files / critical not fixed / warnings / the cap line
    from .test_review_report import review_report
    lines += handover_mod.review_followups(review_report(script="ok"))
    lines += handover_mod.review_followups(review_report(script="error", warnings=12))
    # v2.1d: the operator checks pointer (first "from here on" line)
    from sbmig.core.models import DependencyChange, OperatorCheck
    chk = OperatorCheck(change=DependencyChange(path="pom.xml", key="org.sitemesh:sitemesh", before="2.4.2",
                                                after="3.2.1", kind="major"))
    h4 = build_handover(as_is_profile(), final_tree(tmp_path / "oc"), make_report(operator_checks=[chk]),
                        effective=EFFECTIVE, cfg=cfg).from_here
    assert h4[0] == handover_mod.operator_checks_item(1)
    lines += h4
    # v2.1h: JSPs left with javax references — the rename on (files it could not rewrite) and off
    jsp_prof = as_is_profile()
    jsp_prof.source.jsp_javax_files = 2
    off = cfg.model_copy(deep=True)
    off.layout.jsp_jakarta_rename = False
    for c in (cfg, off):
        lines += build_handover(as_is_profile(), final_tree(tmp_path / f"jsp{len(lines)}"),
                                make_report(profile=jsp_prof), effective=EFFECTIVE, cfg=c).from_here

    # v2.1k: reactor duplicate follow-ups (orchestrator → manual_followups) and the reactor_duplicate suggestion
    lines += _reactor_lines(tmp_path / "rx")
    # v2.1r: the file_lock suggestion
    from sbmig.loop.blocked_registry import FILE_LOCK_SUGGESTION, compose_suggestion
    lines.append(FILE_LOCK_SUGGESTION)
    # v2.1y: the needs_owner suggestions — a sibling of a declined case, and the fallback without the LLM's sentence
    from sbmig.core.models import BlockedItem, BlockReason, Phase
    from sbmig.loop.fixloop import NEEDS_OWNER_KO
    lines.append(f"same cause as 70cf5d17c7: iBATIS 2 has no Spring 7 support — {NEEDS_OWNER_KO}")
    lines.append(compose_suggestion(BlockedItem(id="b", reason=BlockReason.NEEDS_OWNER, phase=Phase.VERIFY,
                                                cluster_id="c")))

    # v2.1z-e: the jakarta build search record / the unmatched owner class on a blocked item (fixloop._jakarta_evidence)
    from sbmig.build.jakarta_discovery import JakartaBuild, JakartaDiscovery
    from sbmig.loop.fixloop import JAKARTA_SEARCH_PREFIX, UNMAPPED_PREFIX
    jd = JakartaDiscovery(key="com.example.grid8:grid8", current="1.0.6",
                          searched=["same coordinates: 3 release(s) newer than 1.0.6 checked, none jakarta",
                                    "Nexus search name=*grid*: no other artifact with that stem"],
                          probed=["grid8-jakarta", "com.example.grid9:grid9"])
    lines.append(f"{JAKARTA_SEARCH_PREFIX} {jd.key}: {jd.searched_text()} — none verified")
    jd.builds.append(JakartaBuild("com.example.grid8", "grid8", "2.0.5-jakarta", source="same", verified=True,
                                  how="version qualifier", major_change=True))
    lines.append(f"{JAKARTA_SEARCH_PREFIX} {jd.key}: {jd.searched_text()} — verified build(s): "
                 + ", ".join(b.coordinates for b in jd.verified))
    lines.append(f"{JAKARTA_SEARCH_PREFIX} {jd.key}: no repository lookup (libraries.lookup off or unreachable)")
    lines.append(f"{UNMAPPED_PREFIX} com.vendor.grid.GridExporter could not be matched to a dependency (classpath, package "
                 f"tokens, local jars) — run mvn dependency:tree in the module and look the library's jakarta build up in Nexus")
    # v2.1z-c: the hollow-reactor first line (with / without unresolved ${…} directories) and the soft line
    from sbmig.analyze.hollow import HollowResult, hollow_followup, outside_followup
    hr = HollowResult(outside={"java": 120, "jsp": 35, "web": 25},
                      outside_by_dir={"DEV": {"java": 120, "jsp": 35}, "STATIC": {"web": 25}}, war=True)
    lines.append(hollow_followup(hr))
    hr.unresolved = ["${DIR_xxxWEB}", "${DIR_xxxJAR}"]
    lines.append(hollow_followup(hr))
    lines.append(outside_followup(HollowResult(inside={"java": 1, "jsp": 0, "web": 0},
                                               outside={"java": 3, "jsp": 0, "web": 0},
                                               outside_by_dir={"old": {"java": 3}})))

    # v2.1z-d: the ADF first line under both policies and the traces note
    from sbmig.analyze.adf import AdfInventory, adf_followup, traces_followup
    adf_inv = AdfInventory(entities=12, view_objects=20, app_modules=2, pages=34, task_flows=5, java_total=1546, java_adf=900,
                           tables=[f"T{i}" for i in range(11)])
    adf_inv.families = {"bc4j": ["m/Emp.xml"], "java": ["m/EmpImpl.java"], "model": [], "faces": []}
    adf = adf_inv.summary()
    lines.append(adf_followup(adf))
    lines.append(adf_followup(adf, "continue"))
    adf_inv.families = {"bc4j": [], "java": [], "model": [], "faces": ["p/main.jspx"]}
    lines.append(traces_followup(adf_inv.summary()))

    # v2.1z-h: the stray pom copy follow-up, inside the module's directory and beside it (orchestrator.stray_pom_followup)
    from sbmig.analyze.pom_parser import StrayPom
    from sbmig.loop.orchestrator import stray_pom_followup
    lines.append(stray_pom_followup(StrayPom("boot/bin/pom.xml", "boot/pom.xml", "com.x:xxx-boot", True)))
    lines.append(stray_pom_followup(StrayPom("backup/boot/pom.xml", "boot/pom.xml", "com.x:xxx-boot", False)))

    # v2.1z-i: the bundled-jar wiring lines (system placeholder / system with pom.properties coordinates / compile, with
    # the loop's ⚠) and the webapp-move follow-up for files left under WEB-INF (jars + classes, jars only, no jars)
    from sbmig.core.models import JarCoordinate
    from sbmig.restructure.system_jars import WiredJar, bundled_jar_followup, webinf_left_followup
    seed = JarCoordinate(path="src/main/webapp/WEB-INF/lib/acme-seed-1.0.jar", provides=["com.acme.seed"])
    lines.append("⚠ " + bundled_jar_followup(WiredJar(jar=seed, module_pom="shop-back/pom.xml", group_id="com.example",
                                                      artifact_id="acme-seed", version="1.0", scope="system",
                                                      placeholder=True)))
    real = seed.model_copy(update={"group_id": "com.acme", "artifact_id": "seed", "version": "2.3",
                                   "source": "pom_properties"})
    lines.append(bundled_jar_followup(WiredJar(jar=real, module_pom="shop-back/pom.xml", group_id="com.acme",
                                               artifact_id="seed", version="2.3", scope="system")))
    lines.append(bundled_jar_followup(WiredJar(jar=real, module_pom="shop-back/pom.xml", group_id="com.acme",
                                               artifact_id="seed", version="2.3", scope="compile")))
    lines.append(webinf_left_followup(["w/WEB-INF/lib/a.jar", "w/WEB-INF/classes/A.class"], "shop-back/src/main/webapp"))
    lines.append(webinf_left_followup(["w/WEB-INF/lib/a.jar"], "shop-back/src/main/webapp"))
    lines.append(webinf_left_followup(["w/META-INF/x.txt"], "shop-back/src/main/webapp"))
    # v2.1z-j: the unused bundled jar line (restructure)
    from sbmig.restructure.bundled_jars import unused_jar_followup
    lines.append(unused_jar_followup("unused-tool-1.0.jar"))

    # v2.1z-ag: the as-is parent lines (transform alignment, fix-loop alignment, rewrite without recipes)
    from sbmig.core.models import GAV as _GAV
    from sbmig.restructure.asis_parent import ParentMiss, aligned_followup, loop_aligned_followup, skipped_followup
    pm = ParentMiss(gav=_GAV.parse("com.c:base-parent:1.0.0-SNAPSHOT"), kind="asis-version",
                    target=_GAV.parse("com.c:base-parent:4.0.0"), spec_name="artifacts.parent")
    lines.append(aligned_followup(pm, ["pom.xml", "a/pom.xml"]))
    lines.append(loop_aligned_followup(pm, ["pom.xml"]))
    lines.append(skipped_followup(ParentMiss(gav=_GAV.parse("com.acme:acme-parent:9.9"), kind="foreign")))
    # v2.1z-ah: the Maven HTTP blocker line (one repository / several)
    from sbmig.build.env_errors import http_blocked_followup, http_blocker_hit

    from ..build.test_http_blocker import DEP_LOG, PARENT_LOG
    lines += [http_blocked_followup(http_blocker_hit(t), cfg) for t in (DEP_LOG, PARENT_LOG)]

    # v2.1z-aj: the Ant refusal's detail line (full flow / remediation flow)
    from sbmig.loop.orchestrator import ant_refusal_detail
    lines += [ant_refusal_detail("build.xml"), ant_refusal_detail("app/build.xml", "remediation")]
    # v2.1z-aj: the abort reasons of intake / the remediation flow's start (tests/report/test_abort_reasons_ko.py)
    from .test_abort_reasons_ko import CASES as ABORT_CASES
    lines += [reason for reason, _ in ABORT_CASES]

    # v2.1z-ak: the framework project's blocked suggestions (tests/report/test_framework_scope.py)
    from .test_framework_scope import SUGGESTIONS as FW_SUGGESTIONS
    lines += [en for en, _ in FW_SUGGESTIONS]
    from .test_framework_scope import KO_SAMPLES as FW_KO_SAMPLES
    lines += [en for en, _ in FW_KO_SAMPLES]
    lines += _suggestion_lines(cfg)
    lines += _more_abort_lines(tmp_path / "abort")
    lines += _loop_followup_lines()
    return lines


def _suggestion_lines(cfg) -> list[str]:
    """The blocked-table suggestions of `compose_suggestion` (every reason, the javax ladder) from the real code."""
    from sbmig.core.models import GAV, JavaxApiJar
    from sbmig.loop.blocked_registry import compose_suggestion

    def item(reason, **kw):
        return BlockedItem(id="b", reason=reason, phase=Phase.VERIFY, cluster_id="c", **kw)

    lib, api = GAV.parse("com.vendor:grid:1.0.6"), "javax.servlet.ServletRequest"
    out = [compose_suggestion(item(BlockReason.EXTERNAL_JAVAX_API, artifact=lib, owner_class="com.vendor.Grid",
                                   required_type=api), cfg),
           compose_suggestion(item(BlockReason.EXTERNAL_API_INCOMPATIBLE, artifact=lib), cfg),
           compose_suggestion(item(BlockReason.EXTERNAL_JAVAX_API, artifact=GAV.parse("javax.servlet:servlet-api:2.5"),
                                   required_type=api), cfg),
           compose_suggestion(item(BlockReason.EXTERNAL_JAVAX_API, owner_class="com.vendor.Grid", required_type=api), cfg),
           compose_suggestion(item(BlockReason.EXTERNAL_JAVAX_API, found_type="jakarta.servlet.ServletRequest"), cfg),
           compose_suggestion(item(BlockReason.ARTIFACT_NOT_FOUND, artifact=GAV.parse("com.example:gone:2.1")), cfg),
           compose_suggestion(item(BlockReason.ARTIFACT_NOT_FOUND, artifact=GAV.parse("com.example:gone:2.1")), cfg,
                              repositories=["https://repo.example.com/maven2"]),
           compose_suggestion(item(BlockReason.PARENT_NOT_FOUND, artifact=GAV.parse("com.example:base-parent:4.0.0")), cfg,
                              settings_file="/data/.m2/settings.xml"),
           compose_suggestion(item(BlockReason.PLUGIN_INCOMPATIBLE), cfg, extra="maven-antrun-plugin"),
           compose_suggestion(item(BlockReason.LLM_UNAVAILABLE), cfg, extra="HTTP 503"),
           compose_suggestion(item(BlockReason.REPEATED_FAILURE, attempts=20), cfg)]
    out += [compose_suggestion(item(r), cfg) for r in (BlockReason.UNSUPPORTED_SOURCE, BlockReason.ENCODING,
                                                       BlockReason.BUDGET_EXHAUSTED, BlockReason.OSCILLATION)]
    jar = JavaxApiJar(artifact=GAV.parse("javax.persistence:javax.persistence-api:2.2"), module="shop-back")
    for via in ("org.qlrm:qlrm:2.1.1", "ch.simas.qlrm:qlrm:1.7.1", "org.vendor:mailer:2.0"):
        out.append(compose_suggestion(item(BlockReason.EXTERNAL_JAVAX_API, via_direct=GAV.parse(via)), cfg, javax_jar=jar))
    c = cfg.model_copy(deep=True)
    c.artifacts.optional = [*c.artifacts.optional, c.artifacts.optional[0].model_copy(
        update={"group_id": "com.example.inhouse", "artifact_id": "legacy-dao", "version": "4.0.0"})] \
        if c.artifacts.optional else c.artifacts.optional
    if c.artifacts.optional:
        out.append(compose_suggestion(item(BlockReason.EXTERNAL_JAVAX_API,
                                           via_direct=GAV.parse("com.example.inhouse:legacy-dao:1.4.0")), c, javax_jar=jar))
        out.append(compose_suggestion(item(BlockReason.EXTERNAL_JAVAX_API,
                                           artifact=GAV.parse("com.example.inhouse:legacy-dao:1.4.0")), c))
    out.append(compose_suggestion(item(BlockReason.EXTERNAL_JAVAX_API),
                                  cfg, javax_jar=jar.model_copy(update={"declared": True})))
    out += ["needs a human",
            "iBATIS 2 has no Spring 7 support; the owning team must choose MyBatis or plain JDBC — 소유팀 결정 필요",
            "a person pinned case 1a2b3c4d5e to `block`: handle it by hand (see `sbmig kb cases show 1a2b3c4d5e`)",
            ("build extension `org.apache.maven.wagon:wagon-webdav-jackrabbit:3.5.3` declared by the company parent "
             "`com.example:base-parent:4.0.0` cannot be downloaded: make the repository serve its pom, jar, parent poms and "
             "dependencies, or drop the extension from the parent (a deploy transport is not needed for packaging); nothing "
             "in the project can change an inherited extension"),
            "EOL: Netflix Hystrix is discontinued — replacement: org.springframework.cloud:spring-cloud-starter-circuitbreaker-resilience4j"]
    return out


def _more_abort_lines(root: Path) -> list[str]:
    """Abort reasons of the analyze step, the parent checks and the HTTP blocker (beyond tests/report/test_abort_reasons_ko)."""
    from sbmig.analyze.adf import adf_abort_reason
    from sbmig.analyze.aggregator import no_root_pom_reason
    from sbmig.analyze.analyzer import PathView
    from sbmig.build.env_errors import http_blocked_reason, http_blocker_hit

    from ..build.test_http_blocker import DEP_LOG
    for rel, aid in (("APP/pom.xml", "app"), ("BATCH/pom.xml", "batch")):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(f'<project xmlns="http://maven.apache.org/POM/4.0.0"><groupId>g</groupId>'
                                f"<artifactId>{aid}</artifactId><version>1</version></project>", encoding="utf-8")
    hit = http_blocker_hit(DEP_LOG)
    return [no_root_pom_reason(PathView(root), True), no_root_pom_reason(PathView(root), False),
            adf_abort_reason("stop"), "build tool 'gradle' is not supported (Maven only)", "no pom.xml found in the project",
            http_blocked_reason(hit), f"{http_blocked_reason(hit)} — run aborted: fix the Maven settings (see the Handover)",
            ("Maven cannot resolve the parent com.example:base-parent:4.0.0, which is the configured version "
             "(artifacts.parent = com.example:base-parent:4.0.0) — the repository does not serve it (settings: default); fix "
             "the repository / settings, or the version in artifacts.parent, and rerun"),
            ("Maven cannot resolve the as-is parent com.example:base-parent:1.0.0-SNAPSHOT; the configured version is "
             "com.example:base-parent:4.0.0 (artifacts.parent) — the run stops because openrewrite.unresolvable_asis_parent "
             "is abort (settings: default); set it to align to use the configured version instead"),
            ("the company parent com.example:base-parent:4.0.0 declares the build extension "
             "org.apache.maven.wagon:wagon-webdav-jackrabbit:3.5.3, which Maven cannot download from the repository "
             "(settings: default) — every Maven call fails before any goal; make Nexus serve its pom, jar, parent poms and "
             "dependencies (or drop the extension from the parent: a deploy transport is not needed for packaging) and rerun")]


def _loop_followup_lines() -> list[str]:
    """Follow-ups of the fix loop / orchestrator / resources filtering, review notes and the default price note, written
    the way the code writes them (f-strings of sbmig/loop/fixloop.py, orchestrator.py, restructure/resources_filtering.py,
    review/reviewer.py and config/default.yml `llm.pricing_note`)."""
    return [
        ("bundled jar `vendor-crypto.jar` provides package `com.vendor.crypto` needed by `shop-back/pom.xml` but is not in "
         "the repository and intake.unknown_jar is skip — declare it as a Maven dependency (publish it to Nexus first)"),
        ("the LLM is not configured — no LLM fix script could be written in this run; the cases that needed one are "
         "blocked (llm_unavailable)"),
        ("the LLM was unavailable during this run (HTTP 503 twice); cases that needed it are listed as blocked "
         "(llm_unavailable) — proven stored fix scripts were still reused"),
        ("the LLM gateway rejected 3 different prompts with the same HTTP 400 (unknown field (x)) — a configuration error "
         "(model name / payload fields in .env or llm.*), not the prompts; LLM calls stopped for this run, fix the setting "
         "and rerun"),
        "test sources do not compile (build passes only with -Dmaven.test.skip=true): src/test/java/A.java:3 cannot find symbol",
        ("test fails at runtime (packaged with -DskipTests): OrderServiceTest — Connection refused — likely an externalized "
         "setting (see Handover 'Properties to set') or infrastructure not reachable at build time"),
        ("sibling poms without a root pom: the aggregator pom could not be written (OSError: read-only) — add a root pom.xml "
         "listing APP, BATCH"),
        ("no aggregator pom: sbmig generated pom.xml (shop-aggregator, modules APP, BATCH) — sibling poms depend on each "
         "other; the as-is build of each module stays as it was"),
        ("no aggregator pom: sbmig generated pom.xml (shop-aggregator, modules APP) — one aggregator below the root; the "
         "as-is build of each module stays as it was; not linked to them and left out: OLD/pom.xml"),
        "'mvn' is not runnable (exit 127): install Maven or set build.mvn; every build below will fail",
        "artifacts.parent is not configured: pass the company artifacts yml with --config",
        ("optional artifact bump step restructure.optional failed: KeyError: 'x' — bump the in-house optional artifacts "
         "by hand"),
        ("restructure step restructure.apply (layout) failed: OSError: busy — the tree keeps the as-is layout / parents; "
         "the final build ran on it, so BUILD SUCCESS here does not mean the migration is complete"),
        ("binary file without an extension under a filtered resource dir: src/main/resources/data/seed — exclude it from "
         "filtering (a second <resource> with <filtering>false</filtering>) or rename it"),
        ("… and 4 more binary file(s) without an extension under filtered resource dirs (see the structure review "
         "facts)"),
        "review.enabled is off", "no LLM client for this run (dry run or LLM off)", "no pom.xml in the workspace",
        "review error: LLMError: 503",
        ("OpenAI list prices as of 2026-10-22 (developers.openai.com model pages); the company gateway may bill "
         "differently"),
        # the whole-code review of 2026-11-18: a stop that made the fix loop leave, the framework check that could not
        # look, a repository that did not answer, and a ZIP over an unpacking limit
        "stopped by user (stop.request) — the fix loop was left early; the last build shows the tree as it was left",
        ("framework check could not run (the parent pom and the dependency tree could not be read): if this project uses "
         "the in-house framework it must not be migrated in full — confirm before using the result"),
        ("`com.example:lib:1.0` could not be downloaded: the repository did not answer (https://nexus.invalid/repo) "
         "(timeout, refused connection or server error) — not a project problem: check the repository and the network, "
         "then rerun"),
        ("bundled jar `vendor-sso.jar` provides package `com.vendor.sso` needed by `shop-back/pom.xml`, but the repository "
         "did not answer whether it has `com.vendor:vendor-sso:2.1` (timeout) — not wired and not called missing; not a "
         "project problem: check the repository and the network, then rerun"),
        ("the project's .mvn/maven.config points Maven at its own settings (-gs .mvn/settings.nexus.xml): ignored here — "
         "every build of this run used the server's settings"),
        "ZIP refused: it unpacks to 5,120 MiB, over the limit of 4,096 MiB (intake.max_unpacked_mb) — nothing was unpacked",
        ("ZIP refused: the entry app/big.bin unpacks to 2,048 MiB, over the limit of 1,024 MiB (intake.max_entry_mb) — "
         "nothing was unpacked"),
        ("ZIP refused: the entry app/big.bin is compressed 1026:1, over the limit of 200:1 (intake.max_compression_ratio) "
         "— nothing was unpacked"),
        ("ZIP refused while unpacking: the entry app/big.bin wrote more than 1,024 MiB (intake.max_entry_mb) — the ZIP's "
         "directory understates its sizes"),
        ("ZIP refused while unpacking: more than 4,096 MiB written (intake.max_unpacked_mb) — the ZIP's directory "
         "understates its sizes"),
        ("ZIP refused while unpacking: the entry app/big.bin expanded more than 200:1 (intake.max_compression_ratio) — the "
         "ZIP's directory understates its sizes"),
    ]


def test_every_handover_pattern_matches_a_real_handover_line(tmp_path, cfg):
    """Lines produced by handover.py → each `PATTERNS` entry is exercised and every templated line is translated."""
    lines = handover_lines(tmp_path, cfg)
    translated = {x: tr_line("ko", x) for x in lines}
    for rx, _ in PATTERNS["ko"]:
        assert any(rx.match(x) for x in lines), f"pattern never matched a handover line: {rx.pattern}"
    untranslated = [x for x, k in translated.items() if k == x]
    allowed = ("web.xml not migrated", "follow-up ", "Hystrix is discontinued", "Springfox is discontinued",
               "final poms: ")
    assert all(x.startswith(allowed) for x in untranslated), untranslated
    assert all(tr_line("en", x) == x for x in lines)


# ---------------------------------------------------------------- wording of the Korean side (v2.1z-as)
# A sentence ends in 합니다체; the plain 한다체, "~할 것" and "~함 / ~됨" endings are the machine-translated tells the
# company reader named. Table headers and short labels stay noun phrases (not checked by these patterns).
PLAIN_ENDING = re.compile(r"(?<![니마보])다(?=$|[.,;:)\]」—]|\s+—)")     # 마다 / 보다 are not endings
WILL_DO = re.compile(r"[가-힣] 것(?=$|[\s.,;:)\]」—])")
NOMINAL_ENDING = re.compile(r"[가-힣](?<!포)(함|됨)(?=$|[\s.,;:)\]」—])")
BANNED = ("사유", "관리됨", "as-is 버전으로 고정", "케이스", "시도", "라운드", "인수인계", "차단", "후속 조치",
          "다스리", "닿", "들어오", "들어옵", "들어온", "들어옴", " 길", "자리")
ALLOWED = ("차단 미러",)                  # Maven's own term (maven-default-http-blocker), not the blocked-item concept
GUIDE_SECTIONS = ("전환 내용", "확인해 주세요", "코드 수정 내역", "해결하지 못한 오류", "진행하지 않은 항목", "참고",
                  "운영자 확인 필요", "Ant 프로젝트의 pom.xml 생성", "ADF 규모", "프로젝트에 포함된 jar",
                  "클래스패스에 남은 javax API jar", "실행 전에 설정할 프로퍼티", "사내 프레임워크 프로젝트",
                  "LLM 사용량과 비용")
# named groups whose value a rule rewrites on purpose (a location phrase, a fixed English list, a path joined with ">")
REWRITTEN_GROUPS = {"w", "pom", "r", "why", "abort", "jk", "tail", "path"}
# the pom path of a source-encoding correction: the line is printed after "`<pom>`:" already
LEFT_OUT = {("^source encoding written: ", "p")}


def _wording_problems(text: str) -> list[str]:
    plain = text
    for ok in ALLOWED:
        plain = plain.replace(ok, "")
    out = [f"banned word {w!r}" for w in BANNED if w in plain]
    for rx, what in ((PLAIN_ENDING, "plain style"), (WILL_DO, "'~할 것'"), (NOMINAL_ENDING, "'~함 / ~됨' ending")):
        m = rx.search(plain)
        if m:
            out.append(f"{what} at …{plain[max(0, m.start() - 12):m.end() + 2]}…")
    out += [f"unknown section 「{s}」" for s in re.findall(r"「([^」]+)」", text) if s not in GUIDE_SECTIONS]
    return out


def _korean_rule_outputs(tmp_path, cfg) -> dict[str, str]:
    return {x: tr_line("ko", x) for x in handover_lines(tmp_path, cfg)}


def test_korean_values_are_polite_sentences_without_code_vocabulary():
    bad = {en: p for en, ko in KO.items() if (p := _wording_problems(ko))}
    assert not bad, bad


def test_korean_rule_outputs_are_polite_sentences_without_code_vocabulary(tmp_path, cfg):
    bad = {en: p for en, ko in _korean_rule_outputs(tmp_path, cfg).items()
           if ko != en and (p := _wording_problems(ko))}
    assert not bad, bad


def test_every_rule_keeps_its_captured_values(tmp_path, cfg):
    """Identifiers, coordinates, paths, keys and commands a rule captures reach the Korean sentence unchanged."""
    seen: set[str] = set()
    lost: list[tuple[str, str, str]] = []
    for line, ko in _korean_rule_outputs(tmp_path, cfg).items():
        rx = next((rx for rx, _ in PATTERNS["ko"] if rx.match(line)), None)
        if rx is None:
            continue
        seen.add(rx.pattern)
        for name, value in rx.match(line).groupdict().items():
            if value and value.strip() and name not in REWRITTEN_GROUPS \
                    and not any(rx.pattern.startswith(p) and name == g for p, g in LEFT_OUT):
                if value.strip() not in ko:
                    lost.append((rx.pattern[:60], name, value))
        if rx.match(line).groupdict().get("path"):
            for hop in rx.match(line).group("path").split("→"):
                assert hop.strip() in ko, (line, hop, ko)
    assert not lost, lost
    assert len(seen) == len(PATTERNS["ko"])


def test_rule_keeps_the_location_and_repositories_it_rewrites():
    via = ("`javax.persistence:javax.persistence-api:2.2` comes through `org.vendor:mailer:2.0` (direct dependency of "
           "module `shop-back`), which is still compiled against javax: upgrade it to a Jakarta release (check its "
           "changelog) or replace it; left unchanged on purpose")
    assert "모듈 `shop-back`의 직접 의존성" in tr_line("ko", via)
    nf = ("`com.example:gone:2.1` is not in any repository: proxy it in Nexus or fix the coordinates (checked "
          "repositories: https://repo.example.com/maven2); the LLM is never asked to invent versions")
    assert "(확인한 저장소: https://repo.example.com/maven2)" in tr_line("ko", nf)


def test_price_note_and_review_note_are_translated_through_the_line_rules():
    note = ("OpenAI list prices as of 2026-10-22 (developers.openai.com model pages); the company gateway may bill "
            "differently")
    assert tr("ko", "Prices: {note}.", note=note).startswith("단가 기준: OpenAI 공개 가격(2026-10-22 기준")
    assert tr("en", "Prices: {note}.", note=note) == f"Prices: {note}."
    assert tr("ko", "skipped ({note})", note="review.enabled is off") == "실행하지 않음 (review.enabled 설정이 꺼져 있습니다)"
    assert tr("ko", "skipped ({note})", note="a free note") == "실행하지 않음 (a free note)"
