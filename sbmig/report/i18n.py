"""report.md languages (`report.language: en | ko`). Dependency-free string table + helpers.

`tr(lang, text, **fmt)` — `text` is the English source string (it is also the table key, `{name}` placeholders
filled by `str.format`); a missing translation falls back to the English text, so `en` output never changes.
`tr_line(lang, line)` — lines the Handover builder stores in report.json (from-here items, merge-conflict lines,
handover notes) stay English in the data; for `ko` the renderer maps the known templates through `PATTERNS`.
Identifiers, paths, Maven / javac messages and free-text notes from the loop are never translated.
"""
from __future__ import annotations

import re

LANGUAGES = ("en", "ko")

STRINGS: dict[str, dict[str, str]] = {"ko": {
    # ---- section titles
    "Blocked — needs human / library action": "차단됨 — 사람 / 라이브러리 조치 필요",
    "Handover — what we replaced, what is yours": "인수인계 — 교체한 것과 이제 팀이 맡을 것",
    "Properties to set before running": "실행 전에 설정할 프로퍼티",
    "# Migration report — {name}": "# 마이그레이션 보고서 — {name}",
    "## Unresolved errors": "## 미해결 오류",
    "## Manual follow-ups": "## 수동 후속 조치",
    "## Flags": "## 플래그",
    "## What we changed": "## 변경 내역",
    "## Phases": "## 단계별 결과",
    "## Fix attempts": "## 수정 시도",
    "## Escalated clusters": "## 에스컬레이션된 클러스터",
    "## Knowledge base": "## 지식 베이스",
    "## LLM usage and cost": "## LLM 사용량과 비용",
    "| LLM cost | {cost} (see \"LLM usage and cost\") |": "| LLM 비용 | {cost} (\"LLM 사용량과 비용\" 참조) |",
    "(+ unpriced: {models})": "(+ 단가 없음: {models})",
    "(token counts missing for some calls)": "(일부 호출의 토큰 수 누락)",
    "| Model | Calls | Input tokens | Output tokens | Price in / out (per 1M) | Cost |":
        "| 모델 | 호출 | 입력 토큰 | 출력 토큰 | 단가 입력 / 출력 (1M당) | 비용 |",
    "no price configured": "단가 미설정",
    "| (no per-model usage recorded) | {calls} | {inp:,} | {out:,} | - | - |":
        "| (모델별 사용량 기록 없음) | {calls} | {inp:,} | {out:,} | - | - |",
    "| **Total** | | | | | **{total}** |": "| **합계** | | | | | **{total}** |",
    "Prices: {note}.": "단가: {note}.",
    "No price for {models} in `llm.pricing` — not included in the total.":
        "{models}의 단가가 `llm.pricing`에 없음 — 합계에서 제외.",
    "Some calls came back without token counts (the gateway did not report `usage`); their cost is not included.":
        "일부 호출은 토큰 수 없이 돌아옴(게이트웨이가 `usage`를 주지 않음); 그 비용은 포함되지 않음.",
    "Cost = input tokens × input price + output tokens × output price, per 1M tokens, in {currency}; cached-input "
    "discounts are not visible to sbmig.":
        "비용 = 입력 토큰 × 입력 단가 + 출력 토큰 × 출력 단가(1M 토큰당, {currency}); 캐시 입력 할인은 sbmig가 알 수 없음.",
    "## Notes": "## 메모",
    "## Outputs": "## 산출물",
    "### Versions replaced": "### 교체된 버전",
    "### Libraries needing your attention": "### 조치가 필요한 라이브러리",
    "### From here on, your team": "### 이후 담당 팀이 할 일",
    # ---- header table
    "✅ BUILD SUCCESS": "✅ 빌드 성공",
    "❌ NOT BUILDING": "❌ 빌드 실패",
    "| **Result** | {ok} (final `mvn clean package`) |": "| **결과** | {ok} (최종 `mvn clean package`) |",
    "| Case | `{case}` |": "| 유형 | `{case}` |",
    "| As-is | java {java}, spring boot {boot}, spring {spring}, {poms} pom(s) |":
        "| As-is | java {java}, spring boot {boot}, spring {spring}, pom {poms}개 |",
    "| Run id | `{run_id}` |": "| 실행 ID | `{run_id}` |",
    "| Started / finished | {started} / {finished} ({duration}) |":
        "| 시작 / 종료 | {started} / {finished} ({duration}) |",
    "| LLM | {calls} call(s), {inp} in / {out} out tokens{model} |":
        "| LLM | {calls}회 호출, 토큰 입력 {inp} / 출력 {out}{model} |",
    "| Flags | {flags} |": "| 플래그 | {flags} |",
    "| **Blocked** | {line} (see below) |": "| **차단됨** | {line} (아래 참조) |",
    "{m}m {s}s": "{m}분 {s}초",
    "{secs}s": "{secs}초",
    # ---- blocked
    "{n} item(s) — ": "{n}건 — ",
    " (class `{owner}`)": " (클래스 `{owner}`)",
    "class `{owner}`": "클래스 `{owner}`",
    "| reason | artifact / owner class | required → found | files | attempts | suggestion |":
        "| 사유 | 아티팩트 / 소유 클래스 | 필요 → 실제 | 파일 | 시도 | 제안 |",
    "{n} cluster(s) were deliberately left unchanged because the fix is outside this project (a library, a "
    "repository, the environment) or the agent gave up on them. The rest of the migration continued.":
        "클러스터 {n}개는 수정할 곳이 이 프로젝트 밖(라이브러리, 저장소, 환경)이거나 에이전트가 해결을 포기해 "
        "의도적으로 손대지 않았다. 나머지 마이그레이션은 계속 진행했다.",
    "<details><summary><code>{id}</code> — {n} error(s)</summary>":
        "<details><summary><code>{id}</code> — 오류 {n}건</summary>",
    # ---- unresolved
    "None. The final build passed.": "없음. 최종 빌드가 통과했다.",
    "None. No structured errors were captured; check the last build log.":
        "없음. 구조화된 오류를 수집하지 못했다. 마지막 빌드 로그를 확인할 것.",
    "{n} error(s) left after the last build, grouped by file:": "마지막 빌드 후 남은 오류 {n}건 (파일별):",
    "{n} error(s) left after the last build (blocked clusters excluded), grouped by file:":
        "마지막 빌드 후 남은 오류 {n}건 (차단된 클러스터 제외, 파일별):",
    "module {module}": "모듈 {module}",
    "artifact {coords}": "아티팩트 {coords}",
    "(no file)": "(파일 없음)",
    " — symbol: `{symbol}`": " — 심볼: `{symbol}`",
    " — location: {location}": " — 위치: {location}",
    "Last build log: `{path}`": "마지막 빌드 로그: `{path}`",
    "None.": "없음.",
    # ---- what we changed / phases / attempts
    "Plan steps executed:": "실행한 계획 단계:",
    "OpenRewrite runs:": "OpenRewrite 실행:",
    "| phase | recipes | changed files | ok |": "| 단계 | 레시피 | 변경 파일 | 성공 |",
    "yes": "예",
    "no: {error}": "아니오: {error}",
    "Restructure / deterministic edits:": "구조 변경 / 결정적 수정:",
    "| phase | steps | builds | build ok | attempts by decision | outcomes |":
        "| 단계 | 스텝 | 빌드 횟수 | 빌드 성공 | 결정별 시도 | 결과 |",
    "| phase/iter | cluster | decision | via | outcome | commit | analysis |":
        "| 단계/반복 | 클러스터 | 결정 | 수단 | 결과 | 커밋 | 분석 |",
    "handler `{handler}`": "핸들러 `{handler}`",
    "KB case {cases}": "KB 사례 {cases}",
    " + advisory {ids}": " + 어드바이저리 {ids}",
    # ---- knowledge base / notes
    "Nothing harvested.": "수집한 항목 없음.",
    "- cases created: {x}": "- 생성된 사례: {x}",
    "- recipe candidates created: {x}": "- 생성된 레시피 후보: {x}",
    "- recipes credited: {x}": "- 성공으로 기록된 레시피: {x}",
    "- recipes run by triage: {x}": "- 트리아지가 실행한 레시피: {x}",
    "- plan: {note}": "- 계획: {note}",
    # ---- handover
    "No version changes and no library advisories recorded for this run.":
        "이번 실행에서 기록된 버전 변경과 라이브러리 어드바이저리가 없다.",
    "- parent: {before} → {after}": "- 부모 POM: {before} → {after}",
    "- java: {before} → {after}": "- java: {before} → {after}",
    "- spring boot: {before} → {after}": "- spring boot: {before} → {after}",
    "| what | before | after | why |": "| 항목 | 이전 | 이후 | 사유 |",
    "managed": "관리됨",
    " (managed)": " (관리됨)",
    "⚠ 1 dependency pinned at the as-is version: the new parent / Boot BOM no longer manages it.":
        "⚠ 의존성 1개를 as-is 버전으로 고정했다: 새 parent / Boot BOM이 더 이상 관리하지 않는다.",
    "⚠ {n} dependencies pinned at the as-is version: the new parent / Boot BOM no longer manages them.":
        "⚠ 의존성 {n}개를 as-is 버전으로 고정했다: 새 parent / Boot BOM이 더 이상 관리하지 않는다.",
    "Added: {items}": "추가: {items}",
    "Removed: {items}": "제거: {items}",
    "No dependency versions changed.": "변경된 의존성 버전 없음.",
    "_note: {note}_": "_참고: {note}_",
    "| library | as-is | status | policy | what we did | outcome | what you do next | docs |":
        "| 라이브러리 | as-is | 상태 | 정책 | 수행한 조치 | 결과 | 다음 할 일 | 문서 |",
    "✅ replaced": "✅ 교체됨",
    "⚠ still present": "⚠ 남아 있음",
    "in the plan": "계획에 포함",
    "added for this advisory": "이 어드바이저리를 위해 추가",
    "built with the as-is version pinned": "as-is 버전을 고정한 채 빌드",
    "**Merge conflicts** (multi-module merge: first module in `<modules>` order wins)":
        "**병합 충돌** (멀티 모듈 병합: `<modules>` 순서상 첫 모듈이 우선)",
    "Nothing beyond the usual review — the migrated project builds with the versions above.":
        "일반적인 검토 외에는 할 일이 없다 — 마이그레이션된 프로젝트는 위 버전으로 빌드된다.",
    # handover reasons (VersionChange.reason constants of handover.py)
    "carried over (no longer managed)": "as-is 버전 유지 (더 이상 관리되지 않음)",
    "parent swap": "parent 교체",
    "optional bump": "선택 아티팩트 버전 상향",
    "mandatory": "필수 아티팩트",
    "spring boot 4 upgrade": "spring boot 4 업그레이드",
    "recipe/handler": "레시피/핸들러",
    # properties (round 8)
    "key": "키",
    "before migration": "마이그레이션 전 공급처",
    "referenced at": "참조 위치",
    "written as": "기록한 값",
    "set via": "설정 방법",
    "The code reads these settings but no packaged resource defines them (the external container supplied them "
    "before). Set each one before the first start.":
        "코드는 이 설정을 읽지만 패키지된 리소스 어디에도 정의가 없다(이전에는 외부 컨테이너가 제공했다). "
        "첫 기동 전에 각각 값을 설정할 것.",
    "How to supply them:": "설정 방법:",
    "Packaged placeholders: {files} (grep `__SET_ME__`)": "패키지된 플레이스홀더: {files} (`__SET_ME__`로 검색)",
    # ---- flag help (FLAG_HELP / FLAG_PREFIX_HELP values of markdown.py)
    "OpenRewrite could not parse some source files (see the phase notes / rewrite log): recipes that need type "
    "information skipped them; the source handlers and the fix loop covered what the build found":
        "OpenRewrite가 일부 소스 파일을 파싱하지 못함(phase 노트 / rewrite 로그 참고): 타입 정보가 필요한 레시피는 그 "
        "파일을 건너뛰었고, 빌드가 찾아낸 것은 소스 핸들러와 수정 루프가 처리했다",
    "javax.* Jakarta EE references the recipes had left in the sources were renamed to jakarta.* by the "
    "jakarta-namespace-sweep handler (string literals / resource files are listed as follow-ups)":
        "레시피가 소스에 남긴 javax.* Jakarta EE 참조를 jakarta-namespace-sweep 핸들러가 jakarta.*로 바꿨다"
        "(문자열 리터럴 / 리소스 파일은 후속 작업에 나열)",
    "javax.* Jakarta EE references inside JSP / tag files (page imports, scriptlets, useBean) were renamed to "
    "jakarta.* by the jakarta-namespace-sweep handler — JSPs are compiled by Tomcat at runtime, so the build cannot "
    "prove them: smoke-test the pages":
        "JSP / 태그 파일 안의 javax.* Jakarta EE 참조(page import, 스크립틀릿, useBean)를 jakarta-namespace-sweep "
        "핸들러가 jakarta.*로 바꿨다 — JSP는 Tomcat이 실행 시점에 컴파일하므로 빌드로는 확인할 수 없다: 화면을 직접 "
        "열어 확인할 것",
    "web.xml servlets / filters / listeners / error pages were turned into a generated WebXmlRegistrations "
    "@Configuration in the boot module (web.xml itself is not read by Spring Boot) — verify the mappings in the "
    "Handover":
        "web.xml의 서블릿 / 필터 / 리스너 / 오류 페이지를 boot 모듈에 생성한 WebXmlRegistrations @Configuration으로 "
        "옮겼다(Spring Boot는 web.xml 자체를 읽지 않음) — 인수인계에서 매핑을 확인할 것",
    "repository interfaces that extended only PagingAndSortingRepository got ListCrudRepository<T, ID> back "
    "(Spring Data 3 removed its CRUD methods)":
        "PagingAndSortingRepository만 상속하던 리포지토리 인터페이스에 ListCrudRepository<T, ID>를 다시 추가했다"
        "(Spring Data 3에서 CRUD 메서드가 빠짐)",
    "javax EE API jars are still on the resolved classpath after the migration (section \"javax API jars still on "
    "the classpath\"): the direct dependencies that drag them in are compiled against javax and need Jakarta builds":
        "마이그레이션 후에도 javax EE API jar가 해석된 클래스패스에 남아 있다(\"클래스패스에 남은 javax API jar\" 절): "
        "이를 끌어오는 직접 의존성이 javax 기준으로 컴파일되어 있어 Jakarta 빌드가 필요하다",
    "a third-party library with a known jakarta.* release was raised to it "
    "(knowledge/libraries/jakarta_libraries.yml, handler jakarta-library-floor)":
        "jakarta.* 릴리스가 알려진 서드파티 라이브러리를 그 버전으로 올렸다"
        "(knowledge/libraries/jakarta_libraries.yml, jakarta-library-floor 핸들러)",
    # ---- javax API jars still on the classpath (X1b, 2026-10-22)
    "javax API jars still on the classpath": "클래스패스에 남은 javax API jar",
    "| javax API jars | {n} |": "| javax API jar | {n}개 |",
    " ← declared in the pom": " ← pom에 직접 선언",
    "{n} javax EE API jar(s) are still on the resolved classpath after the migration (`mvn dependency:tree`). The "
    "library in \"via\" is compiled against javax: code that passes jakarta.* types to it does not compile, and it "
    "fails at runtime.":
        "마이그레이션 후에도 javax EE API jar {n}개가 해석된 클래스패스에 남아 있다(`mvn dependency:tree`). "
        "\"via\"의 라이브러리는 javax 기준으로 컴파일되어 있어, jakarta.* 타입을 넘기는 코드는 컴파일되지 않고 "
        "런타임에도 실패한다.",
    "| artifact | module | scope | via (direct dependency) | declared in pom | what to do |":
        "| 아티팩트 | 모듈 | 스코프 | via (직접 의존성) | pom에 선언 | 할 일 |",
    "(root)": "(루트)",
    "no": "아니오",
    "raise to {version} (jakarta.* since {since})": "{version}(으)로 올림 ({since}부터 jakarta.*)",
    "coordinates changed: relocate to `{key}:{version}` (jakarta.* since {since})":
        "좌표가 바뀜: `{key}:{version}`(으)로 이전 ({since}부터 jakarta.*)",
    ", requires Java {java}": ", Java {java} 이상 필요",
    "in-house → Jakarta build": "사내 아티팩트 → Jakarta 빌드 필요",
    "upgrade / replace": "업그레이드 / 교체",
    "remove the declaration (handler dependency-relocation)": "선언 제거 (dependency-relocation 핸들러)",
    "every build ran with `-Dmaven.test.skip=true` (build.skip_test_compile): test sources were neither compiled nor "
    "run — the owning team compiles and fixes them":
        "모든 빌드가 `-Dmaven.test.skip=true`로 실행됨(build.skip_test_compile): 테스트 소스는 컴파일도 실행도 하지 "
        "않았다 — 소유 팀이 컴파일하고 고칠 것",
    "main code builds, but test sources only compile with `-Dmaven.test.skip=true` (see follow-ups) — fix the "
    "tests before relying on CI":
        "메인 코드는 빌드되지만 테스트 소스는 `-Dmaven.test.skip=true`로만 컴파일된다(후속 조치 참조) — CI에 "
        "의존하기 전에 테스트를 고칠 것",
    "main code compiles and packages, but some tests fail at runtime: the jar was built with `-DskipTests` "
    "(failing tests listed in follow-ups / unresolved)":
        "메인 코드는 컴파일·패키징되지만 일부 테스트가 실행 중 실패한다: jar는 `-DskipTests`로 빌드했다(실패한 "
        "테스트는 후속 조치 / 미해결 오류에 있음)",
    "the run hit `loop.max_total_minutes`; remaining loops were skipped, verify still ran":
        "실행이 `loop.max_total_minutes`에 도달했다. 남은 루프는 건너뛰었고 verify는 실행했다",
    "the run used all `loop.max_llm_calls`; remaining clusters were escalated":
        "실행이 `loop.max_llm_calls`를 모두 썼다. 남은 클러스터는 에스컬레이션했다",
    "dry run: decisions were computed but nothing was applied": "dry run: 결정만 계산했고 아무것도 적용하지 않았다",
    "the run stopped early (see follow-ups for the reason)": "실행이 일찍 멈췄다(이유는 후속 조치 참조)",
    "Maven was not runnable on this machine": "이 머신에서 Maven을 실행할 수 없었다",
    "the LLM failed twice in a row (transport/config); LLM-only clusters are listed as blocked (llm_unavailable) "
    "— handlers and recipes still ran":
        "LLM이 연속 두 번 실패했다(전송/설정). LLM으로만 풀 수 있는 클러스터는 차단(llm_unavailable)으로 "
        "표시했다 — 핸들러와 레시피는 그대로 실행했다",
    "the configured in-house parent POM is not resolvable — nothing can build until it is (see Blocked)":
        "설정한 사내 parent POM을 해석할 수 없다 — 해결될 때까지 아무것도 빌드되지 않는다('차단됨' 참조)",
    "a maven-enforcer rule that fails on JDK 21 was relaxed in the pom":
        "JDK 21에서 실패하는 maven-enforcer 규칙을 pom에서 완화했다",
    "maven-enforcer is skipped (-Denforcer.skip=true) — review the rules":
        "maven-enforcer를 건너뛴다(-Denforcer.skip=true) — 규칙을 검토할 것",
    "-Werror was removed from the compiler configuration (JDK 21 deprecations would be fatal)":
        "컴파일러 설정에서 -Werror를 제거했다(JDK 21 deprecation 경고가 오류가 되기 때문)",
    "some dependencies were pinned at the version the as-is parent managed, because the new parent / Boot BOM no "
    "longer manages them (⚠ in Handover) — the owners must confirm or replace them":
        "새 parent / Boot BOM이 더 이상 관리하지 않아 일부 의존성을 as-is parent가 관리하던 버전으로 고정했다"
        "(인수인계의 ⚠) — 담당 팀이 확인하거나 교체해야 한다",
    "legacy source encoding detected; project.build.sourceEncoding was set accordingly":
        "레거시 소스 인코딩을 감지해 project.build.sourceEncoding을 그에 맞게 설정했다",
    "the run was stopped on purpose after this phase (--stop-after)":
        "이 단계 뒤에서 의도적으로 실행을 멈췄다(--stop-after)",
    "the run stopped early (see Blocked / follow-ups)": "실행이 일찍 멈췄다('차단됨' / 후속 조치 참조)",
    # ---- inventory.md (`sbmig inventory`, sbmig/report/inventory.py)
    "# Webapp inventory — {name}": "# 웹앱 인벤토리 — {name}",
    "Read-only scan of a directory without a pom (scanned {at}). Nothing was changed.":
        "pom이 없는 디렉터리를 읽기 전용으로 스캔했다({at}). 아무것도 변경하지 않았다.",
    "| Item | Value |": "| 항목 | 값 |",
    "| Root | `{root}` |": "| 루트 | `{root}` |",
    "| Webapp roots | {n} |": "| 웹앱 루트 | {n} |",
    "| JSP files | {n} |": "| JSP 파일 | {n} |",
    "| Source roots / .java files | {roots} / {files} |": "| 소스 루트 / .java 파일 | {roots} / {files} |",
    "| Jars known / unknown / ignored | {known} / {unknown} / {ignored} |":
        "| jar 좌표 확인 / 미확인 / 무시 | {known} / {unknown} / {ignored} |",
    "| Class files (Java level) | {n} ({java}) |": "| 클래스 파일 (Java 레벨) | {n} ({java}) |",
    "| Duplicate classes | {n} |": "| 중복 클래스 | {n} |",
    "| Build files found | {files} |": "| 발견된 빌드 파일 | {files} |",
    "| Warnings | {n} |": "| 경고 | {n} |",
    "## Warnings": "## 경고",
    "## Webapp roots": "## 웹앱 루트",
    "No directory with a `WEB-INF` was found.": "`WEB-INF`가 있는 디렉터리를 찾지 못했다.",
    "| web.xml | {path} (Servlet {spec}) |": "| web.xml | {path} (Servlet {spec}) |",
    "| Servlets / filters / listeners / taglibs | {s} / {f} / {l} / {t} |":
        "| 서블릿 / 필터 / 리스너 / taglib | {s} / {f} / {l} / {t} |",
    "| Frameworks | {fw} |": "| 프레임워크 | {fw} |",
    "| JNDI resources | {names} |": "| JNDI 리소스 | {names} |",
    "| JSP files | {n} {sample} |": "| JSP 파일 | {n} {sample} |",
    "| Static files | {n} |": "| 정적 파일 | {n} |",
    "| Spring XML | {files} |": "| Spring XML | {files} |",
    "| WEB-INF/lib jars (ignored files) | {n} ({ignored}) |": "| WEB-INF/lib jar (무시한 파일) | {n} ({ignored}) |",
    "| WEB-INF/classes | {n} class file(s), Java {java} |": "| WEB-INF/classes | 클래스 파일 {n}개, Java {java} |",
    "| Classes without source | {n} {sample} |": "| 소스가 없는 클래스 | {n} {sample} |",
    "## Jars": "## jar 목록",
    "No jars under `WEB-INF/lib`.": "`WEB-INF/lib`에 jar가 없다.",
    "| Jar | Version | Coordinates guess | sha1 | Note |": "| jar | 버전 | 추정 좌표 | sha1 | 메모 |",
    "backup: Tomcat does not load it (left out of the pom)": "백업 파일: Tomcat이 로드하지 않음(pom에서 제외)",
    "unknown coordinates": "좌표 미확인",
    "Other files under `WEB-INF/lib` (ignored): {files}": "`WEB-INF/lib`의 기타 파일(무시): {files}",
    "## Source roots": "## 소스 루트",
    "No `.java` files were found.": "`.java` 파일을 찾지 못했다.",
    "| Source root | Files | Packages | Encoding | javax imports | Frameworks | JDK internals | Webapp |":
        "| 소스 루트 | 파일 | 패키지 | 인코딩 | javax import | 프레임워크 | JDK 내부 API | 웹앱 |",
    "{n} class(es) exist in more than one source root: {sample}": "소스 루트 여러 곳에 있는 클래스 {n}개: {sample}",
    "## Maven module proposal": "## Maven 모듈 제안",
    "Nothing to propose (no webapp root and no sources).": "제안할 것이 없다(웹앱 루트도 소스도 없음).",
    "A guess for the pom synthesis — change it if the split is wrong.":
        "pom 생성을 위한 추정이다 — 분할이 틀렸다면 수정할 것.",
    "| Module | Packaging | Webapp dir | Source dirs | Java | Jars known / unknown |":
        "| 모듈 | 패키징 | 웹앱 디렉터리 | 소스 디렉터리 | Java | jar 확인 / 미확인 |",
    "## What to do next": "## 다음 할 일",
    "1. Check the coordinates of the unknown jars and add the company ones to "
    "`knowledge/libraries/jar_names.yml` (stem → groupId:artifactId), then re-run `sbmig inventory`.":
        "1. 좌표 미확인 jar의 좌표를 확인하고 사내 jar는 `knowledge/libraries/jar_names.yml`에 추가한 뒤"
        "(stem → groupId:artifactId) `sbmig inventory`를 다시 실행한다.",
    "2. Confirm the module split above (one module per webapp root; shared sources need a common jar).":
        "2. 위 모듈 분할을 확인한다(웹앱 루트마다 모듈 하나; 공유 소스는 common jar가 필요).",
    "3. Round 2: `sbmig synthesize-pom` will write the poms from `inventory.json` (not available yet).":
        "3. 2차: `sbmig synthesize-pom`이 `inventory.json`으로 pom을 작성한다(아직 없음).",
}}

# Handover data lines (English in report.json) → Korean, matched against the whole line. Unknown lines (manual
# follow-ups from other modules, advisory text from the knowledge YAML) stay as they are.
PATTERNS: dict[str, list[tuple[re.Pattern[str], str]]] = {"ko": [(re.compile(p), t) for p, t in (
    (r"^(?P<n>\d+) cluster\(s\) blocked — see Blocked$", r"클러스터 \g<n>개 차단됨 — '차단됨' 참조"),
    (r"^(?P<n>\d+) unresolved error\(s\) — see Unresolved errors$", r"미해결 오류 \g<n>건 — '미해결 오류' 참조"),
    ((r"^test sources do not compile yet \(main code builds with -Dmaven\.test\.skip=true\) — fix the tests "
      r"before relying on CI$"),
     (r"테스트 소스가 아직 컴파일되지 않는다(메인 코드는 -Dmaven.test.skip=true로 빌드됨) — CI에 의존하기 전에 "
      r"테스트를 고칠 것")),
    ((r"^(?P<k>\S+) pinned at (?P<v>\S+) — the new parent/Boot BOM no longer manages it: confirm the version or "
      r"replace$"),
     r"\g<k>: \g<v>(으)로 고정함 — 새 parent/Boot BOM이 더 이상 관리하지 않음: 버전을 확인하거나 교체할 것"),
    ((r"^(?P<n>\d+) dependencies are pinned at their as-is versions because the new parent / Boot BOM no longer "
      r"manages them \(⚠ in Versions replaced\) — confirm or replace each; the first (?P<m>\d+):$"),
     (r"새 parent / Boot BOM이 더 이상 관리하지 않아 의존성 \g<n>개를 as-is 버전으로 고정함('교체된 버전'의 ⚠) — "
      r"각각 확인하거나 교체할 것. 처음 \g<m>개:")),
    ((r"^code rewritten by the LLM for (?P<lib>[^:]+): (?P<rest>.+) — review these changes \(changes\.patch, "
      r"attempts table\)$"),
     r"LLM이 \g<lib> 때문에 재작성한 코드: \g<rest> — 변경 내용을 검토할 것 (changes.patch, '수정 시도' 표)"),
    (r"^verify (?P<lib>.+?) → (?P<repl>.+?) \((?P<via>recipe .+?|replaced during the run)\)(?P<rest>: .+)?$",
     r"확인 필요: \g<lib> → \g<repl> (\g<via>)\g<rest>"),
    ((r"^(?P<n>\d+) externalized setting\(s\) need values before the first start — placeholders "
      r"\((?P<s>[^)]*)\) in (?P<files>.+); see 'Properties to set'$"),
     (r"외부 설정 \g<n>개는 첫 기동 전에 값이 필요하다 — 플레이스홀더(\g<s>) 위치: \g<files>; "
      r"'실행 전에 설정할 프로퍼티' 참조")),
    ((r"^(?P<n>\d+) externalized setting\(s\) need values before the first start — no placeholders were "
      r"written \(see the table\); see 'Properties to set'$"),
     (r"외부 설정 \g<n>개는 첫 기동 전에 값이 필요하다 — 플레이스홀더는 기록하지 않았다(표 참조); "
      r"'실행 전에 설정할 프로퍼티' 참조")),
    (r"^… and (?P<n>\d+) more — see Manual follow-ups / Blocked / Unresolved errors$",
     r"… 외 \g<n>건 — '수동 후속 조치' / '차단됨' / '미해결 오류' 참조"),
    (r"^(?P<p>.+): kept the copy from (?P<k>.+?), dropped (?P<d>.+?) \(saved: (?P<s>.*)\)$",
     r"\g<p>: \g<k> 모듈의 사본을 유지, \g<d> 사본은 제외 (보관: \g<s>)"),
    (r"^(?P<n>\d+) identical duplicate\(s\) dropped \(same content in every module\): (?P<names>.+)$",
     r"동일한 중복 \g<n>개 제외(모든 모듈에서 내용이 같음): \g<names>"),
    (r"^as-is effective dependencyManagement unavailable — versions the as-is parent managed are shown as '-'$",
     r"as-is effective dependencyManagement를 얻지 못했다 — as-is parent가 관리하던 버전은 '-'로 표시"),
    (r"^handover unavailable: (?P<x>.+)$", r"인수인계 정보를 만들지 못함: \g<x>"),
    (r"^(?P<n>\d+) javax API jar\(s\) still on the classpath — see that section$",
     r"클래스패스에 남은 javax API jar \g<n>개 — '클래스패스에 남은 javax API jar' 절 참조"),
    ((r"^(?P<n>\d+) javax API jar\(s\) still on the classpath after the migration \(see the report section\) — "
      r"the libraries that drag them in need Jakarta builds: (?P<libs>.+)$"),
     (r"마이그레이션 후에도 javax API jar \g<n>개가 클래스패스에 남아 있다('클래스패스에 남은 javax API jar' 절 참조) — "
      r"이를 끌어오는 라이브러리에 Jakarta 빌드가 필요하다: \g<libs>")),
)]}


def normalize(lang: object) -> str:
    """A supported language code; anything else (None, typo) → "en"."""
    s = str(lang or "en").strip().lower()
    return s if s in LANGUAGES else "en"


def tr(lang: str, text: str, **fmt: object) -> str:
    """Translation of `text` for `lang` (English fallback), formatted with `fmt` when given."""
    s = text
    if lang != "en":
        s = STRINGS.get(lang, {}).get(text) or text
    return s.format(**fmt) if fmt else s


def tr_line(lang: str, line: str) -> str:
    """A Handover data line (see `PATTERNS`) in `lang`; unchanged for `en` and for lines no pattern knows."""
    if lang == "en":
        return line
    for rx, tmpl in PATTERNS.get(lang, []):
        m = rx.match(line)
        if m:
            return m.expand(tmpl)
    return line
