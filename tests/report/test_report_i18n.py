"""report.language (en | ko): Korean report.md, English unchanged, report.json language-independent, table drift."""
from __future__ import annotations

import ast
import json
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
EN_HEADINGS = ("# Migration report — ", "## Handover — what we replaced, what is yours", "## Unresolved errors",
               "## Manual follow-ups", "## Flags", "## What we changed", "## Phases", "## Knowledge base",
               "### Versions replaced", "### Libraries needing your attention", "### From here on, your team",
               "### Properties to set before running", "| Run id |", "**Merge conflicts**")
KO_HEADINGS = ("# 마이그레이션 보고서 — ", "## 인수인계 — 교체한 것과 이제 팀이 맡을 것", "## 미해결 오류",
               "## 수동 후속 조치", "## 플래그", "## 변경 내역", "## 단계별 결과", "## 지식 베이스",
               "### 교체된 버전", "### 조치가 필요한 라이브러리", "### 이후 담당 팀이 할 일",
               "### 실행 전에 설정할 프로퍼티", "| 실행 ID |", "**병합 충돌**")


def _full_report(tmp_path, cfg):
    """Handover with versions, advisories, merge conflicts, properties, blocked, unresolved and flags."""
    blocked = BlockedItem(id="b1", reason=BlockReason.EXTERNAL_JAVAX_API, phase=Phase.SPRING, cluster_id="c")
    report = make_report(blocked=[blocked], flags=["test-compile-skipped", "managed-versions-carried-over"],
                         unresolved=[BuildError(kind=ErrorKind.COMPILE, message="cannot find symbol",
                                                file="src/A.java", line=3, symbol="class Foo")],
                         merge_conflicts=[DIFFERING, IDENTICAL], properties=inventory(), success=False)
    report.handover = build_handover(as_is_profile(), final_tree(tmp_path), report, effective=EFFECTIVE, cfg=cfg)
    return report


def test_korean_headings_replace_english(tmp_path, cfg):
    report = _full_report(tmp_path, cfg)
    ko = render_markdown(report, language="ko")
    en = render_markdown(report)
    for e, k in zip(EN_HEADINGS, KO_HEADINGS):
        assert e in en, e
        assert k in ko and e not in ko, (e, k)
    assert "❌ 빌드 실패" in ko and "NOT BUILDING" not in ko
    assert "| 항목 | 이전 | 이후 | 사유 |" in ko and "as-is 버전 유지 (더 이상 관리되지 않음)" in ko
    assert "| 키 | 마이그레이션 전 공급처 | 참조 위치 | 기록한 값 | 설정 방법 |" in ko
    # Handover data lines (English in report.json) are rendered in Korean …
    assert "클러스터 1개 차단됨 — '차단됨' 참조" in ko and "1 cluster(s) blocked" not in ko
    assert "org.apache.commons:commons-lang3: 3.12.0(으)로 고정함" in ko
    assert "동일한 중복 1개 제외" in ko and "kept the copy from" not in ko
    # … identifiers, free-text follow-ups and compiler messages stay English
    assert "`test-compile-skipped`: 메인 코드는 빌드되지만" in ko
    assert "web.xml not migrated" in ko and "cannot find symbol — 심볼: `class Foo`" in ko


def test_blocked_not_building_in_korean():
    r = sample_report()
    r.blocked = blocked_items()
    ko = render_markdown(r, language="ko")
    assert "## 차단됨 — 사람 / 라이브러리 조치 필요" in ko and "## Blocked" not in ko
    assert "| **차단됨** | 3건 — external_javax_api 1, artifact_not_found 1, repeated_failure 1 (아래 참조) |" in ko
    assert "| 사유 | 아티팩트 / 소유 클래스 | 필요 → 실제 | 파일 | 시도 | 제안 |" in ko
    assert "(클래스 `com.company.legacy.web.LegacyRequestHelper`)" in ko
    assert "<details><summary><code>blk-1</code> — 오류 1건</summary>" in ko
    assert "마지막 빌드 후 남은 오류 5건 (차단된 클러스터 제외, 파일별):" in ko
    assert "### `모듈 core`" in ko and "### `(파일 없음)`" in ko
    assert "| 시작 / 종료 |" in ko and "(2분 05초)" in ko and "핸들러 `lombok-version-floor`" in ko
    assert "## 에스컬레이션된 클러스터" in ko and "- 계획: plan note" in ko and "- spring: spring note" in ko
    assert "Blocked —" not in ko and "item(s)" not in ko and "error(s)" not in ko


def test_success_in_korean():
    ok = render_markdown(sample_report(success=True).model_copy(update={"harvest": None, "timings_s": {}}),
                         language="ko")
    assert "✅ 빌드 성공" in ok and "없음. 최종 빌드가 통과했다." in ok and "수집한 항목 없음." in ok
    assert "BUILD SUCCESS" not in ok and "이번 실행에서 기록된 버전 변경과 라이브러리 어드바이저리가 없다." in ok


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
             markdown_mod.JAVAX_JARS_TITLE}
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


def test_every_handover_pattern_matches_a_real_handover_line(tmp_path, cfg):
    """Lines produced by handover.py → each `PATTERNS` entry is exercised and every templated line is translated."""
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

    translated = {x: tr_line("ko", x) for x in lines}
    for rx, _ in PATTERNS["ko"]:
        assert any(rx.match(x) for x in lines), f"pattern never matched a handover line: {rx.pattern}"
    untranslated = [x for x, k in translated.items() if k == x]
    allowed = ("web.xml not migrated", "follow-up ", "Hystrix is discontinued", "Springfox is discontinued",
               "final poms: ")
    assert all(x.startswith(allowed) for x in untranslated), untranslated
    assert all(tr_line("en", x) == x for x in lines)
