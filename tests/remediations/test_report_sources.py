"""report.md says where a library comes from (들어온 경로), the case-insensitive IVAS spelling, the retitled
`not_inheriting_root` group and the per-module in-house split (sbmig/remediations/report.py, i18n.py)."""
from __future__ import annotations

import re

from sbmig.remediations import report
from sbmig.remediations.models import (
    Finding,
    ModuleSource,
    PomLocation,
    Residual,
    ScanResult,
    write_json_atomic,
)

from .test_report import BANNED_WORDS, COMP, CORE, _company, _detached, _section

LIB_A, LIB_B = "com.example:lib-a:1.0", "com.example:lib-b:2.0"
PARENT = "org.example:parent:1.2.3"


def _by_ga(state, ga):
    return [r for r in state.residuals if r.ga == ga]


def _with_sources(tmp_path):
    """The company state with module sources on every residual: the three source kinds and a variant in both
    sections, one row whose modules share a source, one row whose modules differ."""
    state, paths = _company(tmp_path)
    m0, = _by_ga(state, "org.example:m0")
    m0.sources = [ModuleSource(module=m, how="transitive", via=[LIB_A, LIB_B]) for m in ("app-a", "app-b")]
    m2, = _by_ga(state, "org.example:m2")
    m2.sources = [
        ModuleSource(module="app-a", how="direct_literal",
                     declared=PomLocation(pom="back/pom.xml", line=123, text="1.0")),
        ModuleSource(module="app-b", how="transitive", via=[LIB_A]),
        ModuleSource(module="app-c", how="transitive", via=[LIB_A]),
        ModuleSource(module="app-d", how="parent_managed", manager=PARENT,
                     declared=PomLocation(pom="app-d/pom.xml", line=40, text="")),
        ModuleSource(module="app-e", how="direct_property",
                     declared=PomLocation(pom="pom.xml", line=12, text="1.0", property="m2.version", element="property")),
    ]
    netty, = _by_ga(state, "org.example:netty")
    netty.sources = [ModuleSource(module="app-a", how="transitive", via=[LIB_A], variant="jar/jakarta")]
    json_, = _by_ga(state, "org.example:json")
    json_.sources = [ModuleSource(module="app-domain", how="transitive", via=[f"{CORE}:4.0"], inhouse=[CORE])]
    web, = _by_ga(state, "org.example:spring-web")
    web.sources = [ModuleSource(module="app-store", how="parent_managed", manager=f"{COMP}:4.0", inhouse=[COMP])]
    yaml, = _by_ga(state, "org.example:yaml")
    yaml.sources = [ModuleSource(module="app-domain", how="transitive", via=[f"{CORE}:4.0"], variant="test-jar/tests",
                                 inhouse=[CORE])]
    return state, paths


def test_manual_rows_say_how_each_module_gets_the_library(tmp_path):
    state, paths = _with_sources(tmp_path)
    md = report.build_report_md(state, paths)
    people = _section(md, "직접 확인이 필요한 취약점")
    group = people.split("### 모듈 pom에 직접 지정해야 하는 버전", 1)[1].split("### ", 1)[0]
    # the list comes after the table, one entry per row
    assert group.index("| 위험도 |") < group.index("들어온 경로:")
    # shared by every module of the row: printed once, without module names
    shared = f"- `org.example:m0` 1.0: `{LIB_A}` > `{LIB_B}` 경로로 가져옴"
    assert shared in group and group.count(f"`{LIB_A}` > `{LIB_B}`") == 1
    # differing: one sub-item per group of modules, modules with the same source together
    assert "- `org.example:m2` 1.0\n" in group
    assert "  - `app-a` 모듈: `back/pom.xml` 123행에 직접 선언" in group                       # direct declaration
    assert f"  - `app-b`, `app-c` 모듈: `{LIB_A}` 경로로 가져옴" in group                     # transitive, shared by two
    assert f"  - `app-d` 모듈: `app-d/pom.xml` 40행에 선언, `{PARENT}`에서 버전 지정" in group  # managed version
    assert "  - `app-e` 모듈: `pom.xml` 12행에 직접 선언, 버전 property `m2.version`" in group
    # a row without sources (m1) has no entry
    assert "`org.example:m1` 1.0" not in group
    variant = people.split("### classifier가 붙은 의존성", 1)[1]
    assert f"- `org.example:netty` 4.1: `{LIB_A}` 경로로 가져옴, classifier `jakarta`" in variant
    assert "classifier는 같은 버전의 jar를 용도별로 구분하는 이름입니다(예: jakarta)." in variant


def test_inhouse_rows_say_how_each_module_gets_the_library(tmp_path):
    state, paths = _with_sources(tmp_path)
    sec = _section(report.build_report_md(state, paths), "사내 라이브러리에서 발생한 취약점")
    core = sec.split(f"### `{CORE}`", 1)[1].split("### ", 1)[0]
    assert "| 의존 경로 |" in core                         # the artifactId column stays
    # the 의존 경로 column already says the path: only a row with something more (here a variant) gets an entry
    assert "들어온 경로:" in core and "- `org.example:json`" not in core
    assert f"- `org.example:yaml` 1.3: `{CORE}:4.0` 경로로 가져옴, 형식 `test-jar` classifier `tests`" in core
    comp = sec.split(f"### `{COMP}`", 1)[1].split("### ", 1)[0]
    assert "들어온 경로" not in comp                       # "부모 pom에서 버전 지정" in the column covers it
    web = sec.split("### `com.example.inhouse:inhouse-web`", 1)[1].split("### ", 1)[0]
    assert "들어온 경로" not in web
    # modules with different sources do get the entry
    json_, = _by_ga(state, "org.example:json")
    json_.modules = ["app-domain", "app-store"]
    json_.sources.append(ModuleSource(module="app-store", how="transitive", via=[f"{CORE}:4.0", LIB_A], inhouse=[CORE]))
    core = _section(report.build_report_md(state, paths), "사내 라이브러리에서 발생한 취약점").split(f"### `{CORE}`", 1)[1]
    assert f"  - `app-store` 모듈: `{CORE}:4.0` > `{LIB_A}` 경로로 가져옴" in core


def test_sources_survive_the_state_file(tmp_path):
    from sbmig.remediations.models import RemediationState
    state, paths = _with_sources(tmp_path)
    again = RemediationState.from_dict(state.to_dict())
    srcs = [s for r in again.residuals for s in r.sources]
    assert srcs and all(isinstance(s, ModuleSource) for s in srcs)
    assert any(isinstance(s.declared, PomLocation) for s in srcs)
    assert report.build_report_md(again, paths) == report.build_report_md(state, paths)


def test_no_partial_inhouse_note_and_the_new_report_reads_well(tmp_path):
    state, paths = _with_sources(tmp_path)
    for r in state.residuals:            # an old state may still carry `inhouse` on a manual row: no note any more
        if r.reason == "manual_review":
            r.inhouse = [CORE]
    md = report.build_report_md(state, paths)
    assert "일부 모듈은 사내 라이브러리에서 가져옴" not in md
    assert "루트 pom을 상속하지 않는 모듈" not in md and "상속" not in md.split("## 처리 기록", 1)[0]
    assert _detached(md) == [], _detached(md)
    head = md.split("## 처리 기록", 1)[0]
    for word in BANNED_WORDS:
        assert word not in head, word
    new_parts = "\n".join(ln for ln in head.splitlines() if "들어온 경로" in ln or ln.lstrip().startswith("- `"))
    assert "→" not in new_parts and "—" not in new_parts      # no arrow / em dash in the new lines


def test_reported_version_is_shown_once_and_counted(tmp_path):
    state, paths = _company(tmp_path)
    r = Residual(ga="org.example:ver", version="2.2.0.RELEASE", origin="transitive", reason="manual_review",
                 detail="no patched version known (IVAS gave none) and no policy pin", finding_ids=["CVE-2026-0900"],
                 modules=["app-a"], reported_version="2.2.0.release")
    state.residuals.append(r)
    base = ScanResult.from_dict(__import__("json").loads(paths.scan("baseline").read_text(encoding="utf-8")))
    base.findings.append(Finding(id="CVE-2026-0900", kind="library", identifier="org.example:ver",
                                 version="2.2.0.release", action="NE", score=9.1))
    write_json_atomic(paths.scan("baseline"), base.to_dict())
    md = report.build_report_md(state, paths)
    people = _section(md, "직접 확인이 필요한 취약점")
    row = next(ln for ln in people.splitlines() if "`org.example:ver`" in ln)
    assert "2.2.0.RELEASE (IVAS 표기: 2.2.0.release)" in row
    assert row.startswith("| 🔴 9.1 |") and "🔴 CVE-2026-0900 (9.1)" in row      # scored across the case difference
    assert md.count("IVAS 표기") == 1
    assert "## 직접 확인이 필요한 취약점 — 5건" in md            # counted across the case difference


def test_an_item_split_by_module_is_counted_once(tmp_path):
    state, paths = _company(tmp_path)
    m1, = _by_ga(state, "org.example:m1")
    m1.inhouse = []
    part = Residual(ga="org.example:m1", version="1.0", origin="transitive", reason="inhouse_artifact",
                    detail=f"brought in by the in-house artifact(s) {CORE}: only their owner can change it",
                    finding_ids=list(m1.finding_ids), modules=["app-x"], via=[f"{CORE}:4.0", "org.example:m1:1.0"],
                    inhouse=[CORE],
                    sources=[ModuleSource(module="app-x", how="transitive", via=[f"{CORE}:4.0"], inhouse=[CORE])])
    state.residuals.append(part)
    md = report.build_report_md(state, paths)
    lines = md.splitlines()
    # 7 left = 4 in-house + 4 manual - 1 in both: the total still reads as "남은 7건"
    assert "남은 7건 중 4건은 사내 라이브러리 담당자에게 요청해야 하고, 4건은 프로젝트에서 직접 확인해야 합니다." in lines[3]
    assert "그중 1건은 모듈에 따라 사내 라이브러리 담당자와 프로젝트 양쪽에서 조치해야 해서 두 절에 모두 나옵니다." in lines[3]
    assert "`org.example:m1`" in _section(md, "사내 라이브러리에서 발생한 취약점")
    assert "`org.example:m1`" in _section(md, "직접 확인이 필요한 취약점")
    assert _detached(md) == []


# today's rendering of the legacy company state (no modules, via, in-house or sources), captured before 들어온 경로
# existed; the only differences allowed are the three deliberate wording changes below
DELIBERATE = [
    ("### 루트 pom을 상속하지 않는 모듈 — 라이브러리 3개", "### 모듈 pom에 직접 지정해야 하는 버전 — 라이브러리 3개"),
    ("이 모듈들은 프로젝트의 루트 pom이 아니라 회사 부모 pom을 상속합니다. 루트 pom에서 버전을 지정해도 이 모듈들에는 적용되지 "
     "않아 자동으로 조치하지 못했습니다.",
     "이 모듈들은 프로젝트 최상위 pom.xml이 아니라 회사 부모 pom에서 설정을 물려받습니다. 최상위 pom.xml에 버전을 지정해도 "
     "이 모듈들에는 적용되지 않습니다. 모듈 pom에 버전 지정을 제안하는 설정이 꺼져 있어 제안하지 않았습니다. 해당 모듈의 "
     "pom.xml에서 dependencyManagement에 조치된 버전을 추가하거나, 이 라이브러리를 가져오는 의존성의 버전을 올려 주세요."),
    ("jar가 아닌 형식이거나 classifier가 붙은 의존성은 버전을 지정하는 방식으로 조치할 수 없습니다.",
     "classifier는 같은 버전의 jar를 용도별로 구분하는 이름입니다(예: jakarta). 아래 항목은 jar가 아닌 형식이거나 "
     "classifier가 붙어 있어 버전을 지정하는 방식으로 조치하지 못했습니다."),
]


def test_an_old_state_renders_as_before(tmp_path):
    state, paths = _company(tmp_path, legacy=True)
    expected = LEGACY_TODAY
    for old, new in DELIBERATE:
        assert old in expected
        expected = expected.replace(old, new)
    assert report.build_report_md(state, paths) == expected
    # and the current shape without sources differs from today only by those changes and the dropped note
    state, paths = _company(tmp_path / "now")
    md = report.build_report_md(state, paths)
    assert "들어온 경로" not in md and "IVAS 표기" not in md


LEGACY_TODAY = (
    '# 취약점 조치 결과 — app.zip\n'
    '\n'
    '> ⚠️ **일부만 조치되었습니다.**\n'
    '> 취약점 8건 가운데 1건을 조치했고 7건이 남았습니다. 남은 7건 중 3건은 사내 라이브러리 담당자에게 요청해야 하고, 4건은 프로젝트에서 직접 확인해야 합니다. 상세: 조치 후 검사가 끝나지 않았습니다(사유: 결과 페이지 누락).\n'
    '\n'
    '## 요약\n'
    '\n'
    '| 구분 | 취약점 | 라이브러리 |\n'
    '|---|---|---|\n'
    '| 조치 전 | 8건 | 8개 |\n'
    '| 조치 완료 | 1건 | 1개 |\n'
    '| 사내 라이브러리 담당자 요청 | 3건 | 3개 |\n'
    '| 직접 확인 필요 | 4건 | 4개 |\n'
    '| 조치 후 남은 취약점 | 7건 | - |\n'
    '\n'
    '| 위험도 | 기준 (IVAS 점수) | 조치 전 | 조치 후 |\n'
    '|---|---|---|---|\n'
    '| 🔴 심각 | 9.0 이상 | 1건 | 1건 |\n'
    '| 🟠 높음 | 7.0 이상 | 6건 | 6건 |\n'
    '| 🟡 보통 | 4.0 이상 | 0건 | 0건 |\n'
    '| 낮음·점수 없음 | 4.0 미만 | 1건 | 0건 |\n'
    '\n'
    '결과 파일: 결과 ZIP — 빌드 성공, 재검사 완료\n'
    '\n'
    '## 해야 할 일\n'
    '\n'
    '1. **사내 라이브러리 담당자에게 요청하기 (3건)** — 「사내 라이브러리에서 발생한 취약점」에 아티팩트별로 정리했습니다. 각 아티팩트의 아키텍처 담당자에게 연락해 주세요.\n'
    '2. **직접 확인하기 (4건)** — 「직접 확인이 필요한 취약점」의 항목은 자동으로 조치할 수 없습니다. 항목별 사유를 확인하고 담당 팀에서 조치해 주세요.\n'
    '\n'
    '## 조치 완료 — 1건\n'
    '\n'
    '| 위험도 | 라이브러리 | 버전 변경 | 수정한 곳 | 해결된 취약점 |\n'
    '|---|---|---|---|---|\n'
    '| - | `org.example:log` | 2.25.4 → 2.25.5 | `app-boot/pom.xml` 41행 | CVE-2026-0001 |\n'
    '\n'
    '- **`log` 2.25.4 → 2.25.5** (제안 `p-1-1`)\n'
    '  - 버전 확인: 저장소 nexus.invalid\n'
    '  - 참고: app-domain, app-service, app-store 모듈은 같은 라이브러리를 다른 경로로 가져옵니다. 이 모듈들에 2.25.4 버전이 남아 있으면 다음 라운드에서 다시 조치합니다.\n'
    '\n'
    '## 사내 라이브러리에서 발생한 취약점 — 3건\n'
    '\n'
    '> 해당 아티팩트는 예외처리가 가능합니다. 담당자에게 연락 부탁드립니다.\n'
    '\n'
    '사내 라이브러리가 가져오는 의존성이거나, 회사 부모 pom에서 버전을 지정하는 라이브러리입니다. 사내 라이브러리의 pom은 담당자만 수정할 수 있어 이 프로젝트에서는 변경하지 않았습니다.\n'
    '\n'
    '### `com.example.inhouse:inhouse-core` — 라이브러리 2개\n'
    '\n'
    '| 위험도 | 취약한 라이브러리 | 버전 | 의존 경로 | 취약점 |\n'
    '|---|---|---|---|---|\n'
    '| 🔴 9.8 | `org.example:json` | 2.0 | - | 🔴 CVE-2026-0101 (9.8) |\n'
    '| 🟠 7.5 | `org.example:yaml` | 1.3 | - | 🟠 CVE-2026-0102 (7.5) |\n'
    '\n'
    '### `com.example.inhouse:comp-jar` (회사 부모 pom) — 라이브러리 1개\n'
    '\n'
    '| 위험도 | 취약한 라이브러리 | 버전 | 의존 경로 | 취약점 |\n'
    '|---|---|---|---|---|\n'
    '| 🟠 7.5 | `org.example:spring-web` | 6.0 | 부모 pom에서 버전 지정 | 🟠 CVE-2026-0103 (7.5) |\n'
    '\n'
    '### `com.example.inhouse:inhouse-web` — 라이브러리 1개\n'
    '\n'
    '| 위험도 | 취약한 라이브러리 | 버전 | 의존 경로 | 취약점 |\n'
    '|---|---|---|---|---|\n'
    '| 🔴 9.8 | `org.example:json` | 2.0 | - | 🔴 CVE-2026-0101 (9.8) |\n'
    '\n'
    '## 직접 확인이 필요한 취약점 — 4건\n'
    '\n'
    '### 루트 pom을 상속하지 않는 모듈 — 라이브러리 3개\n'
    '\n'
    '이 모듈들은 프로젝트의 루트 pom이 아니라 회사 부모 pom을 상속합니다. 루트 pom에서 버전을 지정해도 이 모듈들에는 적용되지 않아 자동으로 조치하지 못했습니다.\n'
    '\n'
    '아래 항목은 모두 간접 의존성입니다.\n'
    '\n'
    '| 위험도 | 라이브러리 | 버전 | 취약점 |\n'
    '|---|---|---|---|\n'
    '| 🟠 7.5 | `org.example:m0` | 1.0 | 🟠 CVE-2026-0200 (7.5) |\n'
    '| 🟠 7.5 | `org.example:m1` | 1.0 | 🟠 CVE-2026-0201 (7.5) |\n'
    '| 🟠 7.5 | `org.example:m2` | 1.0 | 🟠 CVE-2026-0202 (7.5) |\n'
    '\n'
    '### classifier가 붙은 의존성 — 라이브러리 1개\n'
    '\n'
    'jar가 아닌 형식이거나 classifier가 붙은 의존성은 버전을 지정하는 방식으로 조치할 수 없습니다.\n'
    '\n'
    '| 위험도 | 라이브러리 | 버전 | 구분 | 취약점 |\n'
    '|---|---|---|---|---|\n'
    '| 🟠 7.5 | `org.example:netty` (linux) | 4.1 | 간접 의존성 | 🟠 CVE-2026-0300 (7.5) |\n'
    '\n'
    '## 변경하지 않은 것\n'
    '\n'
    '- 애플리케이션 소스와 프로젝트 구조(파일·폴더)는 변경하지 않았습니다.\n'
    '- 결과 ZIP의 파일 목록은 원본과 같습니다. 달라진 것은 적용된 pom.xml의 버전 표기뿐입니다.\n'
    '- 원본 ZIP은 `original/source.zip`에 그대로 보관합니다.\n'
    '\n'
    '## 처리 기록\n'
    '\n'
    '<details><summary>단계별 기록 (문의할 때 필요한 정보)</summary>\n'
    '\n'
    '- 작업 ID: `rem-1`\n'
    '- 상태 코드: `INCOMPLETE` · 사유 코드: `manual_review`, `residual_findings`\n'
    '- 진행한 라운드: 1회\n'
    '\n'
    '**1. 조치 전 빌드** — 성공 · 로그 `baseline/build.log`\n'
    '\n'
    '**2. 조치 전 검사 (IVAS)** — 검사 완료 · 취약점 8건 · 검사한 ZIP sha256 `aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa`\n'
    '\n'
    '**3. 라운드 1**\n'
    '\n'
    '| 제안 | 라이브러리 | 버전 변경 | 수정 방식 | 처리 | 버전 확인 |\n'
    '|---|---|---|---|---|---|\n'
    '| p-1-1 | `org.example:log` | 2.25.4 → 2.25.5 | pom의 버전 표기 수정 | 자동 적용 | 저장소 nexus.invalid |\n'
    '\n'
    '- 수정본 빌드: 성공\n'
    '- 수정본 채택: 예\n'
    '- 검증 참고: app-domain 모듈의 org.example:log 버전은 아직 2.25.4입니다. 이번 수정이 적용되지 않는 모듈이므로 다음 라운드에서 조치합니다(p-1-1).\n'
    '- 재검사: `final-r1`\n'
    '\n'
    '**4. 결과 파일** — `delivery/result.zip` (0.1 MB 미만, 파일 3개) · 빌드 성공 · 재검사 완료 · 재검사한 파일과 일치\n'
    '- sha256 `bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb`\n'
    '- 조치 후 검사: `scans/final-r1.json` — 검사 완료, 7건\n'
    '\n'
    '**5. 빌드 설정 점검** — 이상 없음\n'
    '\n'
    '</details>\n'
    '\n'
    '<details><summary>남은 취약점 전체 목록 — 7건</summary>\n'
    '\n'
    '| 위험도 | 취약점 | 라이브러리 | 버전 | IVAS 조치 상태 |\n'
    '|---|---|---|---|---|\n'
    '| 🔴 9.8 | CVE-2026-0101 | `org.example:json` | 2.0 | 조치 필요 (NE) |\n'
    '| 🟠 7.5 | CVE-2026-0200 | `org.example:m0` | 1.0 | 조치 필요 (NE) |\n'
    '| 🟠 7.5 | CVE-2026-0201 | `org.example:m1` | 1.0 | 조치 필요 (NE) |\n'
    '| 🟠 7.5 | CVE-2026-0202 | `org.example:m2` | 1.0 | 조치 필요 (NE) |\n'
    '| 🟠 7.5 | CVE-2026-0300 | `org.example:netty` | 4.1 | 조치 필요 (NE) |\n'
    '| 🟠 7.5 | CVE-2026-0103 | `org.example:spring-web` | 6.0 | 조치 필요 (NE) |\n'
    '| 🟠 7.5 | CVE-2026-0102 | `org.example:yaml` | 1.3 | 조치 필요 (NE) |\n'
    '\n'
    '</details>\n'
    ''
)


def test_variant_words_never_show_a_raw_plain_jar():
    words = report._variant_words
    assert words("") == "" and words("jar/") == ""
    assert words("jar/jakarta") == "classifier `jakarta`"
    assert words("test-jar/") == "형식 `test-jar`"
    assert words("test-jar/tests") == "형식 `test-jar` classifier `tests`"
    assert words("jar/, jar/jakarta") == "사용 형태 2가지(일반 jar, classifier `jakarta`)"
    assert words("jar/jdk8, test-jar/tests") == "사용 형태 2가지(classifier `jdk8`, 형식 `test-jar` classifier `tests`)"
    for v in ("jar/, jar/jakarta", "jar/jdk8, test-jar/tests", "test-jar/"):
        assert "jar/" not in words(v)
        assert all("," not in x and "/" not in x for x in re.findall(r"`([^`]*)`", words(v)))     # one value per code span


def test_a_module_with_the_plain_jar_and_a_variant(tmp_path):
    state, paths = _with_sources(tmp_path)
    netty, = _by_ga(state, "org.example:netty")
    netty.sources[0].variant = "jar/, jar/jakarta"
    md = report.build_report_md(state, paths)
    line = next(ln for ln in md.splitlines() if ln.startswith("- `org.example:netty` 4.1:"))
    assert line.endswith(f"`{LIB_A}` 경로로 가져옴, 사용 형태 2가지(일반 jar, classifier `jakarta`)")
    assert "jar/" not in md.split("## 처리 기록", 1)[0] and _detached(md) == []


def test_a_direct_module_in_a_transitive_row_is_not_called_transitive(tmp_path):
    """A row's origin is the item's; an also-module may declare the library itself. Then "아래 항목은 모두 간접
    의존성입니다." would be false: the origin stays in the table and that row says its modules differ."""
    state, paths = _with_sources(tmp_path)
    people = _section(report.build_report_md(state, paths), "직접 확인이 필요한 취약점")
    group = people.split("### 모듈 pom에 직접 지정해야 하는 버전", 1)[1].split("### ", 1)[0]
    assert "아래 항목은 모두 간접 의존성입니다." not in group        # m2 has a direct and a managed module
    assert "| 위험도 | 라이브러리 | 버전 | 구분 | 사용 모듈 | 취약점 |" in group
    assert "| 🟠 7.5 | `org.example:m2` | 1.0 | 모듈마다 다름 (들어온 경로 참고) |" in group
    assert "| 🟠 7.5 | `org.example:m0` | 1.0 | 간접 의존성 |" in group
    m2, = _by_ga(state, "org.example:m2")
    for src in m2.sources:
        src.how, src.declared, src.manager = "transitive", None, ""
        src.via = src.via or [LIB_A]
    group = _section(report.build_report_md(state, paths), "직접 확인이 필요한 취약점").split(
        "### 모듈 pom에 직접 지정해야 하는 버전", 1)[1].split("### ", 1)[0]
    assert "아래 항목은 모두 간접 의존성입니다." in group and "모듈마다 다름" not in group
