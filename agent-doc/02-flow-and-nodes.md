# 실행 흐름과 노드

> 다루는 것: `sbmig run` 한 번이 지나가는 단계(접수 → 분석 → 계획 → 변환 → 재구성 → 구조 검토 → 검증 빌드 → 수정 루프 → 마무리)마다 코드 위치, 읽는 것, 정하는 것, 쓰는 것, 설정 키, 멈추는 방식. LangGraph 엔진과 웹 UI 이벤트.
> 언제 읽나: 실행이 어디서 무엇을 했는지 따라가야 할 때, 단계 하나를 고치기 전에.
> 함께 읽을 파일: [00-what-sbmig-is.md](00-what-sbmig-is.md), [03-run-debug-and-logs.md](03-run-debug-and-logs.md), [04-cases-scripts-kb.md](04-cases-scripts-kb.md), [06-company-rules-restructure.md](06-company-rules-restructure.md)

## 0. 큰 그림

실행 한 번 = `Orchestrator`(`sbmig/loop/orchestrator.py`) 하나. 진입은 `sbmig run <폴더|zip> --config <회사 yml>`(`sbmig/cli.py`)
또는 `sbmig.api.migrate(...)`(`sbmig/api.py`). 접수 전에 `Workspace.from_input` 이 입력을 `out/<run id>/project/` 로 복사하고,
그 폴더를 git 저장소로 만든다(브랜치 `sbmig/<run id>`, 첫 커밋 `sbmig: as-is snapshot`). 그 뒤 모든 변경은 단계마다 커밋이다 —
`changes.patch` 는 as-is 스냅숏과 마지막 커밋의 차이다.

```
step_intake --> step_analyze --> step_plan --> step_phase(transform)
                                            --> step_phase(restructure: 재구성 + 구조 검토)
                                            --> step_phase(verify: 검증 빌드 + 수정 루프 + 최종 빌드)
                                            --> step_finish --> _finalize (보고서, KB)
```

`_run_guard` 가 전체를 감싼다: 어떤 예외·중단이 나도 `_finalize` 가 돌아 보고서가 남는다(불변조건 1). 중단 사유는
`_abort` 가 플래그 `aborted`, 후속 조치 `run aborted: <이유>`, ERROR 로그 한 줄 `run aborted: …`, 저널 `abort` 로 남긴다.

실행 디렉토리에 남는 것: `project/`, `logs/`(Maven·OpenRewrite 로그 `NNN-<label>.log`, `logs/effective-depmgmt.json` 등),
`journal.jsonl`(이벤트 한 줄씩), `sbmig.log`(DEBUG, 줄마다 `[단계]` 태그), 오류 목록(`out/<run id>/errors.jsonl` + 사람용 md),
`out/<run id>/profile.json`, `out/<run id>/plan.json`, `out/<run id>/report.md`, `out/<run id>/report.json`, `changes.patch`, `result.zip`.

## 1. 접수 (intake) — `step_intake`

| 항목 | 내용 |
|---|---|
| 코드 | `Orchestrator.step_intake` → `_step_intake`: `_preflight`, `_check_build_tool`, `_intake_layout`, `_capture_effective` |
| 읽는 것 | 입력 트리, `artifacts.parent` / `artifacts.boot_parent` / `artifacts.comp_parent`, `build.mvn`, `build.java_home` |
| 하는 일 | (0) `build.setup_command` 가 있으면 먼저 실행(실패 = 중단). (1) 사전 점검: Maven 이 도는지, `mvn -v` 의 Java 가 `build.require_java_major`(21)인지, 사내 부모 좌표가 채워졌는지 → 저널 `preflight`. (2) 빌드 도구: `sbmig/analyze/build_tool.py` 의 판별. Maven 이면 통과. Ant(build.xml 만)면 `intake.ant_to_maven` 일 때 `sbmig/analyze/ant.py` 의 `synthesize_pom` 이 pom.xml 을 쓰고 커밋 `intake: pom.xml synthesized from build.xml`, 플래그 `pom-synthesized`(좌표 모르는 jar 가 있으면 `jar-coordinates-unknown`). Gradle·빌드 파일 없음 → 중단. (3) 레이아웃: 떠도는 pom 복사본 무시(`find_stray_pom_copies`, 폴더를 `Workspace.ignored_dirs` 에 넣음, 플래그 `stray-pom-ignored`, 커밋 없음) → 모듈이 트리에 없는 EAR 루트면 중단(`ear-without-modules`) → 루트 pom 없는 형제 pom 이면 루트 aggregator 생성(`layout.generate_aggregator`, 커밋 `intake: generated root aggregator pom (…)`, 플래그 `aggregator-generated`) → reactor 에 두 번 닿는 모듈의 중복 `<module>` 제거(커밋, 플래그 `reactor-duplicate-removed`) → 배포 전송 빌드 확장(`<build><extensions>` 의 wagon-*, 목록 `knowledge/libraries/build_extensions.yml`) 제거(`sbmig/analyze/build_extensions.py`, 커밋 `intake: deploy transport extension(s) removed (…)`, 플래그 `deploy-extensions-removed`, 인수인계 한 줄씩 — 저장소에 없는 확장 하나가 OpenRewrite 까지 막기 때문, `intake.strip_deploy_extensions`) → 사내 부모 체인이 선언한 빌드 확장 점검(`sbmig/build/parent_extensions.py`: 저장소가 주는데 로컬 `~/.m2` 에 묵은 `*.lastUpdated` 표식만 있으면 그 폴더의 표식 제거, 어디서도 못 읽으면 WARNING + 플래그 `parent-extension-unresolvable` + 인수인계, 저널 `parent_build_extensions`). (4) as-is effective dependencyManagement 캡처(`sbmig/build/effective.py` 의 `capture_effective_depmgmt` → `logs/effective-depmgmt.json`) — 나중에 버전 이어받기와 인수인계의 "바뀐 버전" 에 쓴다 |
| 저널 | `build_hook`, `preflight`, `build_tool`, `pom_synthesized`, `stray_pom_ignored`, `ear_without_modules`, `aggregator_generated`, `reactor_dedupe`, `effective_depmgmt` |
| 멈춤 | Maven 실행 불가(`mvn-not-found`), JDK 가 21 아님(`jdk-not-21`), `unsupported-build-tool:<도구>`, `ear-without-modules` → 모두 exit 3 |

pom 이 없고 build.xml 도 없는 트리(풀어 놓은 Tomcat 웹앱)는 거부된다. 담당팀에 보낼 설명서는 읽기 전용 `sbmig inventory <dir>` 가 만든다.

## 2. 분석 (analyze) — `step_analyze`

| 항목 | 내용 |
|---|---|
| 코드 | `Orchestrator.step_analyze` → `analyze_project`(`sbmig/analyze/analyzer.py`) → `_check_adf`, `_check_hollow`, `_note_bundled_jars`, `_evaluate_advisories`, `_root_pom_present` |
| 읽는 것 | 모든 pom(`sbmig/analyze/pom_parser.py`, Maven 호출 없음), 소스(`sbmig/analyze/source_scanner.py`), web.xml, JSP, 트리 안 jar |
| 정하는 것 | `ProjectProfile`: 유형(`case_type`, `sbmig/analyze/classifier.py`), Java·Spring·Boot 버전, 라이브러리(`sbmig/analyze/libraries.py`), MSA 서비스 루트, 경고, 외부 설정 키, 동봉 jar 목록(`sbmig/analyze/bundled_jars.py`, `intake.bundled_jars`), ADF 요약(`sbmig/analyze/adf.py`) |
| 쓰는 것 | `out/<run id>/profile.json`, 동봉 jar 가 있으면 `logs/bundled-jars.json` + 플래그 `bundled-jars` |
| 권고 | `sbmig/advisory/evaluate.py` 가 EOL 라이브러리 권고를 찾는다. 안내만(차단 없음): 계획 노트, 수정 프롬프트 힌트, 인수인계 |
| 저널 | `analyze`, `adf_project` / `adf_traces`, `hollow_reactor` / `sources_outside_reactor`, `bundled_jars`, `advisories`, `no_root_pom` |
| 멈춤 | ADF 근거 계열이 둘 이상이고 `intake.adf_policy: stop`(기본) → 플래그 `adf-project`, 인수인계 첫 줄 "ADF 프로젝트라 변환이 불가능합니다 (…)", exit 3(Maven·LLM 호출 없음). 루트 pom.xml 이 없고 MSA 도 아님 → `no-root-pom`, exit 3 |
| 계속하지만 성공 아님 | 속 빈 리액터 → 플래그 `hollow-reactor`, 인수인계 첫 줄, exit 2. 계열 하나뿐인 ADF 흔적 → `adf-traces` 메모만 |

## 3. 계획 (plan) — `step_plan`

| 항목 | 내용 |
|---|---|
| 코드 | `build_plan`(`sbmig/plan/planner.py`), 규칙 기반. LLM 없음(`plan.advisor: llm` 일 때만 싼 모델이 레시피를 더할 수 있음, 기본 `none`) |
| 쓰는 것 | `out/<run id>/plan.json`, 저널 `plan`, `followup:` 으로 시작하는 노트는 인수인계로 |
| 그다음 | 실행의 오류 목록을 열고 `BuildFixLoop` 를 만든다. 같은 phase 가 이어지는 단계를 묶어 phase 묶음 목록을 돌려준다 |

단계 id(MSA 가 아닐 때):

| 단계 id | phase | 언제 |
|---|---|---|
| `transform.rewrite` | transform | 자바 소스가 있으면 |
| `transform.compile` | transform | `plan.compile_after_rewrite: true` 일 때만(기본 false — 재구성 전에는 빌드하지 않는다) |
| `restructure.apply` | restructure | 비 Boot 유형, 또는 `layout.boot_as_is: layout` (모드 `layout`) |
| `restructure.boot_pom` | restructure | `spring_boot` + `layout.boot_as_is: in-place`(기본) (모드 `boot_pom`) |
| `restructure.pom_only` | restructure | 자바 소스가 없는 pom 전용 프로젝트 (모드 `msa_pom`) |
| `restructure.review` | restructure | `review.enabled` 일 때 |
| `verify.build` | verify | 항상(계획에 없어도 `loop.always_final_verify` 가 덧붙임) |

MSA: 공유 라이브러리 루트 먼저, 그다음 서비스마다 `transform.rewrite@<x>` → `restructure.msa_pom` → `restructure.review` →
`verify.build`(루트 aggregator 가 없으면 `verify.build@<x>` 서비스마다).

레시피 세트(`openrewrite.recipes`, id 는 `knowledge/recipes/catalog.yml` 에 있어야 한다):

| 세트 | 누구에게 |
|---|---|
| `jdk21` (`UpgradeToJava21`) | 모두 |
| `jakarta` (`JavaxMigrationToJakarta`) | `spring_legacy`, `servlet_tomcat` 항상; `pure_java` 는 Java EE javax import 가 있을 때; `spring_boot` 는 아님(Boot 우산이 함) |
| `servlet_jakarta_10` | `servlet_tomcat` 전부, `javax.servlet` 을 쓰는 `pure_java`, Framework 세트가 빈 `spring_legacy` |
| `spring_framework_7` | `spring_legacy` 전부 |
| `spring_security_7` (6_0 … 6_5, 7_0 체인) | `spring-security` 를 쓰는 `spring_legacy` |
| `spring_batch_6`, `hibernate_7` | 그 라이브러리를 쓰는 `spring_legacy` |
| `spring_boot_4` (`UpgradeSpringBoot_4_0` + `SpringBootProperties_4_1`) | `spring_boot` |
| `junit5` | junit4 를 쓸 때 — 기본은 빈 목록(꺼짐) |

## 4. 변환 (transform) — `_step_rewrite`

| 항목 | 내용 |
|---|---|
| 코드 | `Orchestrator._step_rewrite` → `OpenRewriteRunner`(`sbmig/rewrite/runner.py`), 합성 레시피 `sbmig/rewrite/compose.py` |
| 하는 일 | 계획한 레시피 전부를 rewrite-maven-plugin **한 번**에 돌린다(as-is classpath 위에서, 빌드 없음). JSP 는 파싱하지 않는다(`openrewrite.exclusions`) |
| 쓰는 것 | 커밋 `rewrite: transform.rewrite`, 로그 `logs/NNN-…log`, 합성 레시피 `logs/rewrite-<n>.yml`, 저널 `rewrite` |
| 설정 | `openrewrite.plugin_version`, `openrewrite.recipe_bom_version`(3.37.0), `openrewrite.timeout_minutes`, `openrewrite.jvm_args`, `openrewrite.download_retry` |
| 실패 | 멈추지 않는다 — "빌드가 진짜 문제를 보여 준다". 레시피가 `4.0.x` 같은 글자 그대로의 버전을 남기면 되돌리고 한 번 재시도(플래그 `rewrite-unresolved-versions`). 레시피 id 를 못 찾으면 설정 오류로 그 묶음을 중단 |

## 5. 재구성 (restructure) — `_step_restructure`

| 항목 | 내용 |
|---|---|
| 코드 | `Orchestrator._step_restructure` → `Restructurer.apply(ws, profile, cfg, mode=…, params=…)`(`sbmig/restructure/restructurer.py`) |
| 읽는 것 | 프로필, `artifacts.*`, `layout.*`, as-is 캡처(`params["asis_effective"]`), 재구성 뒤 effective-pom(`params["effective_pom"]`, 필요할 때 `logs/effective-depmgmt-after-restructure.json` 으로 캡처), 사내 부모 체인 사실(`sbmig/build/parent_chain.py`) |
| 하는 일(순서) | 모드별 작업(디렉토리 이동, 루트 → aggregator, 부모 교체, 필수·선택 아티팩트, Application·boot pom 생성, web.xml 등록, legacy 컨텍스트) → 모든 pom 정리: 필터링되는 바이너리의 `nonFilteredFileExtensions` → as-is 버전 이어 적기 → 부모 체인이 관리하는 버전 지우기 → 부모를 덮는 버전 속성 지우기 → 참조 없는 버전 모양 속성 지우기 → 동봉 jar 연결 → (모든 모드 공통) JSP javax → jakarta |
| 쓰는 것 | 커밋 `restructure: <단계 id>` + 중간 커밋 `restructure: …`(JSP 는 `restructure: JSP javax -> jakarta`), 저널 `restructure`, `restructure_moves`, `parent_chain_facts`, `versions_pinned`, `bom_property_overrides`, `non_filtered_extensions`, `bundled_jar_wired` / `bundled_jars_skipped`, `jsp_jakarta`, `placeholders` |
| 설정 | `layout.*`(모듈 이름, `layout.jsp_boot_packaging`, `layout.drop_managed_versions`, `layout.drop_bom_property_overrides`, `layout.wire_bundled_jars`, `layout.jsp_jakarta_rename`, `layout.legacy_context`), `artifacts.*`, `build.non_filtered_extensions` |
| 플래그 | `parent-chain-no-boot-bom`(`artifacts.parent_manages_spring_boot: true` 인데 사내 체인이 Boot BOM 을 import 하지 않음, 인수인계 맨 앞 줄), `web-xml-registered` 등 |
| 실패 | `RestructureResult.ok=False` → 플래그 `restructure-failed`, 나머지 재구성 단계는 건너뛰지만 검증 빌드는 돈다. 최종 빌드가 통과해도 성공 아님(exit 2) |

규칙 하나하나는 [06-company-rules-restructure.md](06-company-rules-restructure.md).

## 6. 구조 검토 (structure review) — `_step_review`

| 항목 | 내용 |
|---|---|
| 코드 | `Orchestrator._step_review` → `StructureReviewer.review`(`sbmig/review/reviewer.py`), 입력 `sbmig/review/inputs.py`, 프롬프트 `sbmig/review/prompts/review_v1.md`, 답변 `sbmig/review/parse.py` |
| 읽는 것 | 재구성된 pom 전부(`review.max_pom_chars`, `review.max_poms`), 재구성 노트, 부모 체인 사실, 인코딩·필터링 리소스 사실 |
| 정하는 것 | 판정 `ok` / `issues` / `skipped` / `failed`, 이슈마다 `critical` 또는 `warning`. critical 예: 틀린 부모, 부모 체인을 덮는 속성, 다른 줄(line)의 Spring 계열 버전 고정, 인코딩 누락 |
| 하는 일 | critical 이 있고 `review.fix_critical` 이면 LLM 답의 `<edits>` 를 적용(기본은 편집만, `review.ask_script: false`). `review.invariant_check` 가 재구성 불변식을 다시 검사해 어긴 편집은 복원하고 한 번 재질의 |
| 쓰는 것 | 커밋 `review: fix critical structure issues (<n>)`(수정이 성공했을 때만; 실패면 바뀐 것을 버림), 저널 `structure_review`, 입력 사본 `logs/review-input.md`(`review.save_input`), warning 은 보고서 "Structure review" 절과 오류 목록 메모 |
| 실패 | 실행을 멈추지 않는다. LLM 이 없거나 예산이 없으면 `failed` / `skipped` 로 적고 계속. LLM 1회(형식 오류면 1회 재시도) |

`SBMIG-RESULT` 줄 끝의 `review=<판정>` 이 이 결과다.

## 7. 검증 빌드 (verify) — `_step_verify`

| 항목 | 내용 |
|---|---|
| 코드 | `Orchestrator._step_verify` → `build_runner.run("clean package", …)`(`sbmig/build/runner.py`) → 실패면 `_verify_fix_loop` → 최종 빌드 한 번 더 |
| 명령 | `mvn clean package -Dmaven.test.skip=true`(`build.skip_test_compile: true`), `build.extra_args`, `build.settings_file`, `build.java_home` |
| 쓰는 것 | 빌드 로그 `logs/NNN-verify.log`, 오류 목록에 오류 추가, 저널 `build`, `verify` |
| 특이 | Windows 의 `target/` 파일 잠금이면 기다렸다 다시(`build.file_lock_retries`), 로컬 저장소 부정 캐시면 `-U` 로 한 번 더(`build.retry_negative_cache`) |
| 수정 루프로 | 남은 클러스터가 모두 이미 차단된 것이면 루프를 돌리지 않는다. 시간 예산(`loop.max_total_minutes`)이 끝났으면 돌리지 않는다 |

## 8. 수정 루프 (개요만 — 자세히는 [04-cases-scripts-kb.md](04-cases-scripts-kb.md))

`BuildFixLoop.run`(`sbmig/loop/fixloop.py`), 설정 `loop.phases.verify`(goal `package`, `max_iterations` 50). 실패한 첫 검증 빌드가
루프의 빌드 1 이다. 라운드 하나:

```
빌드 -> 직전 시도 채점(악화면 되돌림, KB 실적 기록) -> 새 오류를 오류 목록에
     -> BUILD SUCCESS 면 끝
     -> 케이스(L2 서명)로 묶기, 근본 원인 먼저, 이번 라운드에 손댄 파일·모듈 pom 의 케이스는 미룸
     -> 케이스마다 사다리:
          pre-try (sbmig-version / sbmig-resources / sbmig-jakarta / sbmig-bundled, LLM 0회)
          -> 재사용 (KB 저장 스크립트, LLM 0회)
          -> 보강 (LLM 이 이번 실행의 마지막 스크립트를 고침, 최대 script.max_augments_per_case)
          -> 신규 (LLM 새로)
          -> 차단 (script.max_llm_tries_per_case 소진 = repeated_failure, 거절 = needs_owner)
     -> 시도마다 커밋 fix(script:<case>:v<N> <mode>) 또는 fix(edits:<case> <source>)
```

끝나는 조건은 언제나 빌드 뒤: 성공, 라운드 소진, 손댈 케이스 없음, 차단만 남음, 시간 초과. 저널 `loop_start`, `triage`,
`decision`, `attempt`, `attempt_scored`, `script_rollback`, `script_stored`, `script_credit`, `version_try`, `resources_try`,
`jakarta_try`, `blocked`, `loop_end`, 그리고 오케스트레이터 쪽 `verify_loop_start` / `verify_loop_end`. 루프가 돌았으면
`_step_verify` 가 최종 `mvn clean package` 를 **반드시** 다시 한다(불변조건 1).

## 9. 마무리 (finalize) — `_finalize`

| 항목 | 내용 |
|---|---|
| 하는 일 | LLM 사용량·비용(`sbmig/report/cost.py`, `llm.pricing`), 루프의 플래그·후속 조치 합치기, 성공 판정, 미해결 오류, 차단 목록, 인프라 플래그(`infra:<kind>`), 운영자 확인, 권고 검증, 오류 목록·KB 기록, 보고서 쓰기, 그다음 느린 `changes.patch` / `result.zip`(KB 와 보고서가 먼저 — zip 도중 죽어도 잃는 것이 없게) |
| 성공 판정 | 계획한 최종 빌드가 전부 돌았고 전부 성공 + `restructure-failed` 없음 + `hollow-reactor` 없음 + 중단 아님 |
| `tests-not-compiled` | `build.skip_test_compile` 이고 빌드가 한 번이라도 돌았으면 항상 붙는 플래그 + 후속 조치("테스트는 담당팀이 `mvn test-compile` 로") |
| 종료 코드 | `report_exit_code`(`sbmig/report/result_line.py`): `aborted` / `aborted:…` / `mvn-not-found` → 3, 성공이고 차단 없음 → 0, 그 밖 → 2 |
| 마지막 줄 | `SBMIG-RESULT run=… exit=… build=… unresolved=… blocked=… open=… resolved=… llm=… attn=…[ review=…][ major=…]` — 콘솔·`sbmig.log`·보고서 머리 표 |
| 저널 | `report`, 그리고 `build_hook`(teardown: `build.teardown_command`, 실패는 WARNING 만) |

`out/<run id>/report.md` 의 절 순서(`render_markdown`, `sbmig/report/markdown.py`):

```
머리 표 (Result, SBMIG-RESULT 줄, Case, As-is, To-be, LLM, Flags, Blocked, Structure review)
인수인계 (운영자 확인 필요 -> Ant pom 합성 -> 교체된 버전 -> 주의 라이브러리 -> 이제부터 할 일)
ADF sizing (ADF 일 때)
Error list -> Fixes by case -> Structure review -> Bundled jars -> Blocked -> javax API jars
Manual follow-ups -> Flags -> What we changed -> LLM usage and cost -> Notes -> Outputs
```

문구 언어는 `report.language`(en | ko). 운영자용 회사 설정은 ko 를 쓴다. JSON(`out/<run id>/report.json`)은 영어.

KB 쪽: 실행의 오류 목록이 `kb-v2/errors.jsonl` 에 덧붙고, 케이스 폴더 `kb-v2/cases/<case_id>/case.json` 과 저장 스크립트
`kb-v2/cases/<case_id>/fix_v<N>.py` 의 실적이 갱신되고, `kb-v2/cases.md` 가 다시 쓰인다(모두 `kb-v2/.lock` 아래).

## 10. 플래그 한눈에

| 플래그 | 어디서 | 종료 코드 영향 |
|---|---|---|
| `aborted`, `aborted:parent-not-found` | `_abort`, 사내 부모 해석 불가 | 3 |
| `mvn-not-found`, `jdk-not-21`, `unsupported-build-tool:<x>`, `ear-without-modules`, `no-root-pom` | 접수·분석 | 3 |
| `adf-project` | 분석(ADF 2계열 이상, `stop`) | 3 |
| `hollow-reactor` | 분석 | 2 (빌드가 통과해도) |
| `restructure-failed` | 재구성 | 2 |
| `parent-chain-no-boot-bom` | 재구성 | 알림(attn 코드) |
| `tests-not-compiled` | 마무리 | 알림 |
| `bundled-jars`, `stray-pom-ignored`, `pom-synthesized`, `aggregator-generated`, `reactor-duplicate-removed`, `deploy-extensions-removed`, `adf-traces`, `sources-outside-reactor` | 접수·분석 | 기록만 |
| `parent-extension-unresolvable` | 접수 | 사내 부모 체인의 빌드 확장을 로컬·저장소 어디서도 못 읽음 — 모든 Maven 호출이 `Unresolveable build extension` 으로 실패한다. Nexus 에 올리거나 부모에서 빼야 한다 |
| `llm-budget-exhausted`, `time-budget-exhausted`, `infra:<kind>` | 루프·마무리 | 성공이 아니면 2 |

## 11. LangGraph 엔진, inline 엔진, 웹 UI

- 기본 엔진은 `loop.engine: graph`: `build_graph`(`sbmig/graph/engine.py`)가 `StateGraph` 노드 `intake` → `analyze` → `plan` →
  `phase`(phase 묶음마다 반복) → `finalize` (→ `remediation`: `remediation.after_migration` 이고 BUILD SUCCESS 일 때 취약점 조치, 11 문서) → END 를 만든다. 노드마다 오케스트레이터의 같은 `step_*` 메서드를 하나씩 부르고,
  그래프 상태에는 작은 JSON 값만 둔다(무거운 객체는 오케스트레이터에). 노드는 오류를 처리하지 않는다 — 예외는 run guard 로 올라간다.
- `loop.engine: inline` 은 같은 순서를 `Orchestrator._run` 이 그래프 없이 돈다. 보고서·저널·종료 코드가 같다(테스트로 보장).
- 웹 UI: `sbmig serve`(= uvicorn `sbmig.web.app:app`). zip 업로드 → `RunManager`(`sbmig/web/runs.py`)가 실행마다 작업 스레드
  하나에서 `stream_graph` 를 돌린다. 한 번에 이관 하나, 나머지는 `queued`(파일 KB 를 같이 쓰므로). SSE(`/api/runs/{run_id}/events`)
  이벤트: `status`(처음과 끝), `node`(그래프 노드 하나가 끝날 때), `journal`(저널 한 줄), `log`(`sbmig.log` 한 줄), `report`
  (끝난 뒤 보고서 본문), `error`, `ping`, `end`. 중지(Stop)는 노드 사이에서만 반영된다 — 진행 중인 Maven·LLM 호출은 끊지 않고,
  중지된 실행은 최종 빌드 없이 중단(exit 3)으로 마무리된다.
- `--stop-after intake|analyze|plan|transform|restructure|verify` 와 `--dry-run`(결정만 기록, 적용 없음)은 두 엔진에서 같다.

## 확인하는 법

```
grep -n "    def step_\|    def _step_\|    def _finalize" sbmig/loop/orchestrator.py   # 단계 함수
grep -n "self.ws.commit(" sbmig/loop/orchestrator.py sbmig/loop/fixloop.py | head -30  # 커밋 메시지
grep -rhno '"event": "[a-z_]*"' sbmig | sort -u | head -150                          # 저널 이벤트 이름
grep -n "id=\"" sbmig/plan/planner.py                                                # 단계 id
grep -n "recipes:" -A40 config/default.yml                                           # 레시피 세트
grep -n "self._non_filtered_extensions(c)" -A6 sbmig/restructure/restructurer.py     # pom 정리 순서
grep -n "report.success =" -A3 sbmig/loop/orchestrator.py                            # 성공 판정
grep -n "NODES = " sbmig/graph/engine.py; sed -n 15,32p sbmig/web/runs.py           # 그래프 노드, SSE 이벤트
grep -n "L += " sbmig/report/markdown.py | head -20                                  # 보고서 절 순서
.venv/bin/python -m pytest tests/docs/test_agent_doc.py -p no:cacheprovider -q
```
