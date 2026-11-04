# 저장소 지도

> 다루는 것: 최상위 폴더의 역할, `sbmig/` 패키지와 핵심 모듈 한 줄씩, 진입점, 실행 중에 생기는 트리, "이 일을 하려면 이 파일" 표, 테스트·fixture 위치.
> 언제 읽나: 무엇을 고쳐야 하는지는 알지만 어느 파일인지 모를 때. 코드를 열기 전에.
> 함께 읽을 파일: [00-what-sbmig-is.md](00-what-sbmig-is.md), [02-flow-and-nodes.md](02-flow-and-nodes.md), [07-config-and-knowledge.md](07-config-and-knowledge.md), [08-dev-conventions-and-tests.md](08-dev-conventions-and-tests.md)

## 1. 최상위 폴더

| 폴더 / 파일 | 역할 |
|---|---|
| `sbmig/` | 에이전트 본체(Python 패키지). 아래 §2 |
| `config/` | 설정. `config/default.yml`(모든 키의 기본값), `config/artifacts.example.yml`(회사 좌표 자리표시자 — 회사는 복사해 `config/artifacts.yml` 을 만든다), `config/artifacts.local-e2e.yml`(로컬 e2e 용 공개 대역 좌표), `config/llm-anthropic.yml`, `config/llm-plugin.example.yml`, `config/settings.nexus.example.xml` |
| `knowledge/` | 코드와 함께 배포되는 지식 데이터(YAML / md): 레시피 카탈로그 `knowledge/recipes/catalog.yml`, 라이브러리 표·권고 `knowledge/libraries/`, 마이그레이션 노트 `knowledge/notes/`, 규칙 표 `knowledge/config/`, 마스킹 규칙 `knowledge/kb/redact.yml` |
| `docs/` | 설계·계약 문서(영어 위주). `docs/INTERFACES.md`(계약), `docs/PLAN.md`(라운드별 상태), `docs/v2/PLAN-v2.md`, `docs/migration-agent/README.md`(한국어 설계), `docs/handbook/`(운영자용 한국어 핸드북 — 요청이 있을 때만 고친다) |
| `tests/` | pytest. `tests/<패키지>/` 가 `sbmig/<패키지>/` 를 따라간다. 아래 §6 |
| `fixtures/` | 테스트용 실제 Maven 프로젝트(`fixtures/projects/`), 가짜 사내 라이브러리(`fixtures/libs/`), pom 없는 웹앱(`fixtures/webapps/`), 플릿 매니페스트 예(`fixtures/fleet/manifest.example.yml`) |
| `scripts/` | e2e(`scripts/e2e.sh`, `scripts/e2e.py`), KB 데모(`scripts/demo-kb/run.sh`), 플릿 데모(`scripts/fleet-demo.sh`), 핸드북 빌드(`scripts/handbook/`) |
| `deploy/` | 컨테이너·Kubernetes: 이미지 `deploy/docker/Dockerfile`, Job 실행 `deploy/docker/run-job.sh`, 매니페스트 `deploy/k8s/`, 운영자 설명 `deploy/README.md` |
| `coding/` | 같은 게이트웨이 계약을 쓰는 독립 코딩 에이전트(`coding/agent.py`, `coding/README.md`). `sbmig` 의 일부가 아니다 |
| `examples/` | 어댑터 예제: `examples/adapters/company_gateway_adapter.py`(1단 LLM), `examples/adapters/agent_fixer.py`(2단 수정기) |
| `agent-doc/` | 이 문서 묶음(사내 LLM 용 한국어) |
| `CLAUDE.md`, `README.md`, `pyproject.toml`, `.env.example` | 작업 메모(결정의 원본), 소개, 패키지 정의, 환경 변수 예(실제 `.env` 는 git 에 없음) |

실행 중에 생기는 트리(저장소에 커밋하지 않음):

| 트리 | 뜻 |
|---|---|
| `out/<run id>/` | 실행 하나의 디렉토리(기본 `--out out`). `project/`(git 저장소인 작업 사본), `logs/`, `journal.jsonl`, `sbmig.log`, 오류 목록 두 파일, 프로필·계획 json, 보고서 두 파일, `changes.patch`, `result.zip` |
| `kb-v2/` | 파일 KB 의 기본 위치(`kb.dir`). `kb-v2/errors.jsonl`, `kb-v2/cases.md`, `kb-v2/cases/<case_id>/case.json`, `kb-v2/cases/<case_id>/fix_v<N>.py`, `kb-v2/.lock` |
| `out/e2e/` | e2e 결과(`out/e2e/summary.md`)와 e2e 전용 KB(`out/e2e/kb-…`) — 불변조건 6 |

저장소 루트의 `kb/` 폴더(빈 `.gitkeep` 뿐)는 v1 시절 KB 자리의 흔적이다. v2 는 쓰지 않는다.

## 2. `sbmig/` 패키지 지도

### 진입점

| 파일 | 역할 |
|---|---|
| `sbmig/cli.py` | Typer 명령줄. `sbmig run`, `sbmig serve`, `sbmig llm-probe`, `sbmig repo-probe`, `sbmig analyze`, `sbmig plan`, `sbmig inventory`, `sbmig build`, `sbmig parse-log`, `sbmig rewrite`, `sbmig clean`, `sbmig version` + 하위 앱 `sbmig kb`, `sbmig recipes`, `sbmig fleet`, `sbmig advisories` |
| `sbmig/api.py` | 프로그램용 진입점 `migrate(...)` — `sbmig run` 과 같은 파이프라인(불변조건 4) |
| `sbmig/loop/orchestrator.py` | 실행 한 번을 모는 `Orchestrator`: `step_intake` → `step_analyze` → `step_plan` → `step_phase`(×N) → `_finalize` |
| `sbmig/graph/engine.py` | LangGraph 엔진(`loop.engine: graph`, 기본): `build_graph`, `run_graph`(CLI), `stream_graph`(웹 UI) |
| `sbmig/web/app.py` | FastAPI 웹 UI(`sbmig serve` = uvicorn). 실행 관리는 `sbmig/web/runs.py` |

### 패키지별 핵심 모듈

| 패키지 | 한 줄 역할 | 핵심 모듈(한 줄씩) |
|---|---|---|
| `sbmig/core/` | 모든 패키지가 import 하는 공유 계약 | `models.py` 모든 pydantic 모델·Enum(ProjectProfile, PlanStep, BuildError, FixAttempt, RunReport …) · `config.py` 설정 스키마와 `load_config`(default.yml → `--config` → 덮어쓰기 → `SBMIG_*` 환경 변수, 뒤가 이김) · `interfaces.py` 플러그인 Protocol(이음새) · `workspace.py` 작업 사본·실행 디렉토리·git·저널 · `stage.py` 로그 줄의 단계 태그 · `diag.py` 스레드 덤프 · `jakarta_libraries.py` jakarta 릴리스 표 읽기 |
| `sbmig/analyze/` | 접수 보조 + 프로필 만들기 | `analyzer.py` `analyze_project` 본체 · `pom_parser.py` pom 그래프(Maven 호출 없음), `find_stray_pom_copies` · `classifier.py` 유형 판정·MSA 서비스 루트 · `source_scanner.py` 정규식 소스 스캔 · `libraries.py` 표 기반 라이브러리 탐지 · `build_tool.py` Maven / Ant / Gradle 판별 · `ant.py` build.xml → pom.xml 합성 · `jar_coords.py` jar → Maven 좌표 · `aggregator.py` 루트 없는 트리의 aggregator, reactor 중복 정리 · `bundled_jars.py` 동봉 jar 스캔 · `hollow.py` 속 빈 리액터 · `adf.py` Oracle ADF 판정 · `edges.py` 특이 사항 경고 · `properties.py` 외부 설정 키 · `webxml.py` web.xml 모델 · `jsp.py` JSP 코드 영역 · `webapp_inventory.py` `sbmig inventory` 용 목록 · `jakarta_packages.yml` javax → jakarta 패키지 표 |
| `sbmig/plan/` | 규칙 기반 계획 + 레시피 카탈로그 | `planner.py` `build_plan`(단계 목록) · `catalog.py` 레시피 카탈로그 · `catalog_cli.py` `sbmig recipes …` · `jarcheck.py` 레시피 jar 검사 · `versions.py` 버전 비교 |
| `sbmig/rewrite/` | OpenRewrite 1회 실행 | `runner.py` `OpenRewriteRunner` · `compose.py` 이번 실행의 `rewrite.yml` 합성 · `versions.py` 레시피 아티팩트 버전(BOM 3.37.0) |
| `sbmig/restructure/` | 회사 레이아웃(LLM 없음) | `restructurer.py` `Restructurer.apply`(모드 layout / boot_pom / msa_pom / optional_bump, 버전 정리 전부) · `pom_editor.py` 서식을 지키는 lxml pom 편집기 · `bundled_jars.py` 동봉 jar 판정·연결 · `system_jars.py` `wire_bundled_jar` · `resources_filtering.py` 필터링되는 바이너리의 `nonFilteredFileExtensions` · `jsp_jakarta.py` JSP javax → jakarta · `legacy_context.py` / `legacy_apply.py` Spring XML 컨텍스트를 Boot 로 · `webxml_registrations.py` web.xml → `WebXmlRegistrations` · `templates/` Jinja2 템플릿(boot / comp / front pom, Application, WebXmlRegistrations) |
| `sbmig/review/` | 구조 검토(LLM) | `reviewer.py` `StructureReviewer` · `inputs.py` LLM 에 보여 줄 사실 모으기 · `parse.py` `<review>` 답변 읽기 · `prompts/review_v1.md` 시스템 프롬프트 |
| `sbmig/build/` | Maven 실행과 빌드 주변 사실 | `runner.py` 빌드 한 번 → `BuildResult` · `mvn.py` 공통 명령·환경 · `log_parser.py` 로그 → `BuildError` · `signatures.py` L1 / L2 서명 · `effective.py` as-is effective dependencyManagement 캡처 · `parent_chain.py` 사내 부모 체인 사실 · `boot_bom.py` Boot BOM 관리 버전 · `cloud_bom.py` Spring Cloud 트레인 짝 · `repo_versions.py` 저장소 릴리스 조회 · `jakarta_discovery.py` jakarta 빌드 탐색 · `depchanges.py` 시도가 바꾼 의존성 되읽기 · `classpath.py` / `deptree.py` 클래스·jar 귀속 · `env_errors.py` 빌드 PC 탓 오류(파일 잠금) |
| `sbmig/triage/` | 오류 → 케이스 → 할 일 | `cluster.py` 케이스별 묶기, 근본 원인 먼저 · `decide.py` SCRIPT / BLOCKED / SKIP · `scripts.py` 사다리(재사용 → 보강 → 신규 → 차단) · `blocked.py` 프로젝트 안에서 못 고치는 오류 분류 · `triager.py` 창구 |
| `sbmig/loop/` | 실행의 심장 | `orchestrator.py` 단계 구동·마무리 · `fixloop.py` `BuildFixLoop`(라운드, 사전 시도, 채점, 되돌리기, `VersionResolver`) · `blocked_registry.py` 실행 전체의 차단 등록부 · `budget.py` LLM 호출·시간 예산 · `entries.py` 오류 목록·KB 로 가는 통로 · `layout_facts.py` 재구성 뒤 사실(프롬프트 절) · `notes.py` 수정 기록 · `components.py` 늦은 import |
| `sbmig/fix/` | LLM 에게 스크립트를 받는 쪽 | `fixer.py` `LLMFixer`(`propose_script`) · `context.py` 프롬프트 문맥 꾸리기 · `script_protocol.py` `<analysis>` `<edits>` `<script>` 답변 파서 · `client.py` LLM 클라이언트들 · `company.py` 회사 게이트웨이(`requests.post`) · `prompts/` 수정 프롬프트 |
| `sbmig/llm/` | 1단 어댑터 이음새 | `adapter.py` `LLMAdapterLike` 감싸개 · `registry.py` 플러그인 불러오기 |
| `sbmig/script/` | 수정 스크립트 실행기(불변조건 7) | `runner.py` `ScriptRunner` · `check.py` ast 정적 검사 · `harness.py` 자식 프로세스 쪽 · `api.py` 도우미 API `p` · `edits.py` 스크립트 없는 편집 적용 · `notes.py` 수정 기록·변경 조각 |
| `sbmig/errors/` | 실행 오류 목록과 KB 저장소 | `entries.py` `ErrorList`(실행 디렉토리의 오류 목록) · `cases.py` `CaseStore`(케이스 폴더, 저장 스크립트, 실적) · `common.py` ID·마스킹·원자적 쓰기 |
| `sbmig/kb/` | KB 창구와 `sbmig kb` 명령 | `store.py` `FileKnowledgeBase` · `cli.py` `sbmig kb …` · `lock.py` 프로세스 간 잠금 · `jsonl.py` 덧붙이기 전용 jsonl · `redact.py` 비밀값 마스킹 · `catalog.py` 옛 ID 호환 |
| `sbmig/advisory/` | EOL 라이브러리 권고(안내만) | `catalog.py` 권고 목록 합치기 · `evaluate.py` 프로젝트에 맞는 권고 · `hints.py` 프롬프트 힌트 · `verify.py` 마무리 검증 · `cli.py` `sbmig advisories …` |
| `sbmig/report/` | 보고서 | `writer.py` `write_report` · `markdown.py` `render_markdown`(절 순서) · `handover.py` 인수인계 데이터 · `result_line.py` `SBMIG-RESULT` 줄·종료 코드 · `i18n.py` 한국어 문구 · `errors.py` 오류 목록 가공 · `cost.py` LLM 비용 · `inventory.py` `sbmig inventory` 산출물 |
| `sbmig/fleet/` | 순차 일괄 실행 | `runner.py` `FleetRunner` · `manifest.py` 매니페스트 · `preflight.py` 인프라 점검 · `summary.py` 요약 파일 · `cleanup.py` 디스크 정리 · `cli.py` `sbmig fleet …` |
| `sbmig/graph/` | LangGraph 엔진 | `engine.py` 노드 intake → analyze → plan → phase → finalize |
| `sbmig/web/` | 웹 UI | `app.py` 라우트(`/api/runs`, `/api/runs/{run_id}/events` …) · `runs.py` 실행 관리자·SSE 이벤트 · `static/index.html` 화면(바닐라 JS) |

모듈마다 docstring 맨 앞에 한국어 블록(요약 → 흐름상 위치 → 진입점 → 지킬 것)이 있다. 규약: `docs/v2/CODE-COMMENTS-KO.md`.

## 3. 이 일을 하려면 이 파일

| 하려는 일 | 먼저 볼 파일 | 함께 볼 것 |
|---|---|---|
| 빌드 오류 서명(L1 / L2)을 바꾼다 | `sbmig/build/signatures.py` | `sbmig/build/log_parser.py`; 바꾸면 저장된 케이스 ID 가 고아가 된다([10-open-items-and-history.md](10-open-items-and-history.md)) |
| 케이스 묶기·순서(근본 원인 먼저)·미루기 | `sbmig/triage/cluster.py` | `sbmig/loop/fixloop.py` 의 `_deferral` |
| 사다리(재사용·보강·신규·한도) | `sbmig/triage/scripts.py` | `script.max_llm_tries_per_case`, `script.max_augments_per_case` |
| 차단 사유·정책 | `sbmig/triage/blocked.py` | `loop.blocked_policy`, `sbmig/loop/blocked_registry.py` |
| 사전 시도(`sbmig-version` 등) | `sbmig/loop/fixloop.py` | `sbmig/restructure/resources_filtering.py`, `sbmig/build/jakarta_discovery.py`, `sbmig/restructure/system_jars.py` |
| 채점·되돌리기 | `sbmig/loop/fixloop.py` (`_score`) | `loop.revert_on_regression`, `loop.regression_ratio` |
| 회사 레이아웃 규칙 | `sbmig/restructure/restructurer.py` | `layout.*`, `artifacts.*`, [06-company-rules-restructure.md](06-company-rules-restructure.md) |
| pom 쓰기(서식 유지) | `sbmig/restructure/pom_editor.py` | 템플릿 `sbmig/restructure/templates/boot-pom.xml.j2` |
| 버전 정책(추측 금지) | `sbmig/build/parent_chain.py` | `sbmig/build/effective.py`, `sbmig/build/boot_bom.py`, `VersionResolver` in `sbmig/loop/fixloop.py` |
| 저장소에서 릴리스·jakarta 빌드 찾기 | `sbmig/build/repo_versions.py` | `sbmig/build/jakarta_discovery.py`, `sbmig repo-probe` |
| 동봉 jar | `sbmig/analyze/bundled_jars.py` | `sbmig/restructure/bundled_jars.py`, `layout.wire_bundled_jars`, `intake.bundled_jars` |
| Ant 프로젝트 pom 합성 | `sbmig/analyze/ant.py` | `sbmig/analyze/jar_coords.py`, `knowledge/libraries/jar_names.yml`, `intake.*` |
| 루트 없는 트리·reactor 중복 | `sbmig/analyze/aggregator.py` | `layout.generate_aggregator` |
| ADF / 속 빈 리액터 판정 | `sbmig/analyze/adf.py`, `sbmig/analyze/hollow.py` | `intake.adf_policy` |
| 프로젝트 유형 판정 | `sbmig/analyze/classifier.py` | `sbmig/analyze/source_scanner.py` |
| 라이브러리 탐지 표 | `sbmig/analyze/libraries.py` | `knowledge/libraries/jakarta_libraries.yml`, `knowledge/libraries/libraries.company.example.yml` |
| 권고(EOL 안내) | `knowledge/libraries/advisories.yml` | `sbmig/advisory/`, [../docs/ADVISORIES.md](../docs/ADVISORIES.md) |
| 마이그레이션 노트(프롬프트에 붙는 글) | `knowledge/notes/index.yml` | `knowledge/notes/*.md`, `sbmig/fix/context.py` |
| 수정 LLM 프롬프트 | `sbmig/fix/prompts/script_system_v1.md` | `sbmig/fix/prompts/script_user_v1.md.j2`, `sbmig/fix/context.py`, `sbmig/fix/fixer.py` |
| 구조 검토 프롬프트 | `sbmig/review/prompts/review_v1.md` | `sbmig/review/inputs.py`, `sbmig/review/parse.py` |
| LLM 게이트웨이 호출 | `sbmig/fix/company.py` | `sbmig/fix/client.py`, `llm.*`, [05-llm-gateway-and-prompts.md](05-llm-gateway-and-prompts.md) |
| 스크립트 실행 API(`p`) | `sbmig/script/api.py` | `sbmig/script/check.py`(허용 import), `sbmig/script/runner.py`, `script.*` |
| 보고서 절 추가·순서 | `sbmig/report/markdown.py` | `sbmig/report/handover.py`, 문구 번역 `sbmig/report/i18n.py` |
| `SBMIG-RESULT` 줄·attn 코드·종료 코드 | `sbmig/report/result_line.py` | `sbmig/cli.py` (`run_exit_code`) |
| 저널 이벤트 추가 | 이벤트를 내는 모듈의 `self.ws.journal({...})` | `Workspace.journal` in `sbmig/core/workspace.py` |
| 설정 키 추가 | `sbmig/core/config.py` | `config/default.yml`, 문서 `docs/INTERFACES.md` |
| 웹 UI 이벤트·화면 | `sbmig/web/runs.py` | `sbmig/web/app.py`, `sbmig/web/static/index.html` |
| 플릿(일괄 실행) | `sbmig/fleet/runner.py` | `sbmig/fleet/manifest.py`, [../docs/FLEET.md](../docs/FLEET.md) |
| 레시피 목록 | `knowledge/recipes/catalog.yml` | `openrewrite.recipes`, `sbmig/plan/planner.py`, `sbmig recipes check` |
| KB 저장 형식 | `sbmig/errors/cases.py` | `sbmig/kb/store.py`, `sbmig/kb/cli.py`, [04-cases-scripts-kb.md](04-cases-scripts-kb.md) |
| 비밀값 마스킹 규칙 | `knowledge/kb/redact.yml` | `sbmig/kb/redact.py` |
| 빌드 환경 오류 패턴 | `knowledge/config/build_env_errors.yml` | `sbmig/build/env_errors.py` |
| web.xml → Boot 등록 | `sbmig/restructure/webxml_registrations.py` | `knowledge/config/web_xml_rules.yml` |
| Spring XML 컨텍스트 전환 | `sbmig/restructure/legacy_apply.py` | `knowledge/config/legacy_context_rules.yml`, `layout.legacy_context` |
| 외부 설정 자리표시자 | `sbmig/analyze/properties.py` | `knowledge/config/externalized.yml`, `externalized.*` |
| 1단 / 2단 어댑터 끼우기 | `sbmig/llm/registry.py` | `examples/adapters/README.md`, [../docs/ADAPTERS.md](../docs/ADAPTERS.md) |
| 컨테이너·k8s | `deploy/README.md` | `deploy/docker/Dockerfile`, `deploy/k8s/80-build-job.template.yaml`, [09-deploy-and-ops.md](09-deploy-and-ops.md) |

## 4. 지식 데이터(`knowledge/`)

| 파일 | 내용 |
|---|---|
| `knowledge/recipes/catalog.yml` | 레시피 id 의 유일한 목록(불변조건 5) |
| `knowledge/libraries/jakarta_libraries.yml` | javax 시절 라이브러리 → jakarta 릴리스 표 |
| `knowledge/libraries/advisories.yml` | EOL 권고 |
| `knowledge/libraries/jar_names.yml` | jar 파일 이름 → 좌표(Ant 접수, 동봉 jar) |
| `knowledge/notes/` | LLM 프롬프트에 붙는 마이그레이션 노트(`knowledge/notes/spring-mobile.md` 등), 목록 `knowledge/notes/index.yml` |
| `knowledge/config/` | 규칙 표: 빌드 환경 오류, 외부 설정, legacy 컨텍스트, Spring Cloud BOM 짝(`knowledge/config/spring_cloud_bom.yml`), web.xml |

회사가 재배포 없이 고치는 표는 KB 폴더에 둔다: `kb-v2/libraries.yml`(`libraries.company_file`), `kb-v2/jar_names.yml`
(`intake.company_jar_names_file`). 자세히는 [07-config-and-knowledge.md](07-config-and-knowledge.md).

## 5. 프롬프트·템플릿 위치

| 무엇 | 파일 |
|---|---|
| 수정 스크립트 시스템 / 사용자 프롬프트 | `sbmig/fix/prompts/script_system_v1.md`, `sbmig/fix/prompts/script_user_v1.md.j2` |
| 메이저 업그레이드 확인 안내 | `sbmig/fix/prompts/verify_system_v1.md` |
| 구조 검토 | `sbmig/review/prompts/review_v1.md` |
| 재구성 템플릿 | `sbmig/restructure/templates/` (boot / comp / front pom, `Application.java.j2`, `WebXmlRegistrations.java.j2`) |

프롬프트 `.md` 는 영어로 둔다(번역 금지).

## 6. 테스트와 fixture

- `tests/<패키지>/` 가 `sbmig/<패키지>/` 를 따라간다: `tests/analyze/`, `tests/build/`, `tests/loop/`, `tests/restructure/`,
  `tests/review/`, `tests/fix/`, `tests/script/`, `tests/triage/`, `tests/report/`, `tests/kb/`, `tests/fleet/`, `tests/graph/`,
  `tests/web/` … 공용 준비는 `tests/conftest.py`.
- `tests/docs/` 는 문서 검사: `tests/docs/test_agent_doc.py`(이 묶음), `tests/docs/test_handbook.py`(핸드북),
  `tests/docs/test_korean_comments.py`(한국어 docstring 규약).
- `tests/fixtures/` 는 fixture 프로젝트 자체를 검사한다(`tests/fixtures/test_fixture_projects.py`, `tests/fixtures/test_e2e_script.py`).
- fixture 프로젝트: `fixtures/projects/<이름>/` 마다 `README.md`(as-is 빌드 명령), `expected.json`(분석 기대값 + `expect`
  실행 결과), `sbmig.e2e.yml`(가짜 LLM 재생 설정과 `out/` 아래 전용 KB), `replay/llm-NNN.json`(재생할 LLM 답). 예:
  `fixtures/projects/pure-java/`, `fixtures/projects/webapp-bundled-jar/`, `fixtures/projects/hollow-reactor/`,
  `fixtures/projects/adf-jdev/`, `fixtures/projects/boot-stray-pom-copy/`, `fixtures/projects/ant-webapp/`. 전체 표:
  `fixtures/projects/README.md`.
- 새 시나리오마다 fixture + `expected.json` (+ `replay/`) 을 추가한다. 테스트 실행법은 [08-dev-conventions-and-tests.md](08-dev-conventions-and-tests.md).

## 확인하는 법

```
ls sbmig sbmig/*/                                   # 패키지·모듈 목록
.venv/bin/python -m sbmig.cli --help                # 명령 목록
python - <<'EOF'                                    # 모듈마다 docstring 첫 줄(한국어 블록)
import ast, pathlib
for p in sorted(pathlib.Path("sbmig").rglob("*.py")):
    d = ast.get_docstring(ast.parse(p.read_text())) or ""
    print(p, "::", d.strip().split("\n")[0][:120])
EOF
grep -n "^  dir:" config/default.yml                # KB 기본 위치
sed -n 1,25p sbmig/core/workspace.py                # 실행 디렉토리 모양
ls fixtures/projects/pure-java                      # fixture 한 개의 모양
.venv/bin/python -m pytest tests/docs/test_agent_doc.py -p no:cacheprovider -q
```
