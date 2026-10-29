"""report.md 의 언어(``report.language: en | ko``) — 의존성 없는 번역 문자열 표와 도우미 함수.
흐름상 위치: markdown.py 가 report.md 를 그릴 때 ``tr`` / ``tr_line`` 으로 문장을 옮긴다(report.json 은 영어
그대로). 핵심 진입점: ``tr``(영어 원문 = 표의 키, 없으면 영어로 fallback), ``tr_line``(인수인계 데이터 줄을
``PATTERNS`` 정규식으로 통째 매칭해 번역), ``normalize``(언어 코드 정리). 주의: 키가 영어 원문이므로
markdown.py / handover.py 의 영어 문장을 바꾸면 그 번역은 조용히 영어로 돌아간다 — 문장을 바꿀 때는 이 표의
키도 같이 바꿔야 한다. 식별자·경로·Maven / javac 메시지·루프의 자유 문장은 번역하지 않는다.

report.md languages (`report.language: en | ko`). Dependency-free string table + helpers.

`tr(lang, text, **fmt)` — `text` is the English source string (it is also the table key, `{name}` placeholders
filled by `str.format`); a missing translation falls back to the English text, so `en` output never changes.
`tr_line(lang, line)` — lines the Handover builder stores in report.json (from-here items, merge-conflict lines,
handover notes) stay English in the data; for `ko` the renderer maps the known templates through `PATTERNS`.
Identifiers, paths, Maven / javac messages and free-text notes from the loop are never translated.


"""
from __future__ import annotations

import re

LANGUAGES = ("en", "ko")

# 영어 원문 → 한국어. 키는 markdown.py 의 tr(...) 호출에 쓰인 영어 문자열과 글자 하나까지 같아야 한다
STRINGS: dict[str, dict[str, str]] = {"ko": {
    # ---- section titles
    "Blocked — needs human / library action": "차단됨 — 사람 / 라이브러리 조치 필요",
    "Handover — what we replaced, what is yours": "인수인계 — 교체한 것과 이제 팀이 맡을 것",
    "Properties to set before running": "실행 전에 설정할 프로퍼티",
    "# Migration report — {name}": "# 마이그레이션 보고서 — {name}",
    "## Manual follow-ups": "## 수동 후속 조치",
    "## Flags": "## 플래그",
    "## What we changed": "## 변경 내역",
    "## LLM usage and cost": "## LLM 사용량과 비용",
    "fix mode: edits — no fix scripts generated, stored or reused":
        "수정 방식: edits — 수정 스크립트를 만들지도, 저장·재사용하지도 않음",
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
    # ---- v2.1d operator checks (major upgrades / library replacements)
    "### Operator checks ({n} major upgrades)": "### 운영자 확인 필요 (메이저 업그레이드 {n}건)",
    "The build passes, but these changes cross a major version line or replace a library: check the runtime "
    "behaviour (configuration, registrations, renamed packages) before you ship.":
        "빌드는 통과했지만 아래 변경은 메이저 버전을 넘었거나 라이브러리를 바꿨다: 배포 전에 실행 시 동작(설정, 등록, "
        "바뀐 패키지 이름)을 꼭 확인할 것.",
    "case `{case}`": "케이스 `{case}`",
    "structure review": "구조 점검",
    "commit `{sha}`": "커밋 `{sha}`",
    " — approved ({src})": " — 승인됨 ({src})",
    "… {n} more (report.json)": "… 외 {n}줄 (report.json)",
    "(no guide recorded)": "(가이드 없음)",
    "guide source: LLM": "가이드 출처: LLM",
    "guide source: sbmig (synthesized)": "가이드 출처: sbmig 자동 생성",
    "⚠ major ({n}, see Operator checks)": "⚠ 메이저 ({n}건, \"운영자 확인 필요\" 참조)",
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
    "| **Error list** | {n} error(s): {counts} (see \"Error list\") |": "| **에러 목록** | {n}건: {counts} (\"에러 목록\" 참조) |",
    "| To-be | {versions} |": "| 전환 후 | {versions} |",
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
    "No error list was kept for this run; {n} error(s) were left after the last build, grouped by file:":
        "이번 실행은 에러 목록을 남기지 않았다. 마지막 빌드 후 남은 오류 {n}건 (파일별):",
    "module {module}": "모듈 {module}",
    "artifact {coords}": "아티팩트 {coords}",
    "(no file)": "(파일 없음)",
    " — symbol: `{symbol}`": " — 심볼: `{symbol}`",
    " — location: {location}": " — 위치: {location}",
    "Last build log: `{path}`": "마지막 빌드 로그: `{path}`",
    # ---- v2 structure review (sbmig.review)
    "Structure review": "구조 점검",
    "| Structure review | {summary} |": "| 구조 점검 | {summary} |",
    "After the restructure and before the first build, the LLM checked the poms, dependencies and module layout "
    "against the company rules. It fixes critical issues itself (committed, revertible) and leaves a script for the "
    "KB; warnings are only recorded.":
        "구조화 뒤, 첫 빌드 전에 LLM이 pom · 의존성 · 모듈 배치를 회사 규칙에 비춰 점검했다. 심각한 문제는 LLM이 직접 "
        "고치고(커밋, 되돌릴 수 있음) KB용 스크립트를 남긴다. 경고는 기록만 한다.",
    "- solution: {solution}": "- 해결 방법: {solution}",
    "- direct edits in the answer: {n}": "- 답변의 직접 수정: {n}건",
    "- script for the KB: yes": "- KB용 스크립트: 있음",
    "- script in the answer: not stored (fix.mode edits)": "- 답변의 스크립트: 저장 안 함 (fix.mode edits)",
    "- script for the KB: none — {reason}": "- KB용 스크립트: 없음 — {reason}",
    "- changed: {files}": "- 변경: {files}",
    "- verdict: `{verdict}` — {summary}": "- 판정: `{verdict}` — {summary}",
    "- model: `{model}`, {calls} LLM call(s)": "- 모델: `{model}`, LLM {calls}회 호출",
    "| severity | file | problem | fix hint |": "| 심각도 | 파일 | 문제 | 수정 힌트 |",
    "critical": "심각",
    "warning": "경고",
    "(project)": "(프로젝트 전체)",
    "… {n} more issue(s) — see `structure_review` in report.json":
        "… 외 {n}건 — report.json의 `structure_review` 참조",
    "Analysis: {analysis}": "분석: {analysis}",
    "skipped ({note})": "건너뜀 ({note})",
    "failed — no usable answer, the build went ahead ({note})": "실패 — 쓸 수 있는 답이 없어 점검 없이 빌드함 ({note})",
    "ok — no issues": "ok — 문제 없음",
    "{c} critical, {w} warning(s)": "심각 {c}건, 경고 {w}건",
    "fix {status}, changes restored": "수정 {status}, 변경 되돌림",
    "fix {status}, {n} file(s) changed (commit `{commit}`)": "수정 {status}, 파일 {n}개 변경 (커밋 `{commit}`)",
    "fix {status}, {n} file(s) changed": "수정 {status}, 파일 {n}개 변경",
    "fix not applied": "수정 적용 안 함",
    "no fix": "수정 없음",
    "- fix (edits / script): `{status}`, {n} file(s) changed{files}":
        "- 수정 (edits / script): 결과 `{status}`, 파일 {n}개 변경{files}",
    "- commit: `{commit}` (revert it to undo the review's changes)":
        "- 커밋: `{commit}` (구조 점검의 변경을 취소하려면 이 커밋을 되돌린다)",
    "- the fix's changes were restored: they are not in the result": "- 수정한 내용은 되돌렸다: 결과물에 없음",
    "- fix error: {error}": "- 수정 오류: {error}",
    "- fix log: {lines}": "- 수정 로그: {lines}",
    "- fix (edits / script): not applied": "- 수정 (edits / script): 적용하지 않음",
    "- no fix for the critical issue(s): they are handed over": "- 심각한 문제에 대한 수정 없음: 인수인계로 넘김",
    "- note: {note}": "- 메모: {note}",
    # ---- v2.1 error list + fixes by case
    "Error list": "에러 목록",
    "Fixes by case": "케이스별 해결",
    "compile — `mvn compile` after OpenRewrite": "compile — OpenRewrite 뒤 `mvn compile`",
    "restructure — pom corrections, verified by the next build": "restructure — 구조화의 pom 정정, 다음 빌드에서 검증",
    "review — structure review": "review — 구조 점검",
    "build — first `mvn clean package`, new errors only": "build — 첫 `mvn clean package`, 새 에러만",
    "{seen} — new errors of round {n}'s build": "{seen} — 라운드 {n} 빌드의 새 에러",
    "{n} error(s) in this run, one line each (all of them with every try and diff: `errors.md` / `errors.jsonl` in "
    "the run directory): {counts}.":
        "이번 실행의 에러 {n}건, 한 줄에 하나(시도와 diff까지 전부: 실행 디렉터리의 `errors.md` / `errors.jsonl`): "
        "{counts}.",
    "| # | case | file:line | message | status | tries | resolved |": "| # | 케이스 | 파일:줄 | 메시지 | 상태 | 시도 | 해결 |",
    "… {n} more in this group — see `errors.md`": "… 이 그룹에 {n}건 더 — `errors.md` 참조",
    "| case | errors | cause | solution | changes (files, +/−) | edits | script |":
        "| 케이스 | 에러 | 원인 | 해결 방법 | 변경 파일 (+/−) | 수정 | 스크립트 |",
    "| case | errors | cause | solution | changes (files, +/−) | edits | fix try |":
        "| 케이스 | 에러 | 원인 | 해결 방법 | 변경 파일 (+/−) | 수정 | 수정 시도 |",
    "{resolved}/{n} resolved": "{resolved}/{n} 해결",
    "(no solution text) {reason}": "(해결 설명 없음) {reason}",
    "no script: {reason}": "스크립트 없음: {reason}",
    "… {n} more case(s) — see `errors.md`": "… 케이스 {n}개 더 — `errors.md` 참조",
    "Stored scripts live in the KB (`<kb>/cases/<case>/fix_vN.py`); every try with its diff is in `errors.md`, the "
    "whole change set in `changes.patch`.":
        "저장된 스크립트는 KB(`<kb>/cases/<case>/fix_vN.py`)에 있다. 시도별 diff는 `errors.md`에, 전체 변경은 "
        "`changes.patch`에 있다.",
    "fix.mode edits: no fix scripts; `edits:<case>#<n>` is try n of the case in `errors.md` (with its diff), the "
    "whole change set is in `changes.patch`.":
        "fix.mode edits: 수정 스크립트 없음. `edits:<case>#<n>` 은 `errors.md` 에 있는 그 케이스의 n번째 시도(diff 포함)이고, "
        "전체 변경은 `changes.patch`에 있다.",
    # ---- what we changed
    "Plan steps executed:": "실행한 계획 단계:",
    "OpenRewrite runs:": "OpenRewrite 실행:",
    "| phase | recipes | changed files | ok |": "| 단계 | 레시피 | 변경 파일 | 성공 |",
    # v2.1h (A5): the JSP javax → jakarta rename of the restructure
    "JSP javax → jakarta: {files}, {refs}": "JSP javax → jakarta: {files}, {refs}",
    "{n} file": "파일 {n}개",
    "{n} files": "파일 {n}개",
    "{n} reference": "참조 {n}곳",
    "{n} references": "참조 {n}곳",
    " — renamed by the restructure in JSP / tag files (page imports, useBean, scriptlets); OpenRewrite does not parse "
    "JSPs":
        " — 재구성이 JSP / 태그 파일(page import, useBean, 스크립틀릿)에서 바꿨다. OpenRewrite 는 JSP 를 읽지 않는다",
    # v2.1j: the intake's pom synthesis for an Ant project ("What we changed" + the Handover block)
    "pom.xml synthesized from build.xml ({n} jars: {a} pom.properties, {b} table, {c} lookup, {d} unknown)":
        "build.xml 에서 pom.xml 합성 (jar {n}개: pom.properties {a} · 이름표 {b} · 조회 {c} · 미상 {d})",
    "### Ant project: pom.xml synthesized": "### Ant 프로젝트: pom.xml 합성",
    "The project had an Ant `{build}` and no pom.xml: sbmig wrote `{pom}` at intake from build.xml and the "
    "directories (commit `intake: pom.xml synthesized from build.xml`); build.xml itself is left as it was.":
        "Ant `{build}` 만 있고 pom.xml 이 없는 프로젝트라 sbmig 가 접수에서 build.xml 과 폴더 구조로 `{pom}` 을 "
        "만들었다(커밋 `intake: pom.xml synthesized from build.xml`). build.xml 은 그대로 두었다.",
    "- pom: `{gav}` ({packaging}), Java {java}, encoding {enc}": "- pom: `{gav}` ({packaging}), Java {java}, 인코딩 {enc}",
    "- layout: sources {src}, web root {web}, resources {res}, lib {lib}":
        "- 구조: 소스 {src}, 웹 루트 {web}, 리소스 {res}, lib {lib}",
    "- jars: {n} — {a} pom.properties, {b} name table, {c} repository lookup, {d} unknown":
        "- jar: {n}개 — pom.properties {a} · 이름표 {b} · 저장소 조회 {c} · 미상 {d}",
    "**{n} jar(s) without Maven coordinates** — declared with `system` scope (`<systemPath>`): the build passes, but "
    "system-scope jars are not packaged into the WAR / boot jar, so the application fails at runtime until each one "
    "is installed into Nexus and its pom entry is replaced by a normal dependency (drop `<scope>system</scope>` and "
    "`<systemPath>`):":
        "**Maven 좌표를 찾지 못한 jar {n}개** — `system` scope(`<systemPath>`)로 선언했다: 빌드는 통과하지만 system "
        "scope jar 는 WAR / boot jar 에 들어가지 않아 실행 시 실패한다 — 각각 Nexus 에 설치하고 pom 항목을 일반 "
        "의존성으로 바꿀 것(`<scope>system</scope>` 과 `<systemPath>` 삭제):",
    "**{n} jar(s) without Maven coordinates** — not declared (`intake.unknown_jar: skip`): the build shows which "
    "ones the code needs; install each into Nexus and add it to the pom as a dependency:":
        "**Maven 좌표를 찾지 못한 jar {n}개** — 선언하지 않았다(`intake.unknown_jar: skip`): 코드가 필요로 하는 것은 "
        "빌드가 보여 준다. 각각 Nexus 에 설치하고 pom 에 의존성으로 추가할 것:",
    "(`-Dfile` paths are those of the as-is tree; publishing to Nexus is `mvn deploy:deploy-file` with the same "
    "coordinates plus `-Durl` / `-DrepositoryId`.)":
        "(`-Dfile` 경로는 as-is 트리 기준이다. Nexus 에 올릴 때는 같은 좌표로 `mvn deploy:deploy-file` 에 `-Durl` / "
        "`-DrepositoryId` 를 더한다.)",
    "Left out (no coordinates and no source imports their packages): {jars} — add one back if the build or the "
    "runtime needs it.":
        "뺀 jar(좌표도 없고 어떤 소스도 그 패키지를 import 하지 않음): {jars} — 빌드나 실행에 필요하면 다시 추가할 것.",
    "build.xml steps not translated to Maven — check whether the build still needs them:":
        "Maven 으로 옮기지 못한 build.xml 단계 — 빌드에 아직 필요한지 확인할 것:",
    "Synthesis warnings:": "합성 경고:",
    "yes": "예",
    "no: {error}": "아니오: {error}",
    # ---- notes
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
    "| library | as-is | status | what we did | outcome | what you do next | docs |":
        "| 라이브러리 | as-is | 상태 | 수행한 조치 | 결과 | 다음 할 일 | 문서 |",
    "✅ replaced": "✅ 교체됨",
    "⚠ still present": "⚠ 남아 있음",
    "replaced during the run": "실행 중 교체됨",
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
    "recipe / fix script": "레시피 / 수정 스크립트",
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
    "`mvn -v` reported another Java version than `build.require_java_major`: set build.java_home / JAVA_HOME to a "
    "JDK 21 and run again":
        "`mvn -v`가 `build.require_java_major`와 다른 Java 버전을 보고했다 — build.java_home / JAVA_HOME을 JDK 21로 "
        "맞추고 다시 실행할 것",
    "the LLM is not configured (LLM_BASE_URL / LLM_API_KEY / LLM_MODEL in .env, or the gateway rejected the key / "
    "model): LLM fixes were skipped — check with `sbmig llm-probe`":
        "LLM 설정이 안 되어 있다(.env의 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL, 또는 게이트웨이가 키 / 모델을 거부함) "
        "— LLM 수정은 건너뛰었다: `sbmig llm-probe`로 확인할 것",
    "a fix raised a dependency across its major version or replaced a library (the build passes): the operator "
    "verifies the runtime behaviour — see \"Operator checks\" in the Handover":
        "수정이 의존성을 메이저 버전 너머로 올렸거나 라이브러리를 바꿨다(빌드는 통과): 운영자가 실행 시 동작을 확인할 것 — "
        "인수인계의 \"운영자 확인 필요\" 참조",
    "the project had an Ant build.xml and no pom.xml: sbmig synthesized a pom.xml from build.xml and the directories "
    "at intake (intake.ant_to_maven) and migrated it as a Maven project — see \"Ant project: pom.xml synthesized\" "
    "in the Handover":
        "Ant build.xml 만 있고 pom.xml 이 없는 프로젝트라 sbmig 가 접수에서 build.xml 과 폴더 구조로 pom.xml 을 "
        "합성하고(intake.ant_to_maven) Maven 프로젝트로 이관했다 — 인수인계의 \"Ant 프로젝트: pom.xml 합성\" 참조",
    "jars of the Ant project have no Maven coordinates (no pom.properties inside, not in jar_names.yml, not found by "
    "sha1 in the repository): declared with `system` scope (intake.unknown_jar) the build passes, but they are not "
    "packaged into the WAR — install them into Nexus and replace the pom entries (commands in the Handover)":
        "Ant 프로젝트의 jar 중 Maven 좌표를 찾지 못한 것이 있다(jar 안에 pom.properties 없음, jar_names.yml 에 없음, "
        "저장소 sha1 조회 실패): `system` scope 로 선언하면(intake.unknown_jar) 빌드는 통과하지만 WAR 에 들어가지 "
        "않는다 — Nexus 에 설치하고 pom 항목을 바꿀 것(명령은 인수인계에)",
    "the run ended for an infrastructure reason (LLM gateway, Maven, JDK, repository / Nexus, disk), not because of "
    "the project: fix the environment and run it again (a fleet re-queues the project)":
        "프로젝트가 아니라 인프라(LLM 게이트웨이, Maven, JDK, 저장소 / Nexus, 디스크) 문제로 실행이 끝났다 — 환경을 "
        "고치고 다시 실행할 것(플릿은 이 프로젝트를 다시 대기열에 넣는다)",
    "the project had no pom.xml at its root but sibling poms that depend on each other (e.g. APP/BATCH, APP/WEB, APP/EAR): sbmig generated a root aggregator pom so they build in one reactor and are merged into the company layout (layout.generate_aggregator)":
        "프로젝트 루트에 pom.xml이 없고 서로 의존하는 pom들(예: APP/BATCH, APP/WEB, APP/EAR)만 있어 sbmig가 루트 집합 pom을 생성했다 — 한 reactor로 함께 빌드하고 회사 레이아웃으로 병합한다(layout.generate_aggregator)",
    "the input was an EAR pom whose modules are not in the directory (e.g. APP/EAR alone): run sbmig on the directory that contains every module":
        "입력이 EAR pom이고 그 EAR이 묶는 모듈이 디렉터리에 없다(예: APP/EAR만 넣음) — 모든 모듈이 들어 있는 상위 디렉터리로 다시 실행할 것",
    "an EAR module (application-server packaging) was dropped: its modules were merged and the boot module is deployed instead; descriptors are kept under asis-ear/ and the context root is a follow-up":
        "EAR 모듈(애플리케이션 서버 패키징)을 없앴다: 모듈은 병합되고 boot 모듈이 대신 배포된다. 배포 디스크립터는 asis-ear/에 보관하고 context root는 후속 조치로 남겼다",
    "a WAR dependency (maven-war-plugin overlay) was moved from the jar modules to the boot WAR — the overlay WAR must be resolvable from your repositories":
        "WAR 의존성(maven-war-plugin overlay)을 jar 모듈에서 boot WAR로 옮겼다 — overlay WAR는 설정된 저장소(사내 Nexus)에서 받을 수 있어야 한다",
    "web content that no pom references (e.g. a STATIC/ folder with JSPs / downloads) was left in place — serve it as before or move it into the front module":
        "어떤 pom도 참조하지 않는 웹 콘텐츠(예: JSP·다운로드 파일이 있는 STATIC/)는 그대로 두었다 — 예전처럼 웹 서버에서 내보내거나 front 모듈로 옮길 것",
    "OpenRewrite could not parse some source files (see the phase notes / rewrite log): recipes that need type "
    "information skipped them; the fix loop covered what the build found":
        "OpenRewrite가 일부 소스 파일을 파싱하지 못함(phase 노트 / rewrite 로그 참고): 타입 정보가 필요한 레시피는 그 "
        "파일을 건너뛰었고, 빌드가 찾아낸 것은 수정 루프가 처리했다",
    "web.xml servlets / filters / listeners / error pages were turned into a generated WebXmlRegistrations "
    "@Configuration in the boot module (web.xml itself is not read by Spring Boot) — verify the mappings in the "
    "Handover":
        "web.xml의 서블릿 / 필터 / 리스너 / 오류 페이지를 boot 모듈에 생성한 WebXmlRegistrations @Configuration으로 "
        "옮겼다(Spring Boot는 web.xml 자체를 읽지 않음) — 인수인계에서 매핑을 확인할 것",
    "javax EE API jars are still on the resolved classpath after the migration (section \"javax API jars still on "
    "the classpath\"): the direct dependencies that drag them in are compiled against javax and need Jakarta builds":
        "마이그레이션 후에도 javax EE API jar가 해석된 클래스패스에 남아 있다(\"클래스패스에 남은 javax API jar\" 절): "
        "이를 끌어오는 직접 의존성이 javax 기준으로 컴파일되어 있어 Jakarta 빌드가 필요하다",
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
    "remove the declaration from the pom": "pom에서 선언 제거",
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
    "the LLM failed twice in a row (transport/config); the cases still needing a fix script are listed as blocked "
    "(llm_unavailable) — stored scripts and the final build still ran":
        "LLM이 연속 두 번 실패했다(전송/설정). 아직 수정 스크립트가 필요한 케이스는 차단(llm_unavailable)으로 "
        "표시했다 — 저장된 스크립트와 최종 빌드는 그대로 실행했다",
    "the configured in-house parent POM is not resolvable — nothing can build until it is (see Blocked)":
        "설정한 사내 parent POM을 해석할 수 없다 — 해결될 때까지 아무것도 빌드되지 않는다('차단됨' 참조)",
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

# 인수인계 데이터 줄(report.json 에는 영어로 저장)을 한국어로. 정규식은 handover.py 가 만드는 영어 줄 전체와
# 맞아야 한다 — handover.py 의 문장을 바꾸면 여기 패턴도 함께 바꿀 것(안 맞으면 영어 그대로 나간다)
# Handover data lines (English in report.json) → Korean, matched against the whole line. Unknown lines (manual
# follow-ups from other modules, advisory text from the knowledge YAML) stay as they are.
PATTERNS: dict[str, list[tuple[re.Pattern[str], str]]] = {"ko": [(re.compile(p), t) for p, t in (
    (r'^operator checks: (?P<n>\d+) major upgrade\(s\) — see "Operator checks" above$',
     r'운영자 확인 필요: 메이저 업그레이드 \g<n>건 — 위 "운영자 확인 필요" 참조'),
    (r"^(?P<n>\d+) cluster\(s\) blocked — see Blocked$", r"클러스터 \g<n>개 차단됨 — '차단됨' 참조"),
    (r"^(?P<n>\d+) unresolved error\(s\) — see Error list$", r"미해결 오류 \g<n>건 — '에러 목록' 참조"),
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
      r"Fixes by case\)$"),
     r"LLM이 \g<lib> 때문에 재작성한 코드: \g<rest> — 변경 내용을 검토할 것 (changes.patch, '케이스별 해결' 표)"),
    (r"^verify (?P<lib>.+?) → (?P<repl>.+?) \(replaced during the run\)(?P<rest>: .+)?$",
     r"확인 필요: \g<lib> → \g<repl> (실행 중 교체됨)\g<rest>"),
    ((r"^(?P<n>\d+) externalized setting\(s\) need values before the first start — placeholders "
      r"\((?P<s>[^)]*)\) in (?P<files>.+); see 'Properties to set'$"),
     (r"외부 설정 \g<n>개는 첫 기동 전에 값이 필요하다 — 플레이스홀더(\g<s>) 위치: \g<files>; "
      r"'실행 전에 설정할 프로퍼티' 참조")),
    ((r"^(?P<n>\d+) externalized setting\(s\) need values before the first start — no placeholders were "
      r"written \(see the table\); see 'Properties to set'$"),
     (r"외부 설정 \g<n>개는 첫 기동 전에 값이 필요하다 — 플레이스홀더는 기록하지 않았다(표 참조); "
      r"'실행 전에 설정할 프로퍼티' 참조")),
    (r"^… and (?P<n>\d+) more — see Manual follow-ups / Blocked / Error list$",
     r"… 외 \g<n>건 — '수동 후속 조치' / '차단됨' / '에러 목록' 참조"),
    (r"^(?P<p>.+): kept the copy from (?P<k>.+?), dropped (?P<d>.+?) \(saved: (?P<s>.*)\)$",
     r"\g<p>: \g<k> 모듈의 사본을 유지, \g<d> 사본은 제외 (보관: \g<s>)"),
    (r"^(?P<n>\d+) identical duplicate\(s\) dropped \(same content in every module\): (?P<names>.+)$",
     r"동일한 중복 \g<n>개 제외(모든 모듈에서 내용이 같음): \g<names>"),
    (r"^as-is effective dependencyManagement unavailable — versions the as-is parent managed are shown as '-'$",
     r"as-is effective dependencyManagement를 얻지 못했다 — as-is parent가 관리하던 버전은 '-'로 표시"),
    (r"^handover unavailable: (?P<x>.+)$", r"인수인계 정보를 만들지 못함: \g<x>"),
    # v2 structure review follow-ups (handover.review_followups)
    ((r"^structure review fix \(edits / script\) changed (?P<n>\d+) file\(s\) for (?P<k>\d+) critical issue\(s\): "
      r"(?P<files>.+) \(commit (?P<c>[^)]+)\) — check these pom changes$"),
     r"구조 점검 수정(edits / script)이 심각한 문제 \g<k>건 때문에 파일 \g<n>개를 바꿈: \g<files> (커밋 \g<c>) — 이 pom 변경을 확인할 것"),
    (r"^structure review: critical issue not fixed — (?P<rest>.+)$", r"구조 점검: 고치지 못한 심각한 문제 — \g<rest>"),
    (r"^structure review warning — (?P<rest>.+)$", r"구조 점검 경고 — \g<rest>"),
    (r"^… and (?P<n>\d+) more structure review issue\(s\) — see Structure review$",
     r"… 외 구조 점검 문제 \g<n>건 — '구조 점검' 참조"),
    # v2.1h (A5): JSPs whose javax EE names were not renamed (handover.jsp_javax_item)
    ((r"^JSP javax references remain \((?P<n>\d+) files?\) — rename the javax EE names in page imports / scriptlets "
      r"/ useBean to jakarta\.\* \(the build passes, Tomcat 10\+ fails at runtime\); `layout\.jsp_jakarta_rename` is "
      r"off$"),
     (r"JSP 에 javax 참조가 남아 있다(파일 \g<n>개) — page import / 스크립틀릿 / useBean 의 javax EE 이름을 jakarta.* 로 "
      r"바꿀 것(빌드는 통과하지만 Tomcat 10+ 에서 실행 시 실패한다); `layout.jsp_jakarta_rename` 이 꺼져 있다")),
    ((r"^JSP javax references remain \((?P<n>\d+) files?\) — rename the javax EE names in page imports / scriptlets "
      r"/ useBean to jakarta\.\* \(the build passes, Tomcat 10\+ fails at runtime\)$"),
     (r"JSP 에 javax 참조가 남아 있다(파일 \g<n>개) — page import / 스크립틀릿 / useBean 의 javax EE 이름을 jakarta.* 로 "
      r"바꿀 것(빌드는 통과하지만 Tomcat 10+ 에서 실행 시 실패한다)")),
    (r"^(?P<n>\d+) javax API jar\(s\) still on the classpath — see that section$",
     r"클래스패스에 남은 javax API jar \g<n>개 — '클래스패스에 남은 javax API jar' 절 참조"),
    ((r"^(?P<n>\d+) javax API jar\(s\) still on the classpath after the migration \(see the report section\) — "
      r"the libraries that drag them in need Jakarta builds: (?P<libs>.+)$"),
     (r"마이그레이션 후에도 javax API jar \g<n>개가 클래스패스에 남아 있다('클래스패스에 남은 javax API jar' 절 참조) — "
      r"이를 끌어오는 라이브러리에 Jakarta 빌드가 필요하다: \g<libs>")),
)]}


def normalize(lang: object) -> str:
    """지원하는 언어 코드로 정리한다. 그 밖의 값(None, 오타)은 "en".

    A supported language code; anything else (None, typo) → "en"."""
    s = str(lang or "en").strip().lower()
    return s if s in LANGUAGES else "en"


def tr(lang: str, text: str, **fmt: object) -> str:
    """``text``(영어 원문)를 ``lang`` 으로 옮기고 ``fmt`` 가 있으면 ``str.format`` 으로 채운다.
    번역이 없으면 영어 그대로 — 그래서 ``en`` 출력은 절대 바뀌지 않는다.

    Translation of `text` for `lang` (English fallback), formatted with `fmt` when given."""
    s = text
    if lang != "en":
        s = STRINGS.get(lang, {}).get(text) or text
    return s.format(**fmt) if fmt else s


def tr_line(lang: str, line: str) -> str:
    """인수인계 데이터 줄 하나를 ``lang`` 으로(``PATTERNS`` 의
    첫 매칭). ``en`` 이거나 아는 패턴이 없으면 그대로.

    A Handover data line (see `PATTERNS`) in `lang`; unchanged for `en` and for lines no pattern knows."""
    if lang == "en":
        return line
    for rx, tmpl in PATTERNS.get(lang, []):
        m = rx.match(line)
        if m:
            return m.expand(tmpl)
    return line
