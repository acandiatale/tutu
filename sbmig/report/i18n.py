"""report.md의 문장을 한국어로 바꾸는 번역 표와 도우미 함수를 담은 모듈(``report.language: en | ko``).
markdown.py와 guide_ko.py가 report.md를 만들 때 ``tr``과 ``tr_line``으로 문장을 번역한다. report.json은 영어 그대로 둔다.
- ``tr``: 영어 원문을 키로 번역을 찾고, 없으면 영어를 그대로 반환한다
- ``tr_line``: 저장된 영어 줄 전체를 ``PATTERNS`` 정규식으로 맞춰 번역한다
- 키가 영어 원문이므로 영어 문장을 바꾸면 이 표의 키나 패턴도 함께 바꿔야 한다. 그러지 않으면 영어로 출력된다
- 한국어는 요청한 담당자에게 쓰는 합니다체 문장으로 쓰고, 식별자, 경로, 명령, 캡처한 값은 그대로 둔다
- Maven / javac 메시지와 LLM이 쓴 자유 문장은 번역하지 않는다

report.md languages (`report.language: en | ko`). Dependency-free string table + helpers.

`tr(lang, text, **fmt)` — `text` is the English source string (it is also the table key, `{name}` placeholders
filled by `str.format`); a missing translation falls back to the English text, so `en` output never changes.
`tr_line(lang, line)` — lines stored in report.json (from-here items, follow-ups, blocked suggestions, abort reasons,
handover notes) stay English in the data; for `ko` the renderer maps the known templates through `PATTERNS`.
Identifiers, paths, Maven / javac messages and free-text notes from the loop are never translated. The Korean is
workplace Korean for the person who asked for the migration (INTERFACES "v2.1z-as", wording).
"""
from __future__ import annotations

import re
from typing import Callable, Union

LANGUAGES = ("en", "ko")

# 영어 원문을 키로 한 한국어 번역 표. 키는 tr(...) 호출에 쓴 영어 문자열과 정확히 같아야 한다
STRINGS: dict[str, dict[str, str]] = {"ko": {
    # ---- section titles
    "Blocked — needs human / library action": "진행하지 않은 항목",
    "Handover — what we replaced, what is yours": "전환 내용과 확인할 사항",
    "Properties to set before running": "실행 전에 설정할 프로퍼티",
    "# Migration report — {name}": "# 마이그레이션 보고서 — {name}",
    "## Manual follow-ups": "## 확인해 주세요",
    "## Flags": "## 플래그",
    "## What we changed": "## 변경 내역",
    "## LLM usage and cost": "## LLM 사용량과 비용",
    "| LLM cost | {cost} (see \"LLM usage and cost\") |": "| LLM 비용 | {cost} (「LLM 사용량과 비용」 참고) |",
    "(+ unpriced: {models})": "(단가 없는 모델: {models})",
    "(token counts missing for some calls)": "(일부 호출은 토큰 수 없음)",
    "| Model | Calls | Input tokens | Output tokens | Price in / out (per 1M) | Cost |":
        "| 모델 | 호출 | 입력 토큰 | 출력 토큰 | 단가 입력 / 출력 (1M 토큰당) | 비용 |",
    "no price configured": "단가 미설정",
    "| (no per-model usage recorded) | {calls} | {inp:,} | {out:,} | - | - |":
        "| (모델별 사용량 기록 없음) | {calls} | {inp:,} | {out:,} | - | - |",
    "| **Total** | | | | | **{total}** |": "| **합계** | | | | | **{total}** |",
    "Prices: {note}.": "단가 기준: {note}.",
    "No price for {models} in `llm.pricing` — not included in the total.":
        "{models} 모델은 `llm.pricing`에 단가가 없어 합계에 넣지 않았습니다.",
    "Some calls came back without token counts (the gateway did not report `usage`); their cost is not included.":
        "일부 호출은 게이트웨이가 토큰 수(`usage`)를 알려 주지 않아 비용에 넣지 않았습니다.",
    "Cost = input tokens × input price + output tokens × output price, per 1M tokens, in {currency}; cached-input "
    "discounts are not visible to sbmig.":
        "비용은 입력 토큰 × 입력 단가 + 출력 토큰 × 출력 단가로 계산했습니다(1M 토큰 기준, {currency}). 캐시 입력 할인은 "
        "sbmig에서 알 수 없어 반영하지 않았습니다.",
    "## Notes": "## 메모",
    "## Outputs": "## 산출물",
    "### Versions replaced": "### 바꾼 버전",
    "### Libraries needing your attention": "### 확인이 필요한 라이브러리",
    "### From here on, your team": "### 담당 팀에서 확인할 사항",
    # ---- v2.1d operator checks (major upgrades / library replacements)
    "### Operator checks ({n} major upgrades)": "### 운영자 확인 필요 (메이저 업그레이드 {n}건)",
    "The build passes, but these changes cross a major version line or replace a library: check the runtime "
    "behaviour (configuration, registrations, renamed packages) before you ship.":
        "빌드는 통과했지만 아래 변경은 메이저 버전을 올렸거나 라이브러리를 바꾼 것입니다. 배포하기 전에 실행 시 동작(설정, "
        "빈 등록, 바뀐 패키지 이름)을 확인해 주세요.",
    "case `{case}`": "수정 기록 `{case}`",
    "structure review": "구조 점검",
    "commit `{sha}`": "커밋 `{sha}`",
    " — approved ({src})": " — {src}에서 승인한 변경",
    "… {n} more (report.json)": "… 외 {n}건 (report.json 참고)",
    "(no guide recorded)": "(남긴 안내 없음)",
    "guide source: LLM": "안내 작성: LLM",
    "guide source: sbmig (synthesized)": "안내 작성: sbmig 자동 생성",
    "⚠ major ({n}, see Operator checks)": "⚠ 메이저 버전 변경 {n}건 (「운영자 확인 필요」 참고)",
    # ---- header table
    "✅ BUILD SUCCESS": "✅ 빌드 성공",
    "❌ NOT BUILDING": "❌ 빌드 실패",
    "| **Result** | {ok} (final `mvn clean package`) |": "| **결과** | {ok} (최종 `mvn clean package`) |",
    "| Case | `{case}` |": "| 프로젝트 유형 | `{case}` |",
    "| As-is | java {java}, spring boot {boot}, spring {spring}, {poms} pom(s) |":
        "| 전환 전 | Java {java}, Spring Boot {boot}, Spring {spring}, pom {poms}개 |",
    "| Run id | `{run_id}` |": "| 실행 ID | `{run_id}` |",
    "| Started / finished | {started} / {finished} ({duration}) |":
        "| 시작 / 종료 | {started} / {finished} ({duration}) |",
    "| LLM | {calls} call(s), {inp} in / {out} out tokens{model} |":
        "| LLM | 호출 {calls}회, 입력 토큰 {inp} / 출력 토큰 {out}{model} |",
    "| Flags | {flags} |": "| 플래그 | {flags} |",
    "| **Blocked** | {line} (see below) |": "| **진행하지 않은 항목** | {line} (아래 참고) |",
    "| **Error list** | {n} error(s): {counts} (see \"Error list\") |":
        "| **오류 목록** | {n}건: {counts} (`errors.md` 참고) |",
    "| **Error list** | {m} pom correction(s) by sbmig ({counts}); no build error (see \"Error list\") |":
        "| **오류 목록** | 빌드 오류 없음, sbmig가 고친 pom {m}건({counts}) (`errors.md` 참고) |",
    "| **Error list** | {n} error(s): {counts}; {m} pom correction(s) by sbmig (see \"Error list\") |":
        "| **오류 목록** | {n}건: {counts}, sbmig가 고친 pom {m}건 (`errors.md` 참고) |",
    "| To-be | {versions} |": "| 전환 후 | {versions} |",
    "{m}m {s}s": "{m}분 {s}초",
    "{secs}s": "{secs}초",
    # ---- blocked
    "{n} item(s) — ": "{n}건 — ",
    " (class `{owner}`)": " (클래스 `{owner}`)",
    "class `{owner}`": "클래스 `{owner}`",
    "| reason | artifact / owner class | required → found | files | attempts | suggestion |":
        "| 구분 | 아티팩트 / 클래스 | 필요한 타입 → 실제 타입 | 파일 | 수정 횟수 | 조치 방법 |",
    "{n} cluster(s) were deliberately left unchanged because the fix is outside this project (a library, a "
    "repository, the environment) or the agent gave up on them. The rest of the migration continued.":
        "프로젝트 밖(라이브러리, 저장소, 실행 환경)을 고쳐야 하거나 자동 수정으로 해결하지 못한 항목 {n}건은 일부러 "
        "손대지 않았습니다. 나머지 전환은 그대로 진행했습니다.",
    "<details><summary><code>{id}</code> — {n} error(s)</summary>":
        "<details><summary><code>{id}</code> 관련 오류 {n}건</summary>",
    # ---- unresolved
    "None. The final build passed.": "없습니다. 최종 빌드가 통과했습니다.",
    "None. No structured errors were captured; check the last build log.":
        "정리된 오류가 없습니다. 마지막 빌드 로그를 확인해 주세요.",
    "No error list was kept for this run; {n} error(s) were left after the last build, grouped by file:":
        "이번 실행에는 오류 목록이 없습니다. 마지막 빌드 뒤 남은 오류 {n}건을 파일별로 정리했습니다.",
    "module {module}": "모듈 {module}",
    "artifact {coords}": "아티팩트 {coords}",
    "(no file)": "(파일 없음)",
    " — symbol: `{symbol}`": " — 심볼: `{symbol}`",
    " — location: {location}": " — 위치: {location}",
    "Last build log: `{path}`": "마지막 빌드 로그: `{path}`",
    # ---- v2 structure review (sbmig.review)
    "Structure review": "구조 점검",
    "Vulnerability remediation": "취약점 조치",
    "Chained after the migration's BUILD SUCCESS (remediation.after_migration); it does not change the migration result "
    "above.": "마이그레이션 빌드가 성공한 뒤 이어서 실행했습니다. 위의 마이그레이션 결과에는 영향이 없습니다. 자세한 "
              "결과는 아래 파일의 report.md에 있습니다.",
    "Chained after the migration although its final build failed (a framework project: remediation.after_failed_build); "
    "it does not change the migration result above.":
        "마이그레이션 빌드는 통과하지 않았지만, 프레임워크 프로젝트라 취약점 조치를 이어서 실행했습니다. 오픈소스 라이브러리 "
        "검사는 빌드와 따로 할 수 있습니다. 위의 마이그레이션 결과에는 영향이 없습니다. 자세한 결과는 아래 파일의 "
        "report.md에 있습니다.",
    "- The project did not build before the remediation either: the changed versions were scanned again, but no build "
    "confirmed them.":
        "- 조치 전에도 빌드가 되지 않아, 바꾼 버전을 다시 검사했지만 빌드로 확인하지는 못했습니다.",
    "- Remediated, but not confirmed by a build: the project did not build before the remediation either, and the "
    "changed versions were only scanned again.":
        "- 조치 전에도 빌드가 되지 않던 프로젝트라, 바꾼 버전은 IVAS 재검사로만 확인했습니다.",
    "- The project did not build before the remediation either; no version was changed, so there was nothing for a "
    "build to confirm.":
        "- 조치 전에도 빌드가 되지 않았지만, 바꾼 버전이 없어 빌드로 확인할 것은 없습니다.",
    "- The project did not build before the remediation either.": "- 조치 전에도 빌드가 되지 않던 프로젝트입니다.",
    "- Not run: `{reason}`": "- 실행하지 않았습니다: `{reason}`",
    "# Vulnerability remediation report — {name}": "# 취약점 조치 보고서 — {name}",
    "The migration was not run: only the vulnerability remediation was requested.":
        "마이그레이션은 실행하지 않았습니다. 취약점 조치만 요청하신 실행입니다.",
    "Full remediation report: `{path}`": "자세한 조치 결과는 `{path}`에 있습니다.",
    "**Not started:** {why}": "**시작하지 못했습니다.** {why}",
    "- Status: `{status}`{reason}": "- 상태 코드: `{status}`{reason}",
    "- Counts: {counts}": "- 집계: {counts}",
    "- In-house artifacts (exception possible, contact their owners): {n}":
        "- 사내 라이브러리에서 발생한 취약점: 라이브러리 {n}개. 담당자에게 예외 처리를 요청할 수 있습니다.",
    "- Files: {files}": "- 파일: {files}",
    "| Structure review | {summary} |": "| 구조 점검 | {summary} |",
    "After the restructure and before the first build, the LLM checked the poms, dependencies and module layout "
    "against the company rules. It fixes critical issues itself (committed, revertible) and leaves a script for the "
    "KB; warnings are only recorded.":
        "구조 재구성 뒤 첫 빌드 전에 LLM이 pom, 의존성, 모듈 구성을 회사 규칙에 맞춰 점검했습니다. 심각한 문제는 LLM이 "
        "직접 고쳐 커밋으로 남겼고(되돌릴 수 있습니다) KB에 쓸 스크립트도 남겼습니다. 경고는 기록만 했습니다.",
    "- solution: {solution}": "- 해결 방법: {solution}",
    "- direct edits in the answer: {n}": "- 답변에 담긴 직접 수정: {n}건",
    "- script for the KB: yes": "- KB용 스크립트: 있음",
    "- script for the KB: none — {reason}": "- KB용 스크립트: 없음 ({reason})",
    "- changed: {files}": "- 수정한 파일: {files}",
    "- verdict: `{verdict}` — {summary}": "- 판정: `{verdict}` — {summary}",
    "- model: `{model}`, {calls} LLM call(s)": "- 모델: `{model}`, LLM 호출 {calls}회",
    "| severity | file | problem | fix hint |": "| 심각도 | 파일 | 문제 | 수정 방법 |",
    "critical": "심각",
    "warning": "경고",
    "(project)": "(프로젝트 전체)",
    "… {n} more issue(s) — see `structure_review` in report.json":
        "… 외 {n}건 (report.json의 `structure_review` 참고)",
    "Analysis: {analysis}": "분석: {analysis}",
    "skipped ({note})": "실행하지 않음 ({note})",
    "failed — no usable answer, the build went ahead ({note})":
        "실패: 쓸 수 있는 답을 받지 못해 점검 없이 빌드를 진행했습니다 ({note})",
    "ok — no issues": "문제 없음",
    "{c} critical, {w} warning(s)": "심각 {c}건, 경고 {w}건",
    "fix {status}, changes restored": "수정 결과 {status}, 변경은 되돌렸습니다",
    "fix {status}, {n} file(s) changed (commit `{commit}`)": "수정 결과 {status}, 파일 {n}개 수정 (커밋 `{commit}`)",
    "fix {status}, {n} file(s) changed": "수정 결과 {status}, 파일 {n}개 수정",
    "fix not applied": "수정은 적용하지 않았습니다",
    "no fix": "수정 없음",
    "- fix (edits / script): `{status}`, {n} file(s) changed{files}":
        "- 수정 결과: `{status}`, 수정한 파일 {n}개{files}",
    "- commit: `{commit}` (revert it to undo the review's changes)":
        "- 커밋: `{commit}` (구조 점검의 수정을 취소하려면 이 커밋을 revert해 주세요)",
    "- the fix's changes were restored: they are not in the result": "- 수정 내용은 되돌렸습니다. 결과물에는 들어 있지 않습니다.",
    "- fix error: {error}": "- 수정 오류: {error}",
    "- fix log: {lines}": "- 수정 로그: {lines}",
    "- fix (edits / script): not applied": "- 수정은 적용하지 않았습니다.",
    "- no fix for the critical issue(s): they are handed over":
        "- 심각한 문제를 고치지 못해 「확인해 주세요」에 넣었습니다.",
    "- note: {note}": "- 메모: {note}",
    # ---- v2.1 error list + fixes by case
    "Error list": "오류 목록",
    "Fixes by case": "오류 유형별 수정",
    "compile — `mvn compile` after OpenRewrite": "compile — OpenRewrite 뒤 `mvn compile`",
    "restructure — pom corrections, verified by the next build": "restructure — 구조 재구성에서 고친 pom, 다음 빌드로 확인",
    "transform — pom corrections by sbmig, verified by the next build": "transform — sbmig가 고친 pom, 다음 빌드로 확인",
    "review — structure review": "review — 구조 점검",
    "build — first `mvn clean package`, new errors only": "build — 첫 `mvn clean package`, 새로 나온 오류만",
    "{seen} — new errors of round {n}'s build": "{seen} — {n}번째 수정 뒤 빌드에서 새로 나온 오류",
    "{n} error(s) in this run, one line each (all of them with every try and diff: `errors.md` / `errors.jsonl` in "
    "the run directory): {counts}.":
        "이번 실행의 오류 {n}건을 한 줄씩 적었습니다(수정 내용과 diff까지 모두 실행 디렉터리의 `errors.md` / "
        "`errors.jsonl`에 있습니다): {counts}.",
    "| # | case | file:line | message | status | tries | resolved |":
        "| # | 오류 유형 | 파일:줄 | 메시지 | 상태 | 수정 기록 | 해결 |",
    "… {n} more in this group — see `errors.md`": "… 이 묶음에 {n}건 더 있습니다 (`errors.md` 참고)",
    "| case | errors | cause | solution | changes (files, +/−) | edits | script |":
        "| 오류 유형 | 오류 | 원인 | 해결 방법 | 변경 파일 (+/−) | 직접 수정 | 스크립트 |",
    "{resolved}/{n} resolved": "{resolved}/{n} 해결",
    "(no solution text) {reason}": "(해결 설명 없음) {reason}",
    "no script: {reason}": "스크립트 없음: {reason}",
    "… {n} more case(s) — see `errors.md`": "… 오류 유형 {n}개 더 있습니다 (`errors.md` 참고)",
    "Stored scripts live in the KB (`<kb>/cases/<case>/fix_vN.py`); every try with its diff is in `errors.md`, the "
    "whole change set in `changes.patch`.":
        "저장한 스크립트는 KB(`<kb>/cases/<case>/fix_vN.py`)에 있습니다. 수정할 때마다의 diff는 `errors.md`에, 전체 변경은 "
        "`changes.patch`에 있습니다.",
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
        " — 구조 재구성에서 JSP / 태그 파일(page import, useBean, 스크립틀릿)의 이름을 바꿨습니다. OpenRewrite는 JSP를 "
        "읽지 않습니다.",
    # v2.1j: the intake's pom synthesis for an Ant project ("What we changed" + the Handover block)
    "pom.xml synthesized from build.xml ({n} jars: {a} pom.properties, {b} table, {c} lookup, {d} unknown)":
        "build.xml로 pom.xml 생성 (jar {n}개: pom.properties {a}, 이름표 {b}, 저장소 조회 {c}, 미확인 {d})",
    "### Ant project: pom.xml synthesized": "### Ant 프로젝트의 pom.xml 생성",
    "The project had an Ant `{build}` and no pom.xml: sbmig wrote `{pom}` at intake from build.xml and the "
    "directories (commit `intake: pom.xml synthesized from build.xml`); build.xml itself is left as it was.":
        "Ant 빌드 파일 `{build}`만 있고 pom.xml이 없어서, sbmig가 접수 단계에서 build.xml과 폴더 구조를 읽어 `{pom}` "
        "파일을 만들었습니다(커밋 `intake: pom.xml synthesized from build.xml`). build.xml은 그대로 두었습니다.",
    "- pom: `{gav}` ({packaging}), Java {java}, encoding {enc}": "- pom: `{gav}` ({packaging}), Java {java}, 인코딩 {enc}",
    "- layout: sources {src}, web root {web}, resources {res}, lib {lib}":
        "- 구조: 소스 {src}, 웹 루트 {web}, 리소스 {res}, lib {lib}",
    "- jars: {n} — {a} pom.properties, {b} name table, {c} repository lookup, {d} unknown":
        "- jar {n}개: pom.properties {a}개, 이름표 {b}개, 저장소 조회 {c}개, 미확인 {d}개",
    "**{n} jar(s) without Maven coordinates** — declared with `system` scope (`<systemPath>`): the build passes, but "
    "system-scope jars are not packaged into the WAR / boot jar, so the application fails at runtime until each one "
    "is installed into Nexus and its pom entry is replaced by a normal dependency (drop `<scope>system</scope>` and "
    "`<systemPath>`):":
        "**Maven 좌표를 찾지 못한 jar {n}개** — `system` scope(`<systemPath>`)로 선언했습니다. 빌드는 통과하지만 "
        "system scope jar는 WAR / boot jar에 포함되지 않아 실행하면 실패합니다. 각 jar를 Nexus에 올리고 pom 항목을 일반 "
        "의존성으로 바꿔 주세요(`<scope>system</scope>`와 `<systemPath>` 삭제).",
    "**{n} jar(s) without Maven coordinates** — not declared (`intake.unknown_jar: skip`): the build shows which "
    "ones the code needs; install each into Nexus and add it to the pom as a dependency:":
        "**Maven 좌표를 찾지 못한 jar {n}개** — pom에 선언하지 않았습니다(`intake.unknown_jar: skip`). 코드에 필요한 "
        "jar는 빌드 오류로 드러납니다. 각 jar를 Nexus에 올리고 pom에 의존성으로 추가해 주세요.",
    "(`-Dfile` paths are those of the as-is tree; publishing to Nexus is `mvn deploy:deploy-file` with the same "
    "coordinates plus `-Durl` / `-DrepositoryId`.)":
        "(`-Dfile` 경로는 변경 전 프로젝트 기준입니다. Nexus에 배포할 때는 같은 좌표로 `mvn deploy:deploy-file`을 "
        "실행하고 `-Durl` / `-DrepositoryId`를 붙여 주세요.)",
    "Left out (no coordinates and no source imports their packages): {jars} — add one back if the build or the "
    "runtime needs it.":
        "좌표도 없고 어느 소스도 패키지를 import하지 않아 뺀 jar: {jars}. 빌드나 실행에 필요하면 다시 추가해 주세요.",
    "build.xml steps not translated to Maven — check whether the build still needs them:":
        "Maven으로 옮기지 못한 build.xml 단계입니다. 빌드에 아직 필요한지 확인해 주세요.",
    "Synthesis warnings:": "pom 생성 경고:",
    "yes": "예",
    "no: {error}": "아니오: {error}",
    # ---- notes
    "- plan: {note}": "- 계획: {note}",
    # ---- handover
    "No version changes and no library advisories recorded for this run.":
        "이번 실행에서 바뀐 버전과 라이브러리 권고가 없습니다.",
    "- parent: {before} → {after}": "- 부모 pom: {before} → {after}",
    "- java: {before} → {after}": "- Java: {before} → {after}",
    "- spring boot: {before} → {after}": "- Spring Boot: {before} → {after}",
    "| what | before | after | why |": "| 항목 | 변경 전 | 변경 후 | 비고 |",
    "managed": "부모 pom에서 지정",
    " (managed)": " (부모 pom에서 지정)",
    "⚠ 1 dependency pinned at the as-is version: the new parent / Boot BOM no longer manages it.":
        "⚠ 의존성 1개는 새 부모 pom과 Spring Boot BOM에 버전이 없어 기존 버전을 pom에 직접 적었습니다.",
    "⚠ {n} dependencies pinned at the as-is version: the new parent / Boot BOM no longer manages them.":
        "⚠ 의존성 {n}개는 새 부모 pom과 Spring Boot BOM에 버전이 없어 기존 버전을 pom에 직접 적었습니다.",
    "Added: {items}": "추가: {items}",
    "Removed: {items}": "제거: {items}",
    "No dependency versions changed.": "의존성 버전은 바뀌지 않았습니다.",
    "_note: {note}_": "_참고: {note}_",
    "| library | as-is | status | what we did | outcome | what you do next | docs |":
        "| 라이브러리 | 변경 전 버전 | 상태 | 이번 실행에서 한 일 | 결과 | 확인할 일 | 문서 |",
    "✅ replaced": "✅ 교체",
    "⚠ still present": "⚠ 남아 있음",
    "replaced during the run": "실행 중에 교체",
    "built with the as-is version pinned": "기존 버전을 유지한 채 빌드",
    "**Merge conflicts** (multi-module merge: first module in `<modules>` order wins)":
        "**모듈 병합에서 겹친 파일** (`<modules>` 순서에서 첫 모듈의 파일을 남김)",
    "Nothing beyond the usual review — the migrated project builds with the versions above.":
        "평소 검토 외에 따로 확인할 사항은 없습니다. 전환한 프로젝트는 위 버전으로 빌드됩니다.",
    # handover reasons (VersionChange.reason constants of handover.py)
    "carried over (no longer managed)": "새 부모 pom에 버전이 없어 기존 버전을 유지",
    "parent swap": "부모 pom 교체",
    "optional bump": "사내 선택 아티팩트 버전 맞춤",
    "optional (trigger: {matched})": "사내 선택 아티팩트 적용 (대상 의존성: {matched})",
    "pinned from the target Boot BOM (not managed by the parent chain)":
        "새 부모 pom에 버전이 없어 Spring Boot BOM의 버전을 지정",
    "mandatory": "사내 필수",
    "spring boot 4 upgrade": "Spring Boot 4 전환",
    "recipe / fix script": "OpenRewrite 레시피 또는 빌드 오류 수정",
    "recipe": "OpenRewrite 레시피",
    "parent": "부모 pom",
    "dependency": "의존성",
    "bom": "BOM",
    "plugin": "플러그인",
    "property": "프로퍼티",
    "### pom corrections by sbmig": "### sbmig가 고친 pom",
    "sbmig changed these poms itself in the transform step.": "변환 단계에서 sbmig가 직접 고친 pom입니다.",
    "No build error in this run. {m} pom correction(s) by sbmig, verified by the build.":
        "이번 실행에는 빌드 오류가 없습니다. sbmig가 고친 pom {m}건을 빌드로 확인했습니다.",
    "No build error in this run. {m} pom correction(s) by sbmig, {k} of them not verified yet.":
        "이번 실행에는 빌드 오류가 없습니다. sbmig가 고친 pom은 {m}건이고 그중 {k}건은 아직 빌드로 확인하지 못했습니다.",
    "{n} build error(s) in this run, one line each (all of them with every try and diff: `errors.md` / `errors.jsonl` in "
    "the run directory): {counts}. Besides, {m} pom correction(s) by sbmig.":
        "이번 실행의 빌드 오류 {n}건을 한 줄씩 적었습니다(수정 내용과 diff까지 모두 실행 디렉터리의 `errors.md` / "
        "`errors.jsonl`에 있습니다): {counts}. 그 밖에 sbmig가 고친 pom {m}건이 있습니다.",
    "verified": "빌드로 확인",
    "not verified yet": "아직 확인 전",
    # properties (round 8)
    "key": "키",
    "before migration": "변경 전 설정 위치",
    "referenced at": "참조 위치",
    "written as": "기록한 값",
    "set via": "설정 방법",
    "The code reads these settings but no packaged resource defines them (the external container supplied them "
    "before). Set each one before the first start.":
        "코드에서 읽는 설정인데 패키지에 포함된 리소스 어디에도 값이 없습니다(전에는 외부 컨테이너가 값을 넣어 "
        "주었습니다). 처음 기동하기 전에 각 값을 설정해 주세요.",
    "How to supply them:": "값을 넣는 방법:",
    "Packaged placeholders: {files} (grep `__SET_ME__`)": "임시 값을 넣어 둔 파일: {files} (`__SET_ME__`로 검색해 주세요)",
    # ---- flag help (FLAG_HELP / FLAG_PREFIX_HELP values of markdown.py)
    "`mvn -v` reported another Java version than `build.require_java_major`: set build.java_home / JAVA_HOME to a "
    "JDK 21 and run again":
        "`mvn -v`가 알려 준 Java 버전이 `build.require_java_major`와 다릅니다. build.java_home 또는 JAVA_HOME을 JDK 21로 "
        "맞추고 다시 실행해 주세요.",
    "the LLM is not configured (LLM_BASE_URL / LLM_API_KEY / LLM_MODEL in .env, or the gateway rejected the key / "
    "model): LLM fixes were skipped — check with `sbmig llm-probe`":
        "LLM 설정이 없거나 게이트웨이가 키 / 모델을 거부했습니다(.env의 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL). LLM "
        "수정은 건너뛰었습니다. `sbmig llm-probe`로 확인해 주세요.",
    "a fix raised a dependency across its major version or replaced a library (the build passes): the operator "
    "verifies the runtime behaviour — see \"Operator checks\" in the Handover":
        "빌드 오류를 고치면서 의존성의 메이저 버전을 올렸거나 라이브러리를 바꿨습니다(빌드는 통과). 실행 시 동작을 "
        "확인해 주세요. 「운영자 확인 필요」에 정리했습니다.",
    "the project had an Ant build.xml and no pom.xml: sbmig synthesized a pom.xml from build.xml and the directories "
    "at intake (intake.ant_to_maven) and migrated it as a Maven project — see \"Ant project: pom.xml synthesized\" "
    "in the Handover":
        "Ant build.xml만 있고 pom.xml이 없어서, sbmig가 접수 단계에서 build.xml과 폴더 구조로 pom.xml을 만들고"
        "(intake.ant_to_maven) Maven 프로젝트로 전환했습니다. 「Ant 프로젝트의 pom.xml 생성」에 정리했습니다.",
    "jars of the Ant project have no Maven coordinates (no pom.properties inside, not in jar_names.yml, not found by "
    "sha1 in the repository): declared with `system` scope (intake.unknown_jar) the build passes, but they are not "
    "packaged into the WAR — install them into Nexus and replace the pom entries (commands in the Handover)":
        "Ant 프로젝트의 jar 일부는 Maven 좌표를 찾지 못했습니다(jar 안에 pom.properties가 없고, jar_names.yml에도 없고, "
        "저장소 sha1 조회도 실패). `system` scope로 선언하면(intake.unknown_jar) 빌드는 통과하지만 WAR에 포함되지 "
        "않습니다. Nexus에 올리고 pom 항목을 바꿔 주세요(명령은 「Ant 프로젝트의 pom.xml 생성」에 있습니다).",
    "a module was listed twice in the reactor (e.g. by the root pom and as `../DEV` by a nested aggregator), which "
    "Maven refuses (\"is duplicated in the reactor\"): the nested aggregator's listing was removed at intake so the "
    "build runs — see Manual follow-ups":
        "한 모듈이 reactor에 두 번 포함되어 있었습니다(예: 루트 pom과 안쪽 집계 pom의 `../DEV`). Maven은 이런 reactor를 "
        "거부하므로(\"is duplicated in the reactor\") 빌드할 수 있도록 접수 단계에서 안쪽 집계 pom의 줄을 지웠습니다. "
        "「확인해 주세요」를 봐 주세요.",
    "the project has no pom.xml at its root and sbmig could not generate one (the top-level poms are not one "
    "application and not MSA services): the run stopped before any Maven call — add a root pom.xml or run sbmig on "
    "each application's directory":
        "프로젝트 루트에 pom.xml이 없고 sbmig가 만들 수도 없었습니다(최상위 pom들이 한 애플리케이션도, MSA 서비스도 "
        "아닙니다). Maven을 실행하기 전에 멈췄습니다. 루트 pom.xml을 추가하거나 애플리케이션 폴더마다 sbmig를 따로 "
        "실행해 주세요.",
    # 사내 부모의 기존 버전이 저장소에 없을 때의 플래그 설명. markdown.FLAG_HELP를 바꾸면 함께 바꾼다
    "the project's own version of the company parent was not in the repository: sbmig set the configured version in those "
    "poms (openrewrite.unresolvable_asis_parent: align) — the recipes ran on the target parent's classpath, not the as-is "
    "one (see the Handover)":
        "프로젝트가 쓰던 사내 부모 pom 버전이 저장소에 없어서 sbmig가 해당 pom들에 설정된 버전을 적었습니다"
        "(openrewrite.unresolvable_asis_parent: align). 레시피는 기존 부모가 아니라 새 부모 pom의 클래스패스로 "
        "실행했습니다. 「확인해 주세요」를 봐 주세요.",
    "OpenRewrite did not run because an as-is parent pom is not in the repository: no recipe changed the code, the "
    "restructure replaced the parent and the fix loop did the rest (see the Handover)":
        "기존 부모 pom이 저장소에 없어 OpenRewrite를 실행하지 못했습니다. 레시피로 바뀐 코드는 없고, 구조 재구성에서 "
        "부모 pom을 바꾼 뒤 나머지는 빌드 오류 수정 단계에서 처리했습니다. 「확인해 주세요」를 봐 주세요.",
    # 빈 reactor, reactor 밖 소스, stray-pom-ignored 플래그 설명.
    # sbmig/report/markdown.py를 바꾸면 함께 바꾼다
    "a pom.xml with the same coordinates as a module that no pom lists (a stray copy in an IDE output / backup folder such as "
    "`boot/bin/pom.xml`): ignored at intake — not built, not migrated, not shown to the structure review; see Manual follow-ups":
        "어느 pom도 모듈로 나열하지 않는데 다른 모듈과 좌표가 같은 pom.xml이 있어 접수 단계에서 무시했습니다(IDE 출력이나 "
        "백업 폴더에 남은 복사본, 예: `boot/bin/pom.xml`). 빌드, 전환, 구조 점검에서 모두 제외했습니다. 「확인해 "
        "주세요」를 봐 주세요.",
    "the Maven reactor compiles no sources: the java / JSP files lie outside its modules' source and web roots and were "
    "not migrated (the as-is build only packaged files produced elsewhere) — the run is not a success; map the sources "
    "into the modules first (see the Handover's first line)":
        "Maven reactor가 컴파일하는 소스가 없습니다. java / JSP 파일이 모듈의 소스 루트와 웹 루트 밖에 있어 전환하지 "
        "않았습니다(기존 빌드는 다른 곳에서 만든 파일을 묶기만 했습니다). 이 실행은 성공이 아닙니다. 소스를 먼저 모듈로 "
        "옮겨 주세요(「확인해 주세요」의 첫 항목 참고).",
    "more java files lie outside the modules' source roots than inside them: they are neither built nor migrated — "
    "check whether they belong to the application":
        "모듈의 소스 루트 밖에 있는 java 파일이 안에 있는 파일보다 많습니다. 이 파일들은 빌드하지도 전환하지도 "
        "않았습니다. 애플리케이션에 속한 파일인지 확인해 주세요.",
    # 프로젝트에 포함된 jar의 bundled-jars 플래그 설명과 그 절. sbmig/report/markdown.py를 바꾸면 함께 바꾼다
    "jars sit in the tree (e.g. WEB-INF/lib) that no pom declares — the as-is IDE build compiled against them, Maven does "
    "not: when a build error needs one, sbmig wires it into the module pom (`sbmig-bundled`: a normal dependency when the "
    "repository has its coordinates, else system scope + includeSystemScope) — see \"Bundled jars (declared by no pom)\" "
    "and the Handover":
        "어느 pom에도 선언되지 않은 jar가 프로젝트 안에 있습니다(예: WEB-INF/lib). 기존 IDE 빌드는 이 jar로 컴파일했지만 "
        "Maven은 이 jar를 알지 못합니다. 빌드 오류에 이 jar가 필요하면 sbmig가 모듈 pom에 추가합니다(`sbmig-bundled`: "
        "저장소에 좌표가 있으면 일반 의존성, 없으면 system scope와 includeSystemScope). 「프로젝트에 포함된 jar」와 "
        "「확인해 주세요」를 봐 주세요.",
    "Bundled jars (declared by no pom)": "프로젝트에 포함된 jar",
    "Jars in the tree that no pom declares: the as-is IDE build compiled against them, Maven does not. When a build error "
    "needs one, sbmig wires it into the module pom (`sbmig-bundled`, no LLM call).":
        "어느 pom에도 선언되지 않은 채 프로젝트 안에 있는 jar입니다. 기존 IDE 빌드는 이 jar로 컴파일했지만 Maven은 알지 "
        "못합니다. 빌드 오류에 이 jar가 필요하면 sbmig가 모듈 pom에 추가합니다(`sbmig-bundled`, LLM 호출 없음).",
    "| jar | coordinates | packages | used | wired | status |": "| jar | 좌표 | 패키지 수 | 사용 | 추가 방식 | 상태 |",
    "A jar whose coordinates the target parent chain manages (an old Spring jar, say) is superseded by the managed version "
    "and is never wired.":
        "새 부모 pom이 버전을 지정하는 jar(예: 예전 Spring jar)는 그 버전으로 대신하므로 pom에 추가하지 않습니다.",
    "superseded (the target chain manages it)": "추가 안 함 (새 부모 pom에서 지정)",
    "A jar whose coordinates the project's parent chain manages (an old Spring jar, say) is superseded by the managed "
    "version and is never wired.":
        "프로젝트의 부모 pom이 버전을 지정하는 jar(예: 예전 Spring jar)는 그 버전으로 대신하므로 pom에 추가하지 않습니다.",
    "superseded (the parent chain manages it)": "추가 안 함 (부모 pom에서 지정)",
    "coordinates known": "좌표 확인",
    "coordinates unknown": "좌표 미확인",
    "unknown": "미확인",
    # 포함된 jar 표의 추가 단계와 상태 단어, 설명 한 줄. sbmig/report/markdown.py를 바꾸면 함께 바꾼다
    "restructure": "구조 재구성",
    "loop": "빌드 오류 수정",
    "container API": "컨테이너 API",
    "unused": "참조 없음",
    "The restructure wired every referenced jar before the first build (layout.wire_bundled_jars): known coordinates the "
    "repository has as a plain dependency, the others as system scope; container API copies, superseded and unreferenced "
    "jars are left out.":
        "구조 재구성에서 첫 빌드 전에 참조되는 jar를 모두 pom에 추가했습니다(layout.wire_bundled_jars). 저장소에 좌표가 "
        "있는 jar는 일반 의존성으로, 나머지는 system scope로 추가했습니다. 컨테이너 API 사본, 새 부모 pom이 버전을 지정하는 "
        "jar, 참조되지 않는 jar는 제외했습니다.",
    # Oracle ADF 프로젝트의 플래그 설명과 "ADF 규모" 절. ADF 프로젝트는 변환하지 않는다
    "an Oracle ADF (JDeveloper) project (two or more evidence families: BC4J XML, oracle.jbo / oracle.adf imports, ADF Model "
    "bindings, ADF Faces / task flows): conversion is not possible — ADF 프로젝트라 변환이 불가능합니다; the run stopped at "
    "analyze (intake.adf_policy: stop) — see \"ADF sizing\" and the Handover's first line":
        "Oracle ADF(JDeveloper) 프로젝트입니다(근거 두 종류 이상: BC4J XML, oracle.jbo / oracle.adf import, ADF Model "
        "바인딩, ADF Faces / 태스크 플로). ADF 프로젝트라 변환이 불가능합니다. 분석 단계에서 실행을 멈췄습니다"
        "(intake.adf_policy: stop). 「ADF 규모」와 「확인해 주세요」의 첫 항목을 봐 주세요.",
    "ADF evidence of one family only (not rejected): the conversion went on — check whether this is an ADF project (ADF 흔적: "
    "변환은 계속하되 확인 필요; see \"ADF sizing\")":
        "ADF 근거가 한 종류만 있어 변환은 계속했습니다. ADF 프로젝트인지 확인해 주세요. 「ADF 규모」에 정리했습니다.",
    "ADF sizing": "ADF 규모",
    "Verdict: **ADF project — conversion is not possible** ({n} evidence families: {fams}). ADF 프로젝트라 변환이 불가능합니다.":
        "판정: **ADF 프로젝트라 변환이 불가능합니다** (근거 {n}종류: {fams}).",
    "Verdict: **ADF traces only** (one evidence family: {fams}) — not rejected; the conversion went on, check whether this is "
    "an ADF project.":
        "판정: **ADF 흔적만 있습니다** (근거 한 종류: {fams}). 변환은 계속했으니 ADF 프로젝트인지 확인해 주세요.",
    "Oracle ADF (JDeveloper) evidence. An ADF project is not converted (business decision, 2026-11-02): two or more evidence "
    "families end a run with \"ADF 프로젝트라 변환이 불가능합니다\"; one family is only a trace. The numbers are census information.":
        "Oracle ADF(JDeveloper) 근거입니다. ADF 프로젝트는 변환하지 않습니다(현업 결정 2026-11-02). 근거가 두 종류 이상이면 "
        "\"ADF 프로젝트라 변환이 불가능합니다\"로 실행을 끝내고, 한 종류면 흔적으로만 봅니다. 숫자는 규모 파악용입니다.",
    "| Item | Count |": "| 항목 | 개수 |",
    "entities (BC4J) / tables": "엔티티(BC4J) / 테이블",
    "view objects": "뷰 오브젝트",
    "application modules": "애플리케이션 모듈",
    "page definitions (bindings)": "pageDef(바인딩)",
    "task flows / views": "태스크 플로 / 뷰",
    "ADF pages (jspx / jsff)": "ADF 페이지(jspx / jsff)",
    "java files / using oracle.adf, oracle.jbo, oracle.binding": "java 파일 / oracle.adf · oracle.jbo · oracle.binding 사용",
    "Tables: {tables}": "테이블: {tables}",
    " … and {n} more": " … 외 {n}개",
    "Evidence": "판정 근거",
    "hints (not evidence): {files}": "참고 파일(근거 아님): {files}",
    # 사내 프레임워크 프로젝트 절(markdown.framework_lines)과 머리 표의 범위 줄
    "In-house framework project": "사내 프레임워크 프로젝트",
    "In-house framework": "사내 프레임워크",
    "{name} framework project": "{name} 프레임워크 프로젝트",
    "{name} framework": "{name} 프레임워크",
    "**{name} framework projects get JDK 21 and Jakarta only.**": "**{name} 프레임워크는 JDK 21, Jakarta만 적용합니다.**",
    "**In-house framework projects get JDK 21 and Jakarta only.**":
        "**사내 프레임워크 프로젝트는 JDK 21, Jakarta만 적용합니다.**",
    "| Scope | JDK 21 and jakarta only (in-house framework project) |":
        "| 범위 | JDK 21과 jakarta만 적용 (사내 프레임워크 프로젝트) |",
    "{artifact}: declared directly in `{where}`": "{artifact}: `{where}`에 직접 선언",
    "{artifact}: inherited from the parent pom `{parent}` (module `{where}`)":
        "{artifact}: 부모 pom `{parent}`에서 상속(모듈 `{where}`)",
    "{artifact}: inherited from a parent pom (module `{where}`)": "{artifact}: 부모 pom에서 상속(모듈 `{where}`)",
    "{artifact}: jar in the tree (`{where}`)": "{artifact}: 프로젝트 안의 jar 파일(`{where}`)",
    "{artifact}: brought by another library, path `{via}` (module `{where}`)":
        "{artifact}: 다른 라이브러리의 의존성(경로 `{via}`, 모듈 `{where}`)",
    "{artifact}: brought by another library (module `{where}`)": "{artifact}: 다른 라이브러리의 의존성(모듈 `{where}`)",
    "JDK 21 and Jakarta conversion": "JDK 21과 Jakarta 변환",
    "one build": "빌드 1회",
    "build error fixes": "빌드 오류 수정",
    "vulnerability remediation": "취약점 조치",
    "Done in this run: {what}": "이번 실행에서 한 일: {what}",
    "Not done: restructure, structure review. The parent pom and the framework version stay as they are.":
        "하지 않은 일: 구조 정리, 구조 검토. 부모 pom과 프레임워크 버전은 그대로 둡니다.",
    "Not done: restructure, structure review, automatic build error fixes (framework.fix_loop: false). The parent pom "
    "and the framework version stay as they are.":
        "하지 않은 일: 구조 정리, 구조 검토, 빌드 오류 자동 수정. 부모 pom과 프레임워크 버전은 그대로 둡니다.",
    "The full migration ran because the configuration says so (framework.scope: full).":
        "설정(framework.scope: full)에 따라 전체 마이그레이션을 진행했습니다.",
    "The artifact arrives only through another library: the full migration ran because the configuration says so "
    "(framework.transitive: {setting}).":
        "다른 라이브러리의 의존성으로만 포함된 아티팩트여서 설정(framework.transitive: {setting})에 따라 전체 "
        "마이그레이션을 진행했습니다.",
    "Not checked: {where}": "확인하지 못한 곳: {where}",
    "the project uses an in-house framework artifact (framework.artifacts) — see the framework block at the top of "
    "the report for where it was found and what this run did":
        "사내 프레임워크 아티팩트(framework.artifacts)를 쓰는 프로젝트입니다. 어디에서 찾았는지와 이번 실행에서 한 일은 "
        "보고서 맨 위의 프레임워크 프로젝트 안내에 있습니다.",
    " — renamed by sbmig in JSP / tag files (page imports, useBean, scriptlets); OpenRewrite does not parse JSPs":
        " — sbmig가 JSP / 태그 파일(page import, useBean, 스크립틀릿)의 이름을 바꿨습니다. OpenRewrite는 JSP를 읽지 "
        "않습니다.",
    "artifacts.parent_manages_spring_boot is true but the in-house parent chain does not import the Spring Boot "
    "BOM: the versions it does not manage were pinned from the target Boot BOM — import spring-boot-dependencies in "
    "base-parent (see the Handover's first line)":
        "artifacts.parent_manages_spring_boot가 true인데 사내 부모 pom이 Spring Boot BOM을 import하지 않습니다. 부모 pom에 "
        "버전이 없는 의존성은 Spring Boot BOM의 버전을 pom에 직접 적었습니다. base-parent에서 spring-boot-dependencies를 "
        "import하도록 해 주세요(「확인해 주세요」의 첫 항목 참고).",
    "the run ended for an infrastructure reason (LLM gateway, Maven, JDK, repository / Nexus, disk), not because of "
    "the project: fix the environment and run it again (a fleet re-queues the project)":
        "프로젝트 문제가 아니라 실행 환경(LLM 게이트웨이, Maven, JDK, 저장소 / Nexus, 디스크) 문제로 실행이 "
        "끝났습니다. 환경을 고친 뒤 다시 실행해 주세요(fleet에서는 이 프로젝트를 다시 대기열에 넣습니다).",
    "the project had no pom.xml at its root but sibling poms that depend on each other (e.g. APP/BATCH, APP/WEB, APP/EAR) or one aggregator below the root (e.g. APP/pom.xml listing ../DEV): sbmig generated a root aggregator pom so they build in one reactor and are merged into the company layout (layout.generate_aggregator)":
        "프로젝트 루트에 pom.xml이 없고, 서로 의존하는 pom들(예: APP/BATCH, APP/WEB, APP/EAR)이나 루트 아래 집계 pom "
        "하나(예: ../DEV를 나열한 APP/pom.xml)만 있어서 sbmig가 루트 집계 pom을 만들었습니다. 한 reactor로 함께 "
        "빌드하고 회사 표준 구조로 합쳤습니다(layout.generate_aggregator).",
    "the input was an EAR pom whose modules are not in the directory (e.g. APP/EAR alone): run sbmig on the directory that contains every module":
        "올린 프로젝트가 EAR pom인데 EAR에 묶인 모듈이 폴더에 없습니다(예: APP/EAR만 올린 경우). 모든 모듈이 들어 있는 "
        "상위 폴더로 다시 실행해 주세요.",
    "an EAR module (application-server packaging) was dropped: its modules were merged and the boot module is deployed instead; descriptors are kept under asis-ear/ and the context root is a follow-up":
        "EAR 모듈(애플리케이션 서버용 패키징)을 없앴습니다. 모듈은 합치고 boot 모듈을 대신 배포합니다. 배포 디스크립터는 "
        "asis-ear/에 남겼으니 context root 설정은 직접 확인해 주세요.",
    "a WAR dependency (maven-war-plugin overlay) was moved from the jar modules to the boot WAR — the overlay WAR must be resolvable from your repositories":
        "WAR 의존성(maven-war-plugin overlay)을 jar 모듈에서 boot WAR로 옮겼습니다. overlay WAR는 설정된 저장소(사내 "
        "Nexus)에서 받을 수 있어야 합니다.",
    "web content that no pom references (e.g. a STATIC/ folder with JSPs / downloads) was left in place — serve it as before or move it into the front module":
        "어떤 pom도 참조하지 않는 웹 콘텐츠(예: JSP와 다운로드 파일이 있는 STATIC/)는 그대로 두었습니다. 전처럼 웹 "
        "서버에서 제공하거나 front 모듈로 옮겨 주세요.",
    "OpenRewrite could not parse some source files (see the phase notes / rewrite log): recipes that need type "
    "information skipped them; the fix loop covered what the build found":
        "OpenRewrite가 일부 소스 파일을 읽지 못했습니다(단계 메모와 rewrite 로그 참고). 타입 정보가 필요한 레시피는 그 "
        "파일을 건너뛰었고, 빌드에서 드러난 오류는 빌드 오류 수정 단계에서 처리했습니다.",
    "web.xml servlets / filters / listeners / error pages were turned into a generated WebXmlRegistrations "
    "@Configuration in the boot module (web.xml itself is not read by Spring Boot) — verify the mappings in the "
    "Handover":
        "web.xml의 서블릿 / 필터 / 리스너 / 오류 페이지를 boot 모듈에 생성한 WebXmlRegistrations @Configuration으로 "
        "옮겼습니다(Spring Boot는 web.xml을 읽지 않습니다). 매핑이 맞는지 확인해 주세요.",
    "javax EE API jars are still on the resolved classpath after the migration (section \"javax API jars still on "
    "the classpath\"): the direct dependencies that drag them in are compiled against javax and need Jakarta builds":
        "전환 뒤에도 javax EE API jar가 클래스패스에 남아 있습니다(「클래스패스에 남은 javax API jar」 참고). 이 jar를 "
        "가져오는 직접 의존성이 javax 기준으로 컴파일되어 있어 Jakarta 빌드가 필요합니다.",
    # ---- javax API jars still on the classpath (X1b, 2026-10-22)
    "javax API jars still on the classpath": "클래스패스에 남은 javax API jar",
    "| javax API jars | {n} |": "| javax API jar | {n}개 |",
    " ← declared in the pom": " ← pom에 직접 선언",
    "{n} javax EE API jar(s) are still on the resolved classpath after the migration (`mvn dependency:tree`). The "
    "library in \"via\" is compiled against javax: code that passes jakarta.* types to it does not compile, and it "
    "fails at runtime.":
        "전환 뒤에도 javax EE API jar {n}개가 클래스패스에 남아 있습니다(`mvn dependency:tree`). \"via\" 칸의 "
        "라이브러리는 javax 기준으로 컴파일되어 있어서, jakarta.* 타입을 넘기는 코드는 컴파일되지 않고 실행 중에도 "
        "실패합니다.",
    "| artifact | module | scope | via (direct dependency) | declared in pom | what to do |":
        "| 아티팩트 | 모듈 | scope | via (직접 의존성) | pom에 선언 | 할 일 |",
    "(root)": "(루트)",
    "no": "아니오",
    "raise to {version} (jakarta.* since {since})": "{version} 버전으로 업그레이드 ({since}부터 jakarta.*)",
    "coordinates changed: relocate to `{key}:{version}` (jakarta.* since {since})":
        "새 좌표 `{key}:{version}` 사용 ({since}부터 jakarta.*)",
    ", requires Java {java}": ", Java {java} 이상 필요",
    "in-house → Jakarta build": "사내 아티팩트: Jakarta 빌드 필요",
    "upgrade / replace": "업그레이드 또는 교체",
    "remove the declaration from the pom": "pom에서 선언 삭제",
    "every build ran with `-Dmaven.test.skip=true` (build.skip_test_compile): test sources were neither compiled nor "
    "run — the owning team compiles and fixes them":
        "모든 빌드를 `-Dmaven.test.skip=true`로 실행했습니다(build.skip_test_compile). 테스트 소스는 컴파일도 실행도 "
        "하지 않았으니 담당 팀이 컴파일하고 고쳐 주세요.",
    "main code builds, but test sources only compile with `-Dmaven.test.skip=true` (see follow-ups) — fix the "
    "tests before relying on CI":
        "메인 코드는 빌드되지만 테스트 소스는 `-Dmaven.test.skip=true`로 건너뛰어야 빌드됩니다(「확인해 주세요」 참고). "
        "CI에 맡기기 전에 테스트를 고쳐 주세요.",
    "main code compiles and packages, but some tests fail at runtime: the jar was built with `-DskipTests` "
    "(failing tests listed in follow-ups / unresolved)":
        "메인 코드는 컴파일과 패키징이 되지만 일부 테스트가 실행 중 실패합니다. jar는 `-DskipTests`로 빌드했습니다(실패한 "
        "테스트는 「확인해 주세요」와 「해결하지 못한 오류」에 있습니다).",
    "the run hit `loop.max_total_minutes`; remaining loops were skipped, verify still ran":
        "실행 시간이 `loop.max_total_minutes`에 도달했습니다. 남은 수정은 건너뛰고 최종 빌드는 실행했습니다.",
    "the run used all `loop.max_llm_calls`; remaining clusters were escalated":
        "LLM 호출 한도(`loop.max_llm_calls`)를 모두 썼습니다. 남은 오류는 「진행하지 않은 항목」으로 넘겼습니다.",
    "dry run: decisions were computed but nothing was applied": "dry run: 무엇을 할지 정하기만 하고 아무것도 적용하지 않았습니다.",
    "the run stopped early (see follow-ups for the reason)": "실행이 중간에 멈췄습니다(이유는 「확인해 주세요」에 있습니다).",
    "Maven was not runnable on this machine": "이 서버에서 Maven을 실행할 수 없었습니다.",
    "the LLM failed twice in a row (transport/config); the cases still needing a fix script are listed as blocked "
    "(llm_unavailable) — stored scripts and the final build still ran":
        "LLM 호출이 연속 두 번 실패했습니다(연결 또는 설정 문제). 아직 수정 스크립트가 필요한 오류는 「진행하지 않은 "
        "항목」(llm_unavailable)에 넣었습니다. 저장된 스크립트와 최종 빌드는 그대로 실행했습니다.",
    "the configured in-house parent POM (or a build extension it declares) is not resolvable — nothing can build until it is (see Blocked)":
        "설정한 사내 부모 pom(또는 부모 pom이 선언한 빌드 확장)을 저장소에서 받지 못했습니다. 받을 수 있을 때까지 아무것도 "
        "빌드할 수 없습니다(「진행하지 않은 항목」 참고).",
    "Maven (3.8.1+) refused an HTTP repository through its blocker mirror (maven-default-http-blocker): the project and "
    "sbmig are not the cause — fix the Maven settings (see the Handover's first line)":
        "Maven(3.8.1 이상)이 차단 미러(maven-default-http-blocker)로 HTTP 저장소를 막았습니다. 프로젝트나 sbmig 문제가 "
        "아닙니다. Maven settings를 고쳐 주세요(「확인해 주세요」의 첫 항목 참고).",
    "the run was stopped on purpose after this phase (--stop-after)":
        "--stop-after 옵션에 따라 이 단계 뒤에서 실행을 멈췄습니다.",
    "the run stopped early (see Blocked / follow-ups)":
        "실행이 중간에 멈췄습니다(「진행하지 않은 항목」과 「확인해 주세요」 참고).",
    # ---- inventory.md (`sbmig inventory`, sbmig/report/inventory.py)
    "# Webapp inventory — {name}": "# 웹앱 인벤토리 — {name}",
    "Read-only scan of a directory without a pom (scanned {at}). Nothing was changed.":
        "pom이 없는 폴더를 읽기만 하고 살펴봤습니다({at}). 아무것도 바꾸지 않았습니다.",
    "| Item | Value |": "| 항목 | 값 |",
    "| Root | `{root}` |": "| 루트 | `{root}` |",
    "| Webapp roots | {n} |": "| 웹앱 루트 | {n} |",
    "| JSP files | {n} |": "| JSP 파일 | {n} |",
    "| Source roots / .java files | {roots} / {files} |": "| 소스 루트 / .java 파일 | {roots} / {files} |",
    "| Jars known / unknown / ignored | {known} / {unknown} / {ignored} |":
        "| jar 좌표 확인 / 미확인 / 제외 | {known} / {unknown} / {ignored} |",
    "| Class files (Java level) | {n} ({java}) |": "| 클래스 파일 (Java 버전) | {n} ({java}) |",
    "| Duplicate classes | {n} |": "| 중복 클래스 | {n} |",
    "| Build files found | {files} |": "| 찾은 빌드 파일 | {files} |",
    "| Warnings | {n} |": "| 경고 | {n} |",
    "## Warnings": "## 경고",
    "## Webapp roots": "## 웹앱 루트",
    "No directory with a `WEB-INF` was found.": "`WEB-INF`가 있는 폴더를 찾지 못했습니다.",
    "| web.xml | {path} (Servlet {spec}) |": "| web.xml | {path} (Servlet {spec}) |",
    "| Servlets / filters / listeners / taglibs | {s} / {f} / {l} / {t} |":
        "| 서블릿 / 필터 / 리스너 / taglib | {s} / {f} / {l} / {t} |",
    "| Frameworks | {fw} |": "| 프레임워크 | {fw} |",
    "| JNDI resources | {names} |": "| JNDI 리소스 | {names} |",
    "| JSP files | {n} {sample} |": "| JSP 파일 | {n} {sample} |",
    "| Static files | {n} |": "| 정적 파일 | {n} |",
    "| Spring XML | {files} |": "| Spring XML | {files} |",
    "| WEB-INF/lib jars (ignored files) | {n} ({ignored}) |": "| WEB-INF/lib jar (제외한 파일) | {n} ({ignored}) |",
    "| WEB-INF/classes | {n} class file(s), Java {java} |": "| WEB-INF/classes | 클래스 파일 {n}개, Java {java} |",
    "| Classes without source | {n} {sample} |": "| 소스가 없는 클래스 | {n} {sample} |",
    "## Jars": "## jar 목록",
    "No jars under `WEB-INF/lib`.": "`WEB-INF/lib`에 jar가 없습니다.",
    "| Jar | Version | Coordinates guess | sha1 | Note |": "| jar | 버전 | 추정 좌표 | sha1 | 비고 |",
    "backup: Tomcat does not load it (left out of the pom)": "백업 파일: Tomcat이 읽지 않으므로 pom에서 제외",
    "unknown coordinates": "좌표 미확인",
    "Other files under `WEB-INF/lib` (ignored): {files}": "`WEB-INF/lib`의 그 밖의 파일 (제외): {files}",
    "## Source roots": "## 소스 루트",
    "No `.java` files were found.": "`.java` 파일을 찾지 못했습니다.",
    "| Source root | Files | Packages | Encoding | javax imports | Frameworks | JDK internals | Webapp |":
        "| 소스 루트 | 파일 | 패키지 | 인코딩 | javax import | 프레임워크 | JDK 내부 API | 웹앱 |",
    "{n} class(es) exist in more than one source root: {sample}": "여러 소스 루트에 같은 클래스가 {n}개 있습니다: {sample}",
    "## Maven module proposal": "## Maven 모듈 제안",
    "Nothing to propose (no webapp root and no sources).": "제안할 내용이 없습니다(웹앱 루트도 소스도 없습니다).",
    "A guess for the pom synthesis — change it if the split is wrong.":
        "pom 생성을 위해 추정한 모듈 구성입니다. 맞지 않으면 고쳐 주세요.",
    "| Module | Packaging | Webapp dir | Source dirs | Java | Jars known / unknown |":
        "| 모듈 | 패키징 | 웹앱 폴더 | 소스 폴더 | Java | jar 좌표 확인 / 미확인 |",
    "## What to do next": "## 다음에 할 일",
    "1. Check the coordinates of the unknown jars and add the company ones to "
    "`knowledge/libraries/jar_names.yml` (stem → groupId:artifactId), then re-run `sbmig inventory`.":
        "1. 좌표를 모르는 jar의 좌표를 확인하고, 사내 jar는 `knowledge/libraries/jar_names.yml`에 추가한 뒤"
        "(stem → groupId:artifactId) `sbmig inventory`를 다시 실행해 주세요.",
    "2. Confirm the module split above (one module per webapp root; shared sources need a common jar).":
        "2. 위 모듈 구성이 맞는지 확인해 주세요(웹앱 루트마다 모듈 하나, 공유 소스는 공통 jar가 필요합니다).",
    "3. Round 2: `sbmig synthesize-pom` will write the poms from `inventory.json` (not available yet).":
        "3. 다음 단계에서 `sbmig synthesize-pom`이 `inventory.json`으로 pom을 만들 예정입니다(아직 지원하지 않습니다).",
}}


# ---------------------------------------------------------------------------------------------------------------------
# 규칙이 함수로 문장을 만들 때 쓰는 도우미
# ---------------------------------------------------------------------------------------------------------------------
FRAMEWORK_OWNER_KO = ("프로젝트 안에서는 고칠 수 없습니다. 프레임워크 담당자가 jakarta 빌드를 제공해야 합니다. sbmig는 프레임워크 "
                      "버전을 그대로 둡니다.")
LEFT_ALONE_KO = "이 항목은 일부러 손대지 않았습니다."


def _where_ko(where: str) -> str:
    """``module `x``` / ``the root module`` / ``the pom of …`` 같은 위치 표현을 한국어로 바꾼다."""
    w = (where or "").strip()
    if w == "the root module":
        return "루트 모듈"
    m = re.match(r"^module `([^`]+)`$", w)
    if m:
        return f"모듈 `{m.group(1)}`"
    m = re.match(r"^the root `pom\.xml`$", w)
    if m:
        return "루트 `pom.xml`"
    m = re.match(r"^the pom of (.+)$", w)
    if m:
        return f"{_where_ko(m.group(1))}의 pom"
    return w


def _api_subject(m: "re.Match[str]") -> str:
    """외부 라이브러리 API 문구의 머리를 만든다. 라이브러리와 클래스, 아직 받는 javax 타입을 담는다."""
    a, o, t = m.group("a"), m.group("o"), m.group("t")
    who = f"`{a}` 라이브러리" + (f"(클래스 `{o}`)" if o else "")
    return f"{who}가 아직 `{t}` 타입을 받습니다" if t else f"{who}의 API가 이 코드와 맞지 않습니다"


def _mvn_holds_ko(text: str) -> str:
    """``.mvn`` 폴더에 든 것을 적은 영어 목록에서 설명 낱말만 한국어로 바꾼다. 옵션과 파일 이름은 그대로 둔다."""
    out = text
    for en, ko in (("other files: ", "그 밖의 파일: "), ("maven.config: empty", "maven.config: 비어 있음"),
                   ("extensions.xml: no extension", "extensions.xml: 확장 없음")):
        out = out.replace(en, ko)
    return re.sub(r"\(\+(\d+) more\)", r"(외 \1개)", out)


def _repos_ko(text: str) -> str:
    return "" if text.strip() == "the configured repositories" else f"(확인한 저장소: {text})"


def _no_root_why_ko(why: str) -> str:
    """루트 pom이 없는 이유(``aggregator.no_root_pom_reason``)를 한국어로 바꾼다."""
    if why == "layout.generate_aggregator is off":
        return "layout.generate_aggregator 설정이 꺼져 있습니다"
    m = re.match(r"^the top-level poms \((?P<l>.*)\) hold (?P<n>\d+) aggregator poms \((?P<a>.*)\) that are not linked to "
                 r"each other — independent applications, not separate deployable services \(MSA\)$", why)
    if m:
        return (f"최상위 pom({m.group('l')})에 서로 연결되지 않은 집계 pom {m.group('n')}개({m.group('a')})가 있습니다. "
                "서로 독립된 애플리케이션이고 MSA 서비스도 아닙니다")
    m = re.match(r"^the top-level poms \((?P<l>.*)\) do not form one application: they are not linked to each other "
                 r"\(no <module> or dependency between them\) and are not separate deployable services \(MSA\)$", why)
    if m:
        return (f"최상위 pom({m.group('l')})이 서로 연결되어 있지 않아(<module>이나 의존성이 없음) 한 애플리케이션이 "
                "아니고, MSA 서비스도 아닙니다")
    m = re.match(r"^no aggregator pom could be planned \(top-level poms: (?P<l>.*)\)$", why)
    if m:
        return f"집계 pom을 만들 수 없었습니다(최상위 pom: {m.group('l')})"
    return why


def _aggregator_ko(m: "re.Match[str]") -> str:
    out = (f"집계 pom이 없어 sbmig가 {m.group('p')} 파일을 만들었습니다({m.group('a')}, 모듈 {m.group('m')}). "
           f"이유: {m.group('why')}. 각 모듈의 기존 빌드 설정은 그대로 두었습니다.")
    if m.group("ex"):
        out += f" 서로 연결되지 않아 제외한 pom: {m.group('ex')}."
    return out


# report.json에 영어로 저장된 줄(인수인계 할 일, 후속 조치, 차단 항목의 조치 문구, 중단 사유)을 한국어로 바꾸는 패턴.
# 영어 문장을 바꾸면 패턴도 함께 바꿔야 하며, 맞지 않으면 영어 그대로 출력된다
# Stored English lines → Korean, matched against the whole line. Unknown lines (free text from the LLM, advisory text
# from the knowledge YAML, config values) stay as they are.
_W = r"(?:still takes `(?P<t>[^`]+)`|exposes an API this code no longer matches)"
_OWNER = r"(?: \(class `(?P<o>[^`]+)`\))?"

PATTERNS: dict[str, list[tuple[re.Pattern[str], Union[str, Callable[[re.Match[str]], str]]]]] = {"ko": [
    (re.compile(p), t) for p, t in (
    # 접수나 조치 시작 단계에서 실행이 중단된 사유. 웹 화면도 저널 abort의 reason을 이 표로 번역한다
    (r"^vulnerability remediation not started \(not ready\): (?P<m>.+)$",
     r"취약점 조치를 시작하지 못했습니다. 실행 환경이 갖춰지지 않았습니다: \g<m>"),
    (r"^Gradle build detected \(build\.gradle / settings\.gradle\) and no pom\.xml: only Maven projects are supported "
     r"\(convert the build to Maven first\)$",
     r"Gradle 빌드 파일(build.gradle / settings.gradle)만 있고 pom.xml은 없습니다. Maven 프로젝트만 받습니다. Maven으로 "
     r"변환한 뒤 다시 신청해 주세요."),
    (r"^no pom\.xml found \(no supported build tool\): only Maven projects are supported — .*$",
     r"pom.xml이 없어 지원하는 빌드 도구를 찾지 못했습니다. Maven 프로젝트만 받습니다."),
    (r"^'(?P<mvn>[^']*)' is not runnable \(install Maven or set build\.mvn\)$",
     r"Maven(\g<mvn>)을 실행할 수 없습니다. Maven을 설치하거나 build.mvn을 설정해 주세요."),
    (r"^'(?P<mvn>[^']*)' cannot start: (?P<p>.+) — set build\.java_home \(or JAVA_HOME\) to a JDK (?P<v>\d+) home$",
     r"Maven(\g<mvn>)이 시작되지 않습니다(\g<p>). build.java_home(또는 JAVA_HOME)을 JDK \g<v> 경로로 설정해 주세요."),
    (r"^Maven runs on Java (?P<ver>\S+) \((?P<where>.+)\), not Java (?P<want>\d+): set build\.java_home \(or JAVA_HOME\) "
     r"to a JDK \d+ home and rerun$",
     r"Maven이 Java \g<ver>에서 실행됩니다(\g<where>). Java \g<want>이 필요합니다. build.java_home(또는 JAVA_HOME)을 "
     r"JDK \g<want> 경로로 설정한 뒤 다시 실행해 주세요."),
    (r"^Maven settings file not found: (?P<p>.+) \(build\.settings_file\) — put the settings\.xml there and rerun$",
     r"Maven settings.xml을 찾지 못했습니다(\g<p>). 이 경로에 settings.xml을 넣은 뒤 다시 실행해 주세요."),
    (r"^Maven settings file is empty: (?P<p>.+) \(build\.settings_file\) — copy the settings\.xml there and rerun$",
     r"Maven settings.xml이 비어 있습니다(\g<p>). 이 경로에 settings.xml 내용을 넣은 뒤 다시 실행해 주세요."),
    (r"^build\.setup_command failed \(rc=(?P<rc>-?\d+)\): ?(?P<t>.*)$",
     r"실행 준비 명령(build.setup_command)이 실패했습니다(rc=\g<rc>). \g<t>"),
    (r"^stopped by user \(stop\.request\) — the final `mvn clean package` did not run$",
     r"사용자 요청으로 멈췄습니다. 최종 빌드(mvn clean package)는 실행하지 않았습니다."),
    (r"^stopped by user \(stop\.request\) — the fix loop was left early; the last build shows the tree as it was left$",
     r"사용자 요청으로 멈췄습니다. 오류 수정을 중간에 그만두었고, 마지막 빌드 결과가 지금 프로젝트의 상태입니다."),
    (r"^stopped by user \(web UI\) — the final `mvn clean package` did not run$",
     r"화면에서 멈춤을 요청해 멈췄습니다. 최종 빌드(mvn clean package)는 실행하지 않았습니다."),
    # 올린 ZIP이 풀기 한도를 넘어 접수하지 않은 경우(intake.max_unpacked_mb / max_entry_mb / max_compression_ratio)
    (r"^ZIP refused: it unpacks to (?P<n>[\d.,]+) MiB, over the limit of (?P<m>[\d.,]+) MiB \(intake\.max_unpacked_mb\) — "
     r"nothing was unpacked$",
     r"올린 ZIP을 풀면 \g<n> MiB가 되어 한도(\g<m> MiB)를 넘습니다. 접수하지 않았습니다."),
    (r"^ZIP refused: the entry (?P<e>.+) unpacks to (?P<n>[\d.,]+) MiB, over the limit of (?P<m>[\d.,]+) MiB "
     r"\(intake\.max_entry_mb\) — nothing was unpacked$",
     r"올린 ZIP 안의 파일 하나가 풀면 \g<n> MiB가 되어 한도(\g<m> MiB)를 넘습니다(\g<e>). 접수하지 않았습니다."),
    (r"^ZIP refused: the entry (?P<e>.+) is compressed (?P<r>[\d.,]+):1, over the limit of (?P<m>[\d.,]+):1 "
     r"\(intake\.max_compression_ratio\) — nothing was unpacked$",
     r"올린 ZIP 안의 파일 하나가 \g<r>배로 압축되어 있어 한도(\g<m>배)를 넘습니다(\g<e>). 접수하지 않았습니다."),
    (r"^ZIP refused while unpacking: the entry (?P<e>.+) wrote more than (?P<m>[\d.,]+) MiB \(intake\.max_entry_mb\) — the "
     r"ZIP's directory understates its sizes$",
     r"올린 ZIP을 푸는 중에 파일 하나가 한도(\g<m> MiB)를 넘어 멈췄습니다(\g<e>). ZIP에 적힌 크기가 실제와 다릅니다."),
    (r"^ZIP refused while unpacking: more than (?P<m>[\d.,]+) MiB written \(intake\.max_unpacked_mb\) — the ZIP's "
     r"directory understates its sizes$",
     r"올린 ZIP을 푸는 중에 전체 크기가 한도(\g<m> MiB)를 넘어 멈췄습니다. ZIP에 적힌 크기가 실제와 다릅니다."),
    (r"^ZIP refused while unpacking: the entry (?P<e>.+) expanded more than (?P<m>[\d.,]+):1 "
     r"\(intake\.max_compression_ratio\) — the ZIP's directory understates its sizes$",
     r"올린 ZIP을 푸는 중에 파일 하나가 한도(\g<m>배)보다 크게 풀려 멈췄습니다(\g<e>). ZIP에 적힌 크기가 실제와 다릅니다."),
    (r"^build tool '(?P<t>[^']+)' is not supported \(Maven only\)$",
     r"지원하지 않는 빌드 도구입니다(\g<t>). Maven 프로젝트만 받습니다."),
    (r"^no pom\.xml found in the project$", r"프로젝트에서 pom.xml을 찾지 못했습니다. Maven 프로젝트만 받습니다."),
    (r"^no pom\.xml at the project root and none was generated: (?P<why>.+) — Maven cannot build a directory without a "
     r"pom and the restructure needs a root pom; add a root pom\.xml listing the modules to build together, or run sbmig "
     r"on each application's directory$",
     lambda m: ("프로젝트 루트에 pom.xml이 없고 sbmig가 만들지도 못했습니다. " + _no_root_why_ko(m.group("why")) + ". Maven은 "
                "pom이 없는 폴더를 빌드할 수 없고 구조 재구성에도 루트 pom이 필요합니다. 함께 빌드할 모듈을 나열한 루트 "
                "pom.xml을 추가하거나, 애플리케이션 폴더마다 sbmig를 따로 실행해 주세요.")),
    (r"^ADF project — conversion is not possible \(ADF 프로젝트라 변환이 불가능합니다; intake\.adf_policy: (?P<p>\w+)\)$",
     r"ADF 프로젝트라 변환이 불가능합니다(intake.adf_policy: \g<p>)."),
    # 사내 부모 pom을 받지 못해 수정 루프가 멈춘 사유(sbmig/loop/fixloop.py _parent_abort)
    (r"^Maven cannot resolve the parent (?P<c>\S+), which is the configured version \((?P<spec>[\w.]+) = (?P<t>\S+)\) — "
     r"the repository does not serve it \(settings: (?P<s>[^)]+)\); fix the repository / settings, or the version in "
     r"[\w.]+, and rerun$",
     r"설정한 부모 pom(\g<c>)을 저장소에서 받지 못했습니다(settings: \g<s>). \g<spec>에 설정된 버전(\g<t>)입니다. 저장소나 "
     r"settings, 또는 \g<spec>의 버전을 고친 뒤 다시 실행해 주세요."),
    (r"^Maven cannot resolve the as-is parent (?P<c>\S+); the configured version is (?P<t>\S+) \((?P<spec>[\w.]+)\) — the "
     r"run stops because openrewrite\.unresolvable_asis_parent is abort \(settings: (?P<s>[^)]+)\); set it to align to "
     r"use the configured version instead$",
     r"변경 전 부모 pom(\g<c>)을 저장소에서 받지 못했습니다(settings: \g<s>). 설정된 버전은 \g<t>(\g<spec>)입니다. "
     r"openrewrite.unresolvable_asis_parent: abort 설정에 따라 실행을 멈췄습니다. 설정된 버전으로 진행하려면 이 값을 "
     r"align으로 바꿔 주세요."),
    (r"^the company parent (?P<p>\S+) declares the build extension (?P<c>\S+), which Maven cannot download from the "
     r"repository \(settings: (?P<s>[^)]+)\) — every Maven call fails before any goal; make Nexus serve its pom, jar, "
     r"parent poms and dependencies \(or drop the extension from the parent: a deploy transport is not needed for "
     r"packaging\) and rerun$",
     r"사내 부모 pom(\g<p>)이 선언한 빌드 확장(\g<c>)을 Maven이 저장소에서 받지 못했습니다(settings: \g<s>). 이 상태에서는 "
     r"모든 Maven 실행이 시작부터 실패합니다. Nexus가 이 확장의 pom, jar, 부모 pom, 의존성을 제공하도록 하거나 부모 "
     r"pom에서 확장을 뺀 뒤(패키징에는 배포용 전송 확장이 필요 없습니다) 다시 실행해 주세요."),
    # 레시피 뒤에 sbmig가 직접 고친 pom 항목(오류 목록의 메시지, Orchestrator._pom_verify_entries)
    (r"^scope restored: provided \(as-is\): (?P<k>\S+) \(was (?P<o>\S+)\)$",
     r"scope를 변경 전과 같은 provided로 되돌렸습니다(\g<k>, 레시피 뒤에는 \g<o>)"),
    (r"^source encoding written: (?:(?P<p>\S+): )?project\.build\.sourceEncoding=(?P<v>[^\s(]+)(?: \(.*\))?$",
     r"소스 인코딩을 pom에 기록했습니다(project.build.sourceEncoding=\g<v>)"),
    # 테스트를 컴파일하지 않은 실행의 후속 조치(Orchestrator, build.skip_test_compile)
    (r"^test sources were neither compiled nor run \(build\.skip_test_compile: true\): the owning team compiles and "
     r"fixes the tests \(`mvn test-compile`\) before relying on CI$",
     r"테스트 소스는 컴파일도 실행도 하지 않았습니다(build.skip_test_compile: true). CI에 맡기기 전에 담당 팀이 테스트를 "
     r"컴파일하고 고쳐 주세요(`mvn test-compile`)."),
    # 사내 프레임워크 프로젝트의 차단 표 조치 문구(sbmig/loop/fixloop.py의 _framework_parent_abort,
    # framework_owner_suggestion). 영어 문장을 바꾸면 함께 바꾼다
    (r"^parent `(?P<c>[^`]+)` is not resolvable with settings `(?P<s>[^`]+)`: this is a framework project "
     r"\((?P<fw>.+?)\) and keeps its own parent — .*nothing in the project can replace it$",
     r"부모 pom을 저장소에서 받지 못했습니다(`\g<c>`, settings `\g<s>`). 사내 프레임워크 프로젝트(\g<fw>)는 부모 pom을 "
     r"바꾸지 않으므로 저장소가 이 pom을 제공해야 합니다. 저장소에 올리거나 settings 또는 미러 설정을 고친 뒤 다시 실행해 "
     r"주세요. 프로젝트 안에서 대신할 방법은 없습니다."),
    (r"^`(?P<f>[^`]+)` is the in-house framework artifact of this project and is still compiled against javax\.\*"
     r"(?: \(it still takes `(?P<api>[^`]+)`\))?: it cannot be fixed in the project — the owner of the framework "
     r"artifact `[^`]+` must provide a jakarta build of it \(sbmig keeps the framework version as it is\)$",
     lambda m: ("이 프로젝트의 사내 프레임워크 아티팩트가 아직 javax 기준으로 빌드되어 있습니다(`" + m.group("f") + "`"
                + (", 필요한 javax API `" + m.group("api") + "`" if m.group("api") else "") + "). "
                + FRAMEWORK_OWNER_KO)),
    (r"^(?:`(?P<api>[^`]+)`|a javax-era library) comes through the framework artifact `(?P<f>[^`]+)` "
     r"\((?P<path>[^()]*)\): it cannot be fixed in the project — the owner of the framework artifact `[^`]+` must "
     r"provide a jakarta build of it \(sbmig keeps the framework version as it is\)$",
     lambda m: ("javax 기준 " + ("API" if m.group("api") else "라이브러리") + "가 사내 프레임워크 아티팩트의 의존성으로 "
                + "포함되어 있습니다(" + ("`" + m.group("api") + "`, " if m.group("api") else "") + "프레임워크 `"
                + m.group("f") + "`, 경로 "
                + " > ".join(x.strip() for x in m.group("path").split("→")) + "). " + FRAMEWORK_OWNER_KO)),
    # 사내 프레임워크 프로젝트의 부모 pom을 저장소에서 받지 못한 경우. 부모 pom을 바꾸지 않으므로 실행을 중단한다
    (r"^Maven cannot resolve the parent (?:pom )?(?P<p>.+?)[;.] .*\bframework project keeps its (?:own )?parent\b.*$",
     r"부모 pom을 저장소에서 받지 못했습니다(\g<p>). 프레임워크 프로젝트는 부모 pom을 바꾸지 않으므로 저장소에서 받을 수 "
     r"있어야 합니다."),
    (r"^internal error: (?P<e>.+)$", r"내부 오류로 멈췄습니다: \g<e>"),
    (r"^missing component: (?P<e>.+)$", r"필요한 구성 요소가 없어 멈췄습니다: \g<e>"),
    (r"^pom\.xml is an EAR \((?P<a>[^,]+), packaging ear\) that packages modules which are not in this directory: "
     r"(?P<n>.+?)\. An EAR holds no code of its own — .*$",
     r"루트 pom.xml이 EAR(\g<a>)이고, EAR이 묶는 모듈(\g<n>)이 이 폴더에 없습니다. EAR에는 코드가 없으니 모든 모듈이 들어 있는 "
     r"상위 폴더로 다시 신청해 주세요."),
    (r"^Ant build\.xml detected and no pom\.xml: the pom synthesis \(sbmig\.analyze\.ant\) is not available in this "
     r"installation \((?P<e>.+)\) — convert the build to Maven first$",
     r"Ant 프로젝트의 pom.xml을 만드는 기능을 이 설치본에서 쓸 수 없습니다(\g<e>). Maven으로 변환한 뒤 신청해 주세요."),
    (r"^pom\.xml could not be synthesized from (?P<f>\S+): (?P<e>.+) \(Ant project rejected: convert the build to Maven "
     r"first\)$",
     r"\g<f>에서 pom.xml을 만들지 못했습니다(\g<e>). Maven으로 변환한 뒤 신청해 주세요."),
    # Ant 프로젝트를 거부할 때(Orchestrator._refuse_ant) 남기는 상세 줄
    (r"^Ant build file (?P<f>\S+) found and no pom\.xml: the Ant → Maven conversion is switched off "
     r"\(intake\.ant_to_maven: false\)$",
     r"Ant 빌드 파일(\g<f>)만 있고 pom.xml은 없습니다. Ant → Maven 변환은 꺼져 있습니다(intake.ant_to_maven: false)."),
    (r"^Ant build file (?P<f>\S+) found and no pom\.xml: the remediation flow builds the project as uploaded and "
     r"needs a Maven project$",
     r"Ant 빌드 파일(\g<f>)만 있고 pom.xml은 없습니다. 취약점 조치는 올린 프로젝트를 그대로 빌드하므로 Maven 프로젝트여야 합니다."),
    # v2.1z-e: jakarta build discovery — the search record / the unmatched owner class on a blocked item
    # (sbmig.loop.fixloop JAKARTA_SEARCH_PREFIX / UNMAPPED_PREFIX; the dynamic record inside stays as written)
    ((r"^repository search for a jakarta build of (?P<key>\S+): (?P<rest>.*?) — verified build\(s\): (?P<found>.+)$"),
     r"저장소에서 \g<key> 라이브러리의 jakarta 빌드를 찾았습니다: \g<found> (조회 내역: \g<rest>)"),
    ((r"^repository search for a jakarta build of (?P<key>\S+): (?P<rest>.*?) — none verified$"),
     r"저장소에서 \g<key> 라이브러리의 jakarta 빌드를 찾아봤지만 확인된 빌드가 없습니다(조회 내역: \g<rest>)."),
    ((r"^repository search for a jakarta build of (?P<key>\S+): no repository lookup \(libraries\.lookup off or unreachable\)$"),
     r"\g<key> 라이브러리의 jakarta 빌드는 저장소에서 찾아보지 못했습니다(libraries.lookup이 꺼져 있거나 저장소에 연결할 수 "
     r"없습니다)."),
    ((r"^the library behind (?P<who>.+?) could not be matched to a dependency \(classpath, package tokens, local jars\) — "
      r"run mvn dependency:tree in the module and look the library's jakarta build up in Nexus$"),
     r"\g<who> 클래스가 속한 라이브러리를 의존성에서 찾지 못했습니다(classpath, 패키지 이름, 로컬 jar로 확인). 모듈에서 "
     r"mvn dependency:tree를 실행해 라이브러리를 찾고, Nexus에서 그 라이브러리의 jakarta 빌드를 찾아 주세요."),
    # 차단 항목에 붙는 EOL 권고(fixloop._with_eol_advisory). 제목과 대체 라이브러리는 권고표의 값 그대로다
    (r"^EOL: (?P<t>.+?)(?: — replacement: (?P<r>.+))?$",
     lambda m: "지원 종료(EOL): " + m.group("t") + (f". 대체 라이브러리: {m.group('r')}" if m.group("r") else "")),
    # v2.1z-d: the ADF first line (rejected; policy stop / continue) and the traces note
    ((r"^ADF project \(Oracle JDeveloper / ADF\): conversion is not possible — ADF 프로젝트라 변환이 불가능합니다 "
      r"\(evidence: (?P<fams>[^;]+); (?P<e>\d+) entities / (?P<t>\d+) tables, (?P<v>\d+) view objects, "
      r"(?P<a>\d+) application modules, (?P<pd>\d+) page definitions, (?P<f>\d+) task flows, (?P<p>\d+) pages, "
      r"(?P<j>\d+) of (?P<x>\d+) java files on ADF\)$"),
     (r"ADF 프로젝트라 변환이 불가능합니다(근거: \g<fams>; 엔티티 \g<e>개 / 테이블 \g<t>개, 뷰 오브젝트 \g<v>개, 애플리케이션 "
      r"모듈 \g<a>개, pageDef \g<pd>개, 태스크 플로 \g<f>개, 페이지 \g<p>개, java 파일 \g<x>개 중 \g<j>개가 ADF 사용).")),
    ((r"^ADF project \(Oracle JDeveloper / ADF\): conversion is not possible — ADF 프로젝트라 변환이 불가능합니다 "
      r"\(evidence: (?P<fams>[^;]+); (?P<e>\d+) entities / (?P<t>\d+) tables, (?P<v>\d+) view objects, "
      r"(?P<a>\d+) application modules, (?P<pd>\d+) page definitions, (?P<f>\d+) task flows, (?P<p>\d+) pages, "
      r"(?P<j>\d+) of (?P<x>\d+) java files on ADF\) — intake\.adf_policy: continue \(report only, the run goes on; "
      r"not recommended\)$"),
     (r"ADF 프로젝트라 변환이 불가능합니다(근거: \g<fams>; 엔티티 \g<e>개 / 테이블 \g<t>개, 뷰 오브젝트 \g<v>개, 애플리케이션 "
      r"모듈 \g<a>개, pageDef \g<pd>개, 태스크 플로 \g<f>개, 페이지 \g<p>개, java 파일 \g<x>개 중 \g<j>개가 ADF 사용). "
      r"intake.adf_policy: continue 설정에 따라 보고만 하고 실행은 계속했습니다(권장하지 않습니다).")),
    ((r"^ADF traces \(one evidence family only — (?P<fam>\w+): (?P<paths>.*?)\) — the conversion goes on but needs a check: "
      r"ADF 흔적: 변환은 계속하되 확인 필요 \((?P<e>\d+) entities / (?P<t>\d+) tables, (?P<v>\d+) view objects, "
      r"(?P<a>\d+) application modules, (?P<pd>\d+) page definitions, (?P<f>\d+) task flows, (?P<p>\d+) pages, "
      r"(?P<j>\d+) of (?P<x>\d+) java files on ADF\)$"),
     (r"ADF 흔적이 있습니다(근거 한 종류, \g<fam>: \g<paths>). 변환은 계속했으니 ADF 프로젝트인지 확인해 주세요(엔티티 \g<e>개 / "
      r"테이블 \g<t>개, 뷰 오브젝트 \g<v>개, 애플리케이션 모듈 \g<a>개, pageDef \g<pd>개, 태스크 플로 \g<f>개, 페이지 \g<p>개, "
      r"java 파일 \g<x>개 중 \g<j>개가 ADF 사용).")),
    # 빈 reactor와 reactor 밖 소스의 줄(sbmig.analyze.hollow.hollow_followup / outside_followup)
    ((r"^the Maven reactor compiles no sources: (?P<counts>.+?) lie outside its modules \((?P<dirs>.+?)\) and were NOT "
      r"migrated — the as-is build only packaged files produced elsewhere \(unresolved directories: (?P<unres>.+?)\); "
      r"the sources must be mapped into the modules first \(sbmig: tree profile, next increment\)$"),
     (r"Maven reactor가 컴파일하는 소스가 없습니다. 모듈 밖에 있는 소스(\g<counts>; \g<dirs>)는 전환하지 않았습니다. 기존 "
      r"빌드는 다른 곳에서 만든 파일을 묶기만 했습니다(풀지 못한 폴더 변수: \g<unres>). 소스를 먼저 모듈로 옮겨야 합니다"
      r"(sbmig에서는 다음 기능으로 지원할 예정입니다).")),
    ((r"^the Maven reactor compiles no sources: (?P<counts>.+?) lie outside its modules \((?P<dirs>.+?)\) and were NOT "
      r"migrated — the as-is build only packaged files produced elsewhere; "
      r"the sources must be mapped into the modules first \(sbmig: tree profile, next increment\)$"),
     (r"Maven reactor가 컴파일하는 소스가 없습니다. 모듈 밖에 있는 소스(\g<counts>; \g<dirs>)는 전환하지 않았습니다. 기존 "
      r"빌드는 다른 곳에서 만든 파일을 묶기만 했습니다. 소스를 먼저 모듈로 옮겨야 합니다(sbmig에서는 다음 기능으로 지원할 "
      r"예정입니다).")),
    # 모듈 복사본 pom을 무시했다는 줄. sbmig/loop/orchestrator.py의 stray_pom_followup을 바꾸면 함께 바꾼다
    ((r"^pom copy ignored: `(?P<p>[^`]+)` has the same coordinates as `(?P<q>[^`]+)` \(`(?P<c>[^`]+)`\), lies inside the "
      r"directory of `(?P<q2>[^`]+)` and no pom lists it as a module — treated as a stray copy \(an IDE output / backup "
      r"folder\): not built, not migrated, left where it is; delete it from the source, or give it its own coordinates and "
      r"list it in <modules> if it is a real module$"),
     (r"pom 복사본을 무시했습니다. `\g<p>` 파일은 `\g<q>` 파일과 좌표가 같고(`\g<c>`), `\g<q2>` 폴더 안에 있으며, 어느 pom도 "
      r"모듈로 나열하지 않습니다. IDE 출력이나 백업 폴더에 남은 복사본으로 보고 빌드와 전환에서 제외했으며 파일은 그대로 "
      r"두었습니다. 소스에서 지우거나, 실제 모듈이면 좌표를 따로 정하고 <modules>에 추가해 주세요.")),
    ((r"^pom copy ignored: `(?P<p>[^`]+)` has the same coordinates as `(?P<q>[^`]+)` \(`(?P<c>[^`]+)`\), lies beside the "
      r"module `(?P<q2>[^`]+)` and no pom lists it as a module — treated as a stray copy \(an IDE output / backup "
      r"folder\): not built, not migrated, left where it is; delete it from the source, or give it its own coordinates and "
      r"list it in <modules> if it is a real module$"),
     (r"pom 복사본을 무시했습니다. `\g<p>` 파일은 `\g<q>` 파일과 좌표가 같고(`\g<c>`), 모듈 `\g<q2>` 옆에 있으며, 어느 pom도 "
      r"모듈로 나열하지 않습니다. IDE 출력이나 백업 폴더에 남은 복사본으로 보고 빌드와 전환에서 제외했으며 파일은 그대로 "
      r"두었습니다. 소스에서 지우거나, 실제 모듈이면 좌표를 따로 정하고 <modules>에 추가해 주세요.")),
    # Maven의 HTTP 차단 미러 안내 줄. sbmig/build/env_errors.py의 http_blocked_followup을 바꾸면 함께 바꾼다
    ((r"^Maven \(3\.8\.1\+\) refuses the HTTP repositor(?:y|ies) (?P<r>.+?) via the blocked mirror `(?P<m>[^`]+)` — not a "
      r"project or sbmig problem\. Fix the Maven settings: \(a\) an https URL, or \(b\) in (?P<s>.+?) \((?P<o>[\w.]+)\) "
      r"exempt (?P<i0>.+?) from mirror `(?P<m2>[^`]+)` \(append `,!<repository id>` to its `<mirrorOf>`\), or add a mirror "
      r"for (?P<i>.+?) \(same URL, "
      r"`<blocked>false</blocked>`\); blocker from (?P<g>.+)$"),
     (r"Maven(3.8.1 이상)이 HTTP 저장소 \g<r> 접근을 차단 미러 `\g<m>`에서 막았습니다. 프로젝트나 sbmig 문제가 아닙니다. "
      r"Maven settings를 다음 중 하나로 고쳐 주세요. (a) 저장소 URL을 https로 바꾸기. (b) \g<s> (\g<o>)에서 차단 미러 "
      r"`\g<m2>`의 `<mirrorOf>` 끝에 `,!<저장소 ID>`를 붙여 \g<i0> 저장소를 제외하거나, \g<i> 저장소용 미러를 추가하기(같은 "
      r"URL, `<blocked>false</blocked>`). 차단 미러를 정의한 파일: \g<g>")),
    # 같은 원인으로 실행을 멈춘 사유(fixloop.http_blocked_abort)와 조치 요약에 쓰는 문장
    ((r"^Maven \(3\.8\.1\+\) refuses the HTTP repositor(?:y|ies) (?P<r>.+?) via the blocked mirror `(?P<m>[^`]+)` — not a "
      r"project or sbmig problem(?P<abort> — run aborted: fix the Maven settings \(see the Handover\))?$"),
     lambda m: ("Maven(3.8.1 이상)이 HTTP 저장소 " + m.group("r") + " 접근을 차단 미러 `" + m.group("m") + "`에서 막았습니다. "
                "프로젝트나 sbmig 문제가 아닙니다."
                + (" Maven settings를 고쳐 주세요(「확인해 주세요」 참고)." if m.group("abort") else ""))),
    # 사내 부모의 기존 버전이 없을 때와 레시피 없이 계속할 때의 줄.
    # sbmig/loop/orchestrator.py와 fixloop.py를 바꾸면 함께 바꾼다
    ((r"^as-is parent `(?P<o>[^`]+)` is not in the repository: sbmig set the configured `(?P<t>[^`]+)` \((?P<s>[\w.]+)\) in "
      r"(?P<p>.+?) before the recipes — the recipes ran on the target parent's classpath, not the as-is one \(code that only "
      r"compiled against the old parent was fixed by the build loop\)$"),
     (r"변경 전 부모 pom(`\g<o>`)을 저장소에서 찾지 못해, 레시피를 실행하기 전에 sbmig가 \g<p> 파일에 설정된 부모 "
      r"`\g<t>`(\g<s>)를 적었습니다. 그래서 레시피는 기존 부모가 아니라 새 부모 pom의 클래스패스로 실행했습니다(기존 부모로만 "
      r"컴파일되던 코드는 빌드 오류 수정 단계에서 고쳤습니다).")),
    ((r"^as-is parent `(?P<o>[^`]+)` is not in the repository: sbmig set the configured `(?P<t>[^`]+)` \((?P<s>[\w.]+)\) in "
      r"(?P<p>.+?) \(fix loop, sbmig-parent\)$"),
     (r"변경 전 부모 pom(`\g<o>`)을 저장소에서 찾지 못해 sbmig가 \g<p> 파일에 설정된 부모 `\g<t>`(\g<s>)를 적었습니다"
      r"(빌드 오류 수정 단계, sbmig-parent).")),
    ((r"^OpenRewrite did not run: the as-is parent `(?P<o>[^`]+)` is not in the repository — no recipe changed the code; the "
      r"restructure replaced the parent and the fix loop did the rest \(check the repository / settings if that parent "
      r"should be there\)$"),
     (r"변경 전 부모 pom(`\g<o>`)이 저장소에 없어 OpenRewrite를 실행하지 못했습니다. 레시피로 바뀐 코드는 없고, 구조 "
      r"재구성에서 부모 pom을 바꾼 뒤 나머지는 빌드 오류 수정 단계에서 처리했습니다(그 부모 pom이 저장소에 있어야 한다면 "
      r"저장소와 settings를 확인해 주세요).")),
    # 프로젝트에 포함된 jar를 pom에 추가했다는 줄.
    # sbmig/restructure/system_jars.py의 bundled_jar_followup을 바꾸면 함께 바꾼다. 수정 루프가 앞에 ⚠를 붙일 수 있다
    ((r"^(?P<w>⚠ ?)?bundled jar `(?P<n>[^`]+)` \(Maven coordinates unknown — no pom\.properties, not in the jar name table\) "
      r"wired into `(?P<p>[^`]+)` as a system dependency `(?P<c>[^`]+)` — wired by sbmig so the build sees the classes the "
      r"as-is IDE build saw; publish the jar to Nexus \((?P<cmd>`[^`]+`)\) and replace the system dependency with the real "
      r"coordinates$"),
     (r"\g<w>프로젝트에 포함된 jar `\g<n>` 파일을 `\g<p>`에 system 의존성으로 추가했습니다(임시 좌표 `\g<c>`. jar 안에 "
      r"pom.properties가 없고 jar 이름표에도 없어 Maven 좌표를 모릅니다). 기존 IDE 빌드가 쓰던 클래스를 Maven 빌드에서도 "
      r"쓸 수 있도록 sbmig가 추가한 것입니다. jar를 Nexus에 올리고(\g<cmd>) system 의존성을 실제 좌표로 바꿔 주세요.")),
    ((r"^(?P<w>⚠ ?)?bundled jar `(?P<n>[^`]+)` \((?P<src>\w+) coordinates, not in the repository\) "
      r"wired into `(?P<p>[^`]+)` as a system dependency `(?P<c>[^`]+)` — wired by sbmig so the build sees the classes the "
      r"as-is IDE build saw; publish the jar to Nexus \((?P<cmd>`[^`]+`)\) and replace the system dependency with the real "
      r"coordinates$"),
     (r"\g<w>프로젝트에 포함된 jar `\g<n>` 파일을 `\g<p>`에 system 의존성으로 추가했습니다(좌표 `\g<c>`, \g<src>에서 읽은 "
      r"좌표이지만 저장소에 없습니다). 기존 IDE 빌드가 쓰던 클래스를 Maven 빌드에서도 쓸 수 있도록 sbmig가 추가한 것입니다. "
      r"jar를 Nexus에 올리고(\g<cmd>) system 의존성을 실제 좌표로 바꿔 주세요.")),
    ((r"^(?P<w>⚠ ?)?bundled jar `(?P<n>[^`]+)` wired into `(?P<p>[^`]+)` as a compile dependency `(?P<c>[^`]+)` "
      r"\(the repository has it\)$"),
     r"\g<w>프로젝트에 포함된 jar `\g<n>` 파일을 `\g<p>`에 일반(compile) 의존성으로 추가했습니다(좌표 `\g<c>`, 저장소에 "
     r"있습니다)."),
    # 참조가 없어 선언하지 않은 jar의 줄. sbmig/restructure/bundled_jars.py의 unused_jar_followup을 바꾸면 함께 바꾼다
    ((r"^bundled jar `(?P<n>[^`]+)` is referenced by no source / JSP / XML / properties file — not declared \(add it by "
      r"hand if a runtime needs it\)$"),
     r"프로젝트에 포함된 jar `\g<n>` 파일은 어느 소스 / JSP / XML / properties 파일에서도 참조하지 않아 pom에 선언하지 "
     r"않았습니다(실행에 필요하면 직접 추가해 주세요)."),
    # 저장소에 없고 intake.unknown_jar가 skip이어서 추가하지 않은 jar(fixloop)
    ((r"^bundled jar `(?P<n>[^`]+)` provides package `(?P<k>[^`]+)` needed by `(?P<p>[^`]+)` but is not in the repository "
      r"and intake\.unknown_jar is skip — declare it as a Maven dependency \(publish it to Nexus first\)$"),
     r"프로젝트에 포함된 jar `\g<n>` 파일이 `\g<p>`에 필요한 패키지(`\g<k>`)를 제공하지만, 저장소에 없고 "
     r"intake.unknown_jar가 skip이어서 추가하지 않았습니다. Nexus에 올린 뒤 Maven 의존성으로 선언해 주세요."),
    # 웹 루트를 옮긴 뒤 남은 WEB-INF/lib, classes, META-INF 파일의 줄. system_jars.webinf_left_followup을 바꾸면 함께 바꾼다
    ((r"^(?P<k>\d+) file\(s\) under WEB-INF/lib, WEB-INF/classes or META-INF stay in (?P<d>.+?) \(not packaged\): "
      r"jar\(s\) (?P<j>.+?) — sbmig wires a bundled jar into its module pom when a build error needs it \(`sbmig-bundled`\), "
      r"declare the others as Maven dependencies; move the classes into src/main/java$"),
     (r"WEB-INF/lib, WEB-INF/classes, META-INF 아래 파일 \g<k>개가 \g<d> 폴더에 남아 있습니다(패키징에 포함되지 않습니다). "
      r"jar: \g<j>. 빌드 오류에 필요한 jar는 sbmig가 모듈 pom에 추가합니다(`sbmig-bundled`). 나머지 jar는 Maven 의존성으로 "
      r"선언하고, 클래스는 src/main/java로 옮겨 주세요.")),
    ((r"^(?P<k>\d+) file\(s\) under WEB-INF/lib, WEB-INF/classes or META-INF stay in (?P<d>.+?) \(not packaged\): "
      r"jar\(s\) (?P<j>.+?) — sbmig wires a bundled jar into its module pom when a build error needs it \(`sbmig-bundled`\), "
      r"declare the others as Maven dependencies$"),
     (r"WEB-INF/lib, WEB-INF/classes, META-INF 아래 파일 \g<k>개가 \g<d> 폴더에 남아 있습니다(패키징에 포함되지 않습니다). "
      r"jar: \g<j>. 빌드 오류에 필요한 jar는 sbmig가 모듈 pom에 추가합니다(`sbmig-bundled`). 나머지 jar는 Maven 의존성으로 "
      r"선언해 주세요.")),
    ((r"^(?P<k>\d+) file\(s\) under WEB-INF/lib, WEB-INF/classes or META-INF stay in (?P<d>.+?) \(not packaged\): "
      r"declare the jars as Maven dependencies / move the classes into src/main/java$"),
     (r"WEB-INF/lib, WEB-INF/classes, META-INF 아래 파일 \g<k>개가 \g<d> 폴더에 남아 있습니다(패키징에 포함되지 않습니다). "
      r"jar는 Maven 의존성으로 선언하고, 클래스는 src/main/java로 옮겨 주세요.")),
    ((r"^sources outside the Maven reactor: (?P<counts>.+?) lie outside the modules' source roots \((?P<dirs>.+?)\) and "
      r"are not built or migrated — check whether they belong to the application \(a stale copy, a tool, or sources the "
      r"as-is build reached in another way\)$"),
     (r"Maven reactor 밖에 소스가 있습니다. 모듈의 소스 루트 밖에 있는 소스(\g<counts>; \g<dirs>)는 빌드와 전환에서 빠졌습니다. "
      r"애플리케이션에 속한 파일인지 확인해 주세요(오래된 복사본, 도구, 기존 빌드가 다른 방법으로 쓰던 소스일 수 있습니다).")),
    (r'^operator checks: (?P<n>\d+) major upgrade\(s\) — see "Operator checks" above$',
     r"메이저 버전을 올린 변경이 \g<n>건 있습니다. 「운영자 확인 필요」를 봐 주세요."),
    (r"^(?P<n>\d+) cluster\(s\) blocked — see Blocked$", r"진행하지 않은 항목이 \g<n>건 있습니다. 「진행하지 않은 항목」을 봐 주세요."),
    (r"^(?P<n>\d+) unresolved error\(s\) — see Error list$",
     r"해결하지 못한 오류가 \g<n>건 있습니다. 「해결하지 못한 오류」를 봐 주세요."),
    ((r"^test sources do not compile yet \(main code builds with -Dmaven\.test\.skip=true\) — fix the tests "
      r"before relying on CI$"),
     (r"테스트 소스는 아직 컴파일되지 않습니다(메인 코드는 -Dmaven.test.skip=true로 빌드했습니다). CI에 맡기기 전에 테스트를 "
      r"고쳐 주세요.")),
    ((r"^(?P<k>\S+) pinned at (?P<v>\S+) — the new parent/Boot BOM no longer manages it: confirm the version or "
      r"replace$"),
     r"\g<k> 의존성은 새 부모 pom과 Spring Boot BOM에 버전이 없어 기존 버전(\g<v>)을 pom에 직접 적었습니다. 이 버전이 맞는지 "
     r"확인하거나 다른 라이브러리로 교체해 주세요."),
    ((r"^(?P<n>\d+) dependencies are pinned at their as-is versions because the new parent / Boot BOM no longer "
      r"manages them \(⚠ in Versions replaced\) — confirm or replace each; the first (?P<m>\d+):$"),
     (r"의존성 \g<n>개는 새 부모 pom과 Spring Boot BOM에 버전이 없어 기존 버전을 pom에 직접 적었습니다(「전환 내용」의 ⚠ "
      r"표시). 각각 버전을 확인하거나 교체해 주세요. 처음 \g<m>개:")),
    ((r"^code rewritten by the LLM for (?P<lib>[^:]+): (?P<rest>.+) — review these changes \(changes\.patch, "
      r"Fixes by case\)$"),
     r"\g<lib> 라이브러리 때문에 LLM이 고친 코드: \g<rest>. 변경 내용을 확인해 주세요(「코드 수정 내역」, changes.patch)."),
    (r"^verify (?P<lib>.+?) → (?P<repl>.+?) \(replaced during the run\)(?P<rest>: .+)?$",
     r"교체한 라이브러리를 확인해 주세요: \g<lib> → \g<repl> (실행 중 교체)\g<rest>"),
    ((r"^(?P<n>\d+) externalized setting\(s\) need values before the first start — placeholders "
      r"\((?P<s>[^)]*)\) in (?P<files>.+); see 'Properties to set'$"),
     (r"외부 설정 \g<n>개는 처음 기동하기 전에 값을 넣어야 합니다. 임시 값(\g<s>)을 넣어 둔 파일: \g<files>. 「실행 전에 "
      r"설정할 프로퍼티」를 봐 주세요.")),
    ((r"^(?P<n>\d+) externalized setting\(s\) need values before the first start — no placeholders were "
      r"written \(see the table\); see 'Properties to set'$"),
     (r"외부 설정 \g<n>개는 처음 기동하기 전에 값을 넣어야 합니다. 임시 값은 넣지 않았습니다(표 참고). 「실행 전에 설정할 "
      r"프로퍼티」를 봐 주세요.")),
    (r"^… and (?P<n>\d+) more — see Manual follow-ups / Blocked / Error list$",
     r"… 외 \g<n>건이 더 있습니다. 「확인해 주세요」, 「진행하지 않은 항목」, 「해결하지 못한 오류」를 봐 주세요."),
    (r"^(?P<p>.+): kept the copy from (?P<k>.+?), dropped (?P<d>.+?) \(saved: (?P<s>.*)\)$",
     r"\g<p>: \g<k> 모듈의 파일을 남기고 \g<d> 모듈의 파일은 뺐습니다(보관: \g<s>)"),
    (r"^(?P<n>\d+) identical duplicate\(s\) dropped \(same content in every module\): (?P<names>.+)$",
     r"모든 모듈에서 내용이 같은 중복 파일 \g<n>개를 하나로 합쳤습니다: \g<names>"),
    (r"^as-is effective dependencyManagement unavailable — versions the as-is parent managed are shown as '-'$",
     r"변경 전 effective dependencyManagement를 얻지 못했습니다. 기존 부모 pom이 지정하던 버전은 '-'로 표시했습니다."),
    (r"^handover unavailable: (?P<x>.+)$", r"전환 내용을 정리하지 못했습니다: \g<x>"),
    # v2 structure review follow-ups (handover.review_followups)
    ((r"^structure review fix \(edits / script\) changed (?P<n>\d+) file\(s\) for (?P<k>\d+) critical issue\(s\): "
      r"(?P<files>.+) \(commit (?P<c>[^)]+)\) — check these pom changes$"),
     r"구조 점검에서 심각한 문제 \g<k>건을 고치면서 파일 \g<n>개를 바꿨습니다: \g<files> (커밋 \g<c>). 이 pom 변경을 확인해 "
     r"주세요."),
    (r"^structure review: critical issue not fixed — (?P<rest>.+)$", r"구조 점검에서 찾았지만 고치지 못한 심각한 문제: \g<rest>"),
    (r"^structure review warning — (?P<rest>.+)$", r"구조 점검 경고: \g<rest>"),
    (r"^… and (?P<n>\d+) more structure review issue\(s\) — see Structure review$",
     r"구조 점검에서 나온 문제가 \g<n>건 더 있습니다. report.json의 `structure_review`에서 볼 수 있습니다."),
    # 구조 점검을 하지 않은 이유(sbmig/review/reviewer.py). review_summary의 {note}로 들어온다
    (r"^review\.enabled is off$", r"review.enabled 설정이 꺼져 있습니다"),
    (r"^no LLM client for this run \(dry run or LLM off\)$", r"이번 실행에는 LLM이 없습니다(dry run이거나 LLM이 꺼져 있습니다)"),
    (r"^no pom\.xml in the workspace$", r"작업 폴더에 pom.xml이 없습니다"),
    (r"^review error: (?P<e>.+)$", r"점검 중 오류: \g<e>"),
    # 비용 절의 단가 기준(config/default.yml llm.pricing_note 기본값)
    (r"^OpenAI list prices as of (?P<d>\S+?)(?P<tail> \(developers\.openai\.com model pages\); the company gateway may "
     r"bill differently)?$",
     lambda m: (f"OpenAI 공개 가격({m.group('d')} 기준"
                + (", developers.openai.com 모델 페이지). 사내 게이트웨이의 실제 청구 금액은 다를 수 있습니다"
                   if m.group("tail") else ")"))),
    # v2.1h (A5): JSPs whose javax EE names were not renamed (handover.jsp_javax_item)
    ((r"^JSP javax references remain \((?P<n>\d+) files?\) — rename the javax EE names in page imports / scriptlets "
      r"/ useBean to jakarta\.\* \(the build passes, Tomcat 10\+ fails at runtime\); `layout\.jsp_jakarta_rename` is "
      r"off$"),
     (r"JSP에 javax 참조가 남아 있습니다(파일 \g<n>개). page import / 스크립틀릿 / useBean의 javax EE 이름을 jakarta.*로 "
      r"바꿔 주세요(빌드는 통과하지만 Tomcat 10 이상에서 실행하면 실패합니다). `layout.jsp_jakarta_rename` 설정이 꺼져 "
      r"있습니다.")),
    ((r"^JSP javax references remain \((?P<n>\d+) files?\) — rename the javax EE names in page imports / scriptlets "
      r"/ useBean to jakarta\.\* \(the build passes, Tomcat 10\+ fails at runtime\)$"),
     (r"JSP에 javax 참조가 남아 있습니다(파일 \g<n>개). page import / 스크립틀릿 / useBean의 javax EE 이름을 jakarta.*로 "
      r"바꿔 주세요(빌드는 통과하지만 Tomcat 10 이상에서 실행하면 실패합니다).")),
    # reactor에 중복으로 나열된 모듈을 정리한 줄. sbmig/analyze/aggregator.py의 reactor_followups 문장을 바꾸면 함께 바꾼다
    ((r"^(?P<d>.+?) was listed (?:twice|\d+ times) in the reactor \((?P<poms>[^)]+)\); the (?P<a>.+?) listings? "
      r"(?:was|were) removed so Maven can build — if (?P<b>.+?) is meant to be built on its own, add (?P=d) back "
      r"there and remove it from the root$"),
     (r"\g<d> 모듈이 reactor에 중복으로 포함되어 있었습니다(\g<poms>). Maven이 빌드할 수 있도록 \g<a> 쪽 목록에서 "
      r"지웠습니다. \g<b> 쪽을 따로 빌드해야 한다면 그쪽에 \g<d> 모듈을 다시 넣고 루트 pom에서 빼 주세요.")),
    ((r"^(?P<d>.+?) was listed (?:twice|\d+ times) in the reactor \((?P<poms>[^)]+)\); the (?P<a>.+?) listings? "
      r"(?:was|were) removed so Maven can build — if (?P<b>.+?) is meant to be built on its own, add (?P=d) back "
      r"there and remove it from (?P<k>.+)$"),
     (r"\g<d> 모듈이 reactor에 중복으로 포함되어 있었습니다(\g<poms>). Maven이 빌드할 수 있도록 \g<a> 쪽 목록에서 "
      r"지웠습니다. \g<b> 쪽을 따로 빌드해야 한다면 그쪽에 \g<d> 모듈을 다시 넣고 \g<k> 쪽에서 빼 주세요.")),
    ((r"^(?P<d>.+?) was listed more than once in the same <modules> of (?P<p>.+?); the repeated listing was removed "
      r"so Maven can build$"),
     r"\g<d> 모듈이 \g<p> 파일의 같은 <modules>에 두 번 이상 있었습니다. Maven이 빌드할 수 있도록 반복된 줄을 지웠습니다."),
    ((r"^(?P<d>.+?) is listed more than once by the root pom \((?P<p>[^)]+)\): left as it is — remove the extra "
      r"<module> by hand so Maven can build$"),
     (r"\g<d> 모듈이 루트 pom(\g<p>)에 두 번 이상 있어 그대로 두었습니다. Maven이 빌드할 수 있도록 남는 <module> 줄을 직접 "
      r"지워 주세요.")),
    ((r"^(?P<d>.+?) is also listed by profile '(?P<id>[^']+)' of (?P<p>.+?) \(not active by default\): building with "
      r"-P(?P=id) lists (?P=d) twice in the reactor — remove it from one of them if that profile is still used$"),
     (r"\g<d> 모듈은 \g<p> 파일의 프로필 '\g<id>'(기본으로 꺼져 있음)에도 있습니다. -P\g<id> 옵션으로 빌드하면 reactor에 두 번 "
      r"포함됩니다. 그 프로필을 아직 쓴다면 둘 중 한 곳에서 빼 주세요.")),
    # 차단 표의 reactor_duplicate 조치 문구(sbmig/loop/blocked_registry.py reactor_duplicate_suggestion)
    ((r"^`(?P<c>[^`]+)` is reached more than once through the reactor's <module> lists \(e\.g\. the root pom lists DEV "
      r"and a nested aggregator lists \.\./DEV\): keep one listing per module — the intake removes the duplicate "
      r"listings of nested aggregators, this one it could not \(two listings in the root pom, two directories with "
      r"the same coordinates, or a profile\); fix the poms by hand, the LLM is not asked$"),
     (r"`\g<c>` 프로젝트가 reactor의 <module> 목록에 두 번 이상 포함되어 있습니다(예: 루트 pom의 DEV와 안쪽 집계 pom의 "
      r"../DEV). 모듈마다 한 줄만 남겨 주세요. 안쪽 집계 pom의 중복은 접수 단계에서 지우지만 이 경우는 지우지 못했습니다(루트 "
      r"pom 안의 두 줄, 좌표가 같은 두 폴더, 또는 프로필). pom을 직접 고쳐 주세요. LLM에는 맡기지 않았습니다.")),
    # 차단 표의 file_lock 조치 문구(sbmig/loop/blocked_registry.py FILE_LOCK_SUGGESTION)
    ((r"^the build output \(target/\) was locked by another process: exclude the project's target/ from the antivirus "
      r"real-time scan, close any IDE / Explorer window / leftover java process holding it, then run again; the LLM "
      r"is not asked$"),
     (r"빌드 결과 폴더(target/)를 다른 프로세스가 잠그고 있었습니다. 백신 실시간 검사에서 프로젝트의 target/ 폴더를 예외로 "
      r"등록하고, 열려 있는 IDE나 탐색기 창, 남아 있는 java 프로세스를 닫은 뒤 다시 실행해 주세요. LLM에는 맡기지 "
      r"않았습니다.")),
    # 차단 표의 needs_owner 조치 문구. 같은 원인의 오류와 LLM이 쓴 문장이 없을 때의 문구다
    # LLM이 쓴 문장 자체는 번역하지 않는다
    (r"^same cause as (?P<c>\w+): (?P<s>.+) — 소유팀 결정 필요$",
     r"앞의 오류(\g<c>)와 원인이 같습니다. \g<s> — 담당 팀의 결정이 필요합니다."),
    # LLM이 쓴 원인 문장 뒤에 붙는 꼬리(fixloop NEEDS_OWNER_KO). 문장 자체는 그대로 둔다
    (r"^(?!same cause as |the LLM named a blocker )(?P<s>.+?) — 소유팀 결정 필요$", r"\g<s> — 담당 팀의 결정이 필요합니다."),
    ((r"^the LLM named a blocker outside the project \(a library without a jakarta build, a framework to replace, an "
      r"artifact the repository lacks\) — 소유팀 결정 필요$"),
     (r"LLM이 프로젝트 밖의 원인을 짚었습니다(jakarta 빌드가 없는 라이브러리, 교체해야 할 프레임워크, 저장소에 없는 "
      r"아티팩트). 담당 팀의 결정이 필요합니다.")),
    # 차단 표의 조치 문구(sbmig/loop/blocked_registry.py compose_suggestion, _via_suggestion)
    ((r"^`(?P<v>[^`]+)` is `(?P<k>[^`]+)` under its earlier coordinates → `(?P<n>[^`]+)` \(jakarta\.\* since "
      r"(?P<s>[^,)]+)(?:, requires Java (?P<j>[^)]+))?\): relocate the declaration \(it drags in `(?P<api>[^`]+)`, "
      r"direct dependency of (?P<w>.+)\)$"),
     lambda m: (f"`{m.group('v')}` 의존성은 `{m.group('k')}` 라이브러리의 예전 좌표입니다. 새 좌표 `{m.group('n')}` "
                f"버전으로 선언을 바꿔 주세요({m.group('s')}부터 jakarta.*"
                + (f", Java {m.group('j')} 이상 필요" if m.group("j") else "")
                + f"). 지금 선언은 `{m.group('api')}` jar를 함께 가져옵니다({_where_ko(m.group('w'))}의 직접 의존성).")),
    ((r"^`(?P<api>[^`]+)` comes through `(?P<v>[^`]+)` \(direct dependency of (?P<w>[^)]+)\): `(?P<k>[^`]+)` moved to "
      r"jakarta\.\* in (?P<s>\S+) — raise it to (?P<ver>\S+?)(?: \(requires Java (?P<j>[^)]+)\))?(?: \((?P<note>.*)\))?$"),
     lambda m: (f"`{m.group('api')}` jar는 `{m.group('v')}` 라이브러리를 거쳐 클래스패스에 포함됩니다"
                f"({_where_ko(m.group('w'))}의 직접 의존성). `{m.group('k')}` 라이브러리는 {m.group('s')} 버전부터 "
                f"jakarta.*를 씁니다. {m.group('ver')} 버전으로 올려 주세요"
                + (f"(Java {m.group('j')} 이상 필요)" if m.group("j") else "") + "."
                + (f" 참고: {m.group('note')}" if m.group("note") else ""))),
    ((r"^`(?P<v>[^`]+)` is an in-house artifact still compiled against javax \(it drags in `(?P<api>[^`]+)`\): it needs "
      r"a Jakarta build (?:on the (?P<line>\S+) line|on the configured in-house version line); add it to "
      r"`artifacts\.optional` with that version so the optional bump moves it$"),
     lambda m: (f"`{m.group('v')}` 사내 아티팩트가 아직 javax 기준으로 컴파일되어 있습니다(`{m.group('api')}` jar를 함께 "
                "가져옵니다). " + (f"{m.group('line')} 버전대의 " if m.group("line") else "사내 버전대의 ")
                + "Jakarta 빌드가 필요합니다. 그 버전을 `artifacts.optional`에 추가하면 사내 선택 아티팩트 버전 맞춤에서 함께 "
                "올라갑니다.")),
    ((r"^`(?P<api>[^`]+)` comes through `(?P<v>[^`]+)` \(direct dependency of (?P<w>[^)]+)\), which is still compiled "
      r"against javax: upgrade it to a Jakarta release \(check its changelog\) or replace it; left unchanged on purpose$"),
     lambda m: (f"`{m.group('api')}` jar는 `{m.group('v')}` 라이브러리를 거쳐 클래스패스에 포함됩니다"
                f"({_where_ko(m.group('w'))}의 직접 의존성). 이 라이브러리는 아직 javax 기준으로 컴파일되어 있습니다. "
                f"Jakarta를 지원하는 릴리스로 올리거나(변경 이력 확인) 다른 라이브러리로 교체해 주세요. {LEFT_ALONE_KO}")),
    ((r"^`(?P<api>[^`]+)` is declared in (?P<pom>.+?) — remove it \(its jakarta\.\* successor comes with the Spring Boot "
      r"starters\)$"),
     lambda m: (f"`{m.group('api')}` jar가 {_where_ko(m.group('pom'))}에 직접 선언되어 있습니다. 선언을 지워 주세요"
                "(jakarta.* 버전은 Spring Boot starter에 들어 있습니다).")),
    ((r"^`(?P<k>[^`]+)` is a configured optional artifact; its version bump to `(?P<v>[^`]+)` happens in the restructure "
      r"— if this is still blocked after the bump, `[^`]+` itself still exposes javax: ask the library owner for a "
      r"Jakarta build$"),
     (r"`\g<k>` 라이브러리는 설정된 사내 선택 아티팩트여서 구조 재구성에서 `\g<v>` 버전으로 올립니다. 올린 뒤에도 이 항목이 "
      r"남으면 `\g<v>` 버전도 아직 javax를 쓰는 것이므로, 라이브러리 담당자에게 Jakarta 빌드를 요청해 주세요.")),
    ((r"^`(?P<a>[^`]+)`" + _OWNER + " " + _W + r"; the javac error only names the javax type; run `(?P<cmd>mvn "
      r"dependency:tree -Dincludes=[^`]+)` in the module to find the library that still uses it, then upgrade that "
      r"library to a Jakarta release or replace it$"),
     lambda m: (_api_subject(m) + f". javac 오류에는 javax 타입만 나옵니다. 모듈에서 `{m.group('cmd')}`를 실행해 이 타입을 "
                "아직 쓰는 라이브러리를 찾고, 그 라이브러리를 Jakarta 릴리스로 올리거나 교체해 주세요.")),
    ((r"^`(?P<a>[^`]+)`" + _OWNER + " " + _W + r"; upgrade `(?P<k>[^`]+)` to a Jakarta build \(add it to "
      r"artifacts\.optional with that version\) or wrap the call with a shim; left unchanged on purpose$"),
     lambda m: (_api_subject(m) + f". `{m.group('k')}` 라이브러리를 Jakarta 빌드로 올리거나(그 버전으로 artifacts.optional에 "
                f"추가) 호출 부분을 감싸는 shim을 만들어 주세요. {LEFT_ALONE_KO}")),
    ((r"^`(?P<a>[^`]+)`" + _OWNER + " " + _W + r"; upgrade `(?P<k>[^`]+)` to a compatible version or adapt the call by "
      r"hand; left unchanged on purpose$"),
     lambda m: (_api_subject(m) + f". `{m.group('k')}` 라이브러리를 호환되는 버전으로 올리거나 호출 부분을 직접 고쳐 "
                f"주세요. {LEFT_ALONE_KO}")),
    ((r"^`(?P<o>[^`]+)` " + _W + r"; owner class `[^`]+` could not be attributed to an artifact; check the module "
      r"classpath \(mvn dependency:tree\) and upgrade that library(?P<jk> to a Jakarta build)?$"),
     lambda m: (f"클래스 `{m.group('o')}`" + (f"가 아직 `{m.group('t')}` 타입을 받습니다" if m.group("t")
                                             else "의 API가 이 코드와 맞지 않습니다")
                + ". 이 클래스가 어느 아티팩트에 속하는지 찾지 못했습니다. 모듈의 클래스패스(mvn dependency:tree)를 확인해 "
                + "그 라이브러리를 " + ("Jakarta 빌드로 " if m.group("jk") else "") + "올려 주세요.")),
    ((r"^a library API " + _W + r"(?: \(project supplies `(?P<f>[^`]+)`\))?; the owning class could not be determined — "
      r"check the module classpath \(mvn dependency:tree\) for a javax-based library and upgrade it to a Jakarta build$"),
     lambda m: ("라이브러리 API가 " + (f"아직 `{m.group('t')}` 타입을 받습니다" if m.group("t") else "이 코드와 맞지 않습니다")
                + (f"(프로젝트 코드는 `{m.group('f')}` 타입을 넘깁니다)" if m.group("f") else "")
                + ". 어느 클래스인지 찾지 못했습니다. 모듈의 클래스패스(mvn dependency:tree)에서 javax 기준 라이브러리를 찾아 "
                "Jakarta 빌드로 올려 주세요.")),
    # 프로젝트의 .mvn 폴더를 빌드에 쓰지 않았다는 안내다. 폴더에 든 것과 명령줄로 넘긴 옵션을 함께 알린다
    ((r"^the project's \.mvn/ folders? (?:was|were) not used for the builds of this run \((?P<w>.*)\); carried over to the "
      r"command line: (?P<tail>.+?); the folders? (?:is|are) delivered unchanged — check (?:it|them) against JDK "
      r"(?P<n>\d+) and the Maven version in use before building with (?:it|them)$"),
     lambda m: ("프로젝트의 `.mvn` 폴더는 이번 실행의 빌드에 쓰지 않았습니다(" + _mvn_holds_ko(m.group("w")) + "). "
                + ("빌드에 필요한 옵션은 없어 넘긴 것이 없습니다." if m.group("tail") == "nothing"
                   else f"이 가운데 `{m.group('tail')}` 옵션은 명령줄로 넘겨 빌드에 반영했습니다.")
                + f" 폴더는 올리신 그대로 돌려드립니다. 이 폴더로 직접 빌드하기 전에 JDK {m.group('n')}과 사용하는 Maven "
                "버전에 맞는지 확인해 주세요.")),
    # 프로젝트의 .mvn/maven.config가 자기 settings를 가리킬 때의 안내다. 서버 설정으로 빌드했는지, 섞였는지를 알린다
    ((r"^the project's \.mvn/maven\.config points Maven at its own settings \((?P<what>.+?)\): (?:ignored here — "
      r"every build of this run used the server's settings|Maven read them in this run as its global settings, merged "
      r"with the server's \((?P<why>build\.ignore_project_settings is false|Maven home not found, so no -gs was "
      r"passed)\))(?:\. The same file also turns on offline mode \((?P<off>[^)]+)\), which the command line cannot "
      r"switch off: Maven downloads nothing in this run and builds only from what the local repository already holds)?$"),
     lambda m: (f"프로젝트의 `.mvn/maven.config`가 자체 Maven 설정을 가리키고 있습니다({m.group('what')}). "
                + ("이번 실행에서는 이 설정을 쓰지 않고 서버의 설정으로만 빌드했습니다."
                   if not m.group("why") else
                   "이번 실행에서 Maven이 이 설정을 서버의 설정과 함께 읽었습니다("
                   + ("build.ignore_project_settings가 false입니다" if (m.group("why") or "").startswith("build.")
                      else "Maven 설치 위치를 찾지 못해 서버의 전역 설정을 지정하지 못했습니다") + ").")
                + (f" 같은 파일에 오프라인 모드({m.group('off')})가 켜져 있어 Maven이 아무것도 내려받지 않고 로컬 "
                   "저장소에 있는 것만으로 빌드했습니다." if m.group("off") else ""))),
    ((r"^bundled jar `(?P<j>[^`]+)` provides package `(?P<p>[^`]+)` needed by `(?P<pom>[^`]+)`, but the repository did "
      r"not answer whether it has `(?P<c>[^`]+)` \((?P<why>.+)\) — not wired and not called missing; not a project problem: "
      r"check the repository and the network, then rerun$"),
     lambda m: (f"프로젝트에 들어 있는 jar `{m.group('j')}`가 `{m.group('pom')}`에 필요한 패키지 `{m.group('p')}`를 "
                f"제공하지만, 저장소가 응답하지 않아 `{m.group('c')}`가 저장소에 있는지 확인하지 못했습니다"
                f"({m.group('why')}). 이 jar는 연결하지 않았고, 없는 것으로 처리하지도 않았습니다. 프로젝트 문제가 "
                "아니니 저장소와 네트워크를 확인한 뒤 다시 실행해 주세요.")),
    ((r"^`(?P<c>[^`]+)` could not be downloaded: the repository did not answer(?: \((?P<r>[^)]+)\))? \(timeout, refused "
      r"connection or server error\) — not a project problem: check the repository and the network, then rerun$"),
     lambda m: (f"`{m.group('c')}` 아티팩트를 내려받지 못했습니다. 저장소가 응답하지 않았습니다"
                + (f"(확인한 저장소: {m.group('r')})" if m.group("r") else "")
                + ". 시간 초과, 연결 거부 또는 서버 오류로 보이며 프로젝트 문제가 아닙니다. 저장소와 네트워크를 확인한 뒤 "
                "다시 실행해 주세요.")),
    ((r"^`(?P<c>[^`]+)` is not in any repository: proxy it in Nexus or fix the coordinates \(checked repositories: "
      r"(?P<r>.+)\); the LLM is never asked to invent versions$"),
     lambda m: (f"`{m.group('c')}` 아티팩트를 어느 저장소에서도 찾지 못했습니다{_repos_ko(m.group('r'))}. "
                "Nexus에서 프록시하도록 설정하거나 좌표를 고쳐 주세요. 버전을 추측하지 않도록 LLM에는 맡기지 않았습니다.")),
    ((r"^parent `(?P<c>[^`]+)` is not resolvable(?: \(checked: (?P<r>[^)]+)\))? with settings `(?P<s>[^`]+)`: check "
      r"build\.settings_file / the repository mirror and artifacts\.parent; nothing else can build until it resolves$"),
     lambda m: (f"부모 pom(`{m.group('c')}`)을 저장소에서 받지 못했습니다(settings `{m.group('s')}`"
                + (f", 확인한 저장소: {m.group('r')}" if m.group("r") else "")
                + "). build.settings_file, 저장소 미러, artifacts.parent를 확인해 주세요. 부모 pom을 받을 수 있을 때까지 "
                "아무것도 빌드할 수 없습니다.")),
    ((r"^build extension `(?P<c>[^`]+)` declared by the company parent `(?P<p>[^`]+)` cannot be downloaded: make the "
      r"repository serve its pom, jar, parent poms and dependencies, or drop the extension from the parent \(a deploy "
      r"transport is not needed for packaging\); nothing in the project can change an inherited extension$"),
     (r"사내 부모 pom(`\g<p>`)이 선언한 빌드 확장(`\g<c>`)을 저장소에서 받지 못했습니다. 저장소가 이 확장의 pom, jar, 부모 "
      r"pom, 의존성을 제공하도록 하거나 부모 pom에서 확장을 빼 주세요(패키징에는 배포용 전송 확장이 필요 없습니다). 상속된 "
      r"확장은 프로젝트 안에서 바꿀 수 없습니다.")),
    ((r"^build plugin `(?P<p>[^`]+)` fails on JDK 21: upgrade it \(raise its version in the pom\) or remove/replace it; "
      r"left unchanged on purpose$"),
     r"`\g<p>` 플러그인이 JDK 21에서 실패합니다. pom에서 버전을 올리거나, 플러그인을 빼거나 다른 것으로 바꿔 주세요. "
     + LEFT_ALONE_KO),
    ((r"^non-Java / generated sources \(Kotlin, Groovy, Scala, protobuf/jaxb/openapi generators\) are not migrated by "
      r"sbmig: migrate this module or generator configuration by hand$"),
     (r"Java가 아닌 소스나 생성 코드(Kotlin, Groovy, Scala, protobuf/jaxb/openapi 생성기)는 sbmig가 전환하지 않습니다. 이 "
      r"모듈이나 생성기 설정은 직접 전환해 주세요.")),
    (r"^the source encoding could not be determined: set project\.build\.sourceEncoding or convert to UTF-8$",
     r"소스 인코딩을 알아내지 못했습니다. project.build.sourceEncoding을 지정하거나 소스를 UTF-8로 바꿔 주세요."),
    ((r"^the run's LLM/time budget was used up before this cluster could be fixed: rerun with a higher "
      r"loop\.max_llm_calls / loop\.max_total_minutes or fix it by hand$"),
     (r"이 항목을 고치기 전에 이번 실행의 LLM 호출 / 시간 한도를 모두 썼습니다. loop.max_llm_calls / loop.max_total_minutes를 "
      r"늘려 다시 실행하거나 직접 고쳐 주세요.")),
    ((r"^the LLM was unavailable for this run \(proven stored fix scripts were still reused\): fix the LLM endpoint / key "
      r"and rerun, or fix it by hand(?: — last error: (?P<e>.+))?$"),
     lambda m: ("이번 실행에서는 LLM을 쓸 수 없었습니다(검증된 저장 스크립트는 그대로 썼습니다). LLM 주소 / 키를 고친 뒤 다시 "
                "실행하거나 직접 고쳐 주세요." + (f" 마지막 오류: {m.group('e')}" if m.group("e") else ""))),
    ((r"^(?P<n>\d+) fix attempt\(s\) did not resolve it; needs a human \(see the fix attempts and the journal for what "
      r"was tried\)$"),
     r"\g<n>번 고쳐 봤지만 해결하지 못했습니다. 직접 확인해 주세요(고친 내용은 errors.md와 journal.jsonl에 있습니다)."),
    (r"^fixes kept toggling between the same error sets; needs a human \(see the fix attempts\)$",
     r"같은 오류 사이를 오가며 수정이 되풀이되었습니다. 직접 확인해 주세요(errors.md 참고)."),
    (r"^needs a human$", r"직접 확인해 주세요."),
    (r"^a person pinned case (?P<c>\S+) to `block`: handle it by hand \(see `(?P<cmd>sbmig kb cases show \S+)`\)$",
     r"담당자가 이 오류 유형(\g<c>)을 `block`으로 지정했습니다. 직접 처리해 주세요(`\g<cmd>` 참고)."),
    # 수정 루프와 오케스트레이터가 남기는 확인 사항(sbmig/loop/fixloop.py, orchestrator.py)
    (r"^(?P<why>.+) — no LLM fix script could be written in this run; the cases that needed one are blocked "
     r"\(llm_unavailable\)$",
     r"\g<why>. 이번 실행에서는 LLM 수정 스크립트를 만들 수 없었습니다. 스크립트가 필요한 오류는 「진행하지 않은 항목」"
     r"(llm_unavailable)에 넣었습니다."),
    (r"^the LLM was unavailable during this run \((?P<w>.+)\); cases that needed it are listed as blocked "
     r"\(llm_unavailable\) — proven stored fix scripts were still reused$",
     r"이번 실행 중 LLM을 쓸 수 없었습니다(\g<w>). LLM이 필요한 오류는 「진행하지 않은 항목」(llm_unavailable)에 "
     r"넣었습니다. 검증된 저장 스크립트는 그대로 썼습니다."),
    (r"^the LLM gateway rejected (?P<n>\d+) different prompts with the same HTTP 400 \((?P<b>.*)\) — a configuration "
     r"error \(model name / payload fields in \.env or llm\.\*\), not the prompts; LLM calls stopped for this run, fix "
     r"the setting and rerun$",
     r"LLM 게이트웨이가 서로 다른 요청 \g<n>건을 같은 HTTP 400 오류로 거부했습니다(\g<b>). 요청 내용이 아니라 설정 "
     r"문제입니다(.env나 llm.*의 모델 이름 / 요청 필드). 이번 실행의 LLM 호출은 멈췄습니다. 설정을 고친 뒤 다시 실행해 주세요."),
    (r"^test sources do not compile \(build passes only with -Dmaven\.test\.skip=true\): (?P<rest>.+)$",
     r"테스트 소스가 컴파일되지 않습니다(-Dmaven.test.skip=true로만 빌드됩니다): \g<rest>"),
    (r"^test fails at runtime \(packaged with -DskipTests\): (?P<who>.+?) — (?P<m>.+) — likely an externalized setting "
     r"\(see Handover 'Properties to set'\) or infrastructure not reachable at build time$",
     r"테스트가 실행 중 실패합니다(-DskipTests로 패키징했습니다): \g<who> — \g<m>. 외부 설정 값(「실행 전에 설정할 "
     r"프로퍼티」 참고)이 없거나 빌드 중에 접속할 수 없는 인프라 때문일 가능성이 큽니다."),
    (r"^sibling poms without a root pom: the aggregator pom could not be written \((?P<e>.+)\) — add a root pom\.xml "
     r"listing (?P<m>.+)$",
     r"루트 pom 없이 형제 pom만 있는데 집계 pom을 만들지 못했습니다(\g<e>). \g<m> 모듈을 나열한 루트 pom.xml을 추가해 주세요."),
    (r"^no aggregator pom: sbmig generated (?P<p>\S+) \((?P<a>[^,]+), modules (?P<m>.+?)\) — (?P<why>.+?); the as-is "
     r"build of each module stays as it was(?:; not linked to them and left out: (?P<ex>.+))?$",
     _aggregator_ko),
    (r"^'(?P<mvn>[^']*)' is not runnable(?: \((?P<d>.*)\))?: install Maven or set build\.mvn; every build below will fail$",
     lambda m: (f"Maven({m.group('mvn')})을 실행할 수 없습니다" + (f"({m.group('d')})" if m.group("d") else "")
                + ". Maven을 설치하거나 build.mvn을 설정해 주세요. 이대로는 이후 모든 빌드가 실패합니다.")),
    (r"^artifacts\.parent is not configured: pass the company artifacts yml with --config$",
     r"artifacts.parent가 설정되어 있지 않습니다. 회사 artifacts yml을 --config로 지정해 주세요."),
    (r"^optional artifact bump step (?P<s>\S+) failed: (?P<w>.+) — bump the in-house optional artifacts by hand$",
     r"사내 선택 아티팩트 버전 맞춤 단계(\g<s>)가 실패했습니다: \g<w>. 사내 선택 아티팩트의 버전을 직접 올려 주세요."),
    # 수정 루프 없이 빌드가 실패한 프레임워크 프로젝트의 인수인계 첫 줄(sbmig.analyze.framework.no_fix_loop_followup)
    (r"^framework project: build errors are not fixed automatically \(framework\.fix_loop: false\) — (?P<n>\d+) "
     r"error\(s\) remain after the JDK 21 \+ jakarta conversion; see the error list$",
     r"사내 프레임워크 프로젝트라 빌드 오류를 자동으로 고치지 않았습니다(framework.fix_loop: false). JDK 21과 Jakarta 변환 "
     r"뒤 빌드에 남은 오류 \g<n>건을 아래 「변환 뒤 남은 빌드 오류」에 정리했습니다. 프로젝트 담당자와 프레임워크 담당자가 "
     r"고쳐 주셔야 합니다."),
    (r"^framework project: build errors are not fixed automatically \(framework\.fix_loop: false\) — the build failed "
     r"after the JDK 21 \+ jakarta conversion; see the build log$",
     r"사내 프레임워크 프로젝트라 빌드 오류를 자동으로 고치지 않았습니다(framework.fix_loop: false). JDK 21과 Jakarta 변환 "
     r"뒤 빌드가 실패했습니다. 「참고」의 마지막 빌드 로그를 봐 주세요."),
    (r"^framework check could not run \(the parent pom and the dependency tree could not be read\): if this project uses "
     r"the in-house framework it must not be migrated in full — confirm before using the result$",
     r"사내 프레임워크를 쓰는 프로젝트인지 확인하지 못했습니다(부모 pom과 의존성 트리를 읽지 못했습니다). 사내 프레임워크를 "
     r"쓰는 프로젝트는 전체 마이그레이션 대상이 아니므로, 결과를 쓰기 전에 운영 담당자에게 확인해 주세요."),
    (r"^restructure step (?P<s>\S+) \((?P<m>[^)]+)\) failed: (?P<w>.+) — the tree keeps the as-is layout / parents; the "
     r"final build ran on it, so BUILD SUCCESS here does not mean the migration is complete$",
     r"구조 재구성 단계(\g<s>, \g<m>)가 실패했습니다: \g<w>. 프로젝트는 기존 구조와 부모 pom을 그대로 둔 채 최종 빌드를 "
     r"실행했습니다. 빌드가 성공했더라도 마이그레이션이 끝난 것은 아닙니다."),
    (r"^binary file without an extension under a filtered resource dir: (?P<f>.+) — exclude it from filtering \(a second "
     r"<resource> with <filtering>false</filtering>\) or rename it$",
     r"필터링하는 리소스 폴더에 확장자 없는 바이너리 파일이 있습니다: \g<f>. 필터링에서 제외하거나(<filtering>false"
     r"</filtering>인 <resource>를 하나 더 추가) 파일 이름을 바꿔 주세요."),
    (r"^… and (?P<n>\d+) more binary file\(s\) without an extension under filtered resource dirs \(see the structure "
     r"review facts\)$",
     r"… 확장자 없는 바이너리 파일이 \g<n>개 더 있습니다(구조 점검 자료 참고)."),
    (r"^(?P<n>\d+) javax API jar\(s\) still on the classpath — see that section$",
     r"클래스패스에 javax API jar \g<n>개가 남아 있습니다. 「클래스패스에 남은 javax API jar」를 봐 주세요."),
    ((r"^(?P<n>\d+) javax API jar\(s\) still on the classpath after the migration \(see the report section\) — "
      r"the libraries that drag them in need Jakarta builds: (?P<libs>.+)$"),
     (r"전환 뒤에도 javax API jar \g<n>개가 클래스패스에 남아 있습니다(「클래스패스에 남은 javax API jar」 참고). 이 jar를 "
      r"가져오는 라이브러리에 Jakarta 빌드가 필요합니다: \g<libs>")),
)]}

# tr이 채우는 값 가운데 저장된 영어 줄이어서 tr_line으로 한 번 더 번역할 자리
# Fields of a `tr` template whose value is itself a stored English line (a config value / a review note).
_LINE_FIELDS: dict[str, tuple[str, ...]] = {
    "Prices: {note}.": ("note",),
    "skipped ({note})": ("note",),
    "failed — no usable answer, the build went ahead ({note})": ("note",),
}


def normalize(lang: object) -> str:
    """언어 코드를 지원하는 값으로 정리한다. None이나 오타 같은 다른 값이면 "en"을 반환한다.

    A supported language code; anything else (None, typo) → "en"."""
    s = str(lang or "en").strip().lower()
    return s if s in LANGUAGES else "en"


def tr(lang: str, text: str, **fmt: object) -> str:
    """영어 원문 ``text``를 ``lang`` 언어로 번역하고, ``fmt``가 있으면 ``str.format``으로 채운다.
    번역이 없으면 영어를 그대로 쓰므로 ``en`` 출력은 바뀌지 않는다.

    Translation of `text` for `lang` (English fallback), formatted with `fmt` when given."""
    s = text
    if lang != "en":
        s = STRINGS.get(lang, {}).get(text) or text
        for name in _LINE_FIELDS.get(text, ()):
            if name in fmt:
                fmt[name] = tr_line(lang, str(fmt[name]))
    return s.format(**fmt) if fmt else s


def tr_line(lang: str, line: str) -> str:
    """저장된 영어 줄 하나를 ``PATTERNS``의 첫 번째로 맞는 패턴으로 번역한다.
    ``en``이거나 맞는 패턴이 없으면 그대로 반환한다.

    A stored English line (see `PATTERNS`) in `lang`; unchanged for `en` and for lines no pattern knows. A stored abort
    follow-up `run aborted: <reason>` gets a translated head and its reason translated on its own; the bare reason (the
    journal `abort` event) translates directly."""
    if lang == "en":
        return line
    # 후속 조치로 저장된 중단 사유("run aborted: <사유>")는 앞부분과 사유를 따로 번역한다
    if line.startswith(ABORT_PREFIX) and lang in ABORT_LABEL:
        return ABORT_LABEL[lang] + tr_line(lang, line[len(ABORT_PREFIX):])
    for rx, tmpl in PATTERNS.get(lang, []):
        m = rx.match(line)
        if m:
            # 값에 따라 문장 모양이 달라지는 규칙(선택 그룹, 경로 구분자 변환)은 함수로 둔다
            return m.expand(tmpl) if isinstance(tmpl, str) else tmpl(m)
    # v2.1z-e: a blocked item's suggestion may carry the search record / the unmatched owner class / the EOL advisory as
    # a tail ("…; repository search for a jakarta build of g:a: …") — translate head and tail on their own
    for sep in _TAIL_SEPARATORS:
        if sep in line:
            head, tail = line.split(sep, 1)
            tail = sep[2:] + tail
            head_ko = tr_line(lang, head)
            # 번역된 앞 문장은 마침표로 끝나므로 공백으로 잇고, 영어 그대로면 원래 구분자를 쓴다
            joint = " " if head_ko != head and head_ko.rstrip().endswith(".") else "; "
            return f"{head_ko}{joint}{tr_line(lang, tail)}"
    return line


ABORT_PREFIX = "run aborted: "                 # Orchestrator._abort가 후속 조치 앞에 붙이는 문구
ABORT_LABEL = {"ko": "실행 중단: "}

_TAIL_SEPARATORS = ("; repository search for a jakarta build of ", "; the library behind ", "; EOL: ")
