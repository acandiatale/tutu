# 한국어 주석 작업 중 발견한 것 (2026-10-28)

`sbmig/` 전체(121 파일)에 한국어 주석을 달면서 코드를 한 줄씩 읽은 결과다. **코드는 고치지 않았다** — 주석 작업은
동작을 바꾸지 않는다는 원칙(`docs/v2/CODE-COMMENTS-KO.md` 규칙 1·7) 때문이고, 아래 항목은 사람이 결정한다.
확신도는 "읽어서 의심" 수준이다(재현한 것은 따로 표시).

## A. 의심 버그 · 정책 질문

| # | 위치 | 내용 |
|---|---|---|
| A1 | `sbmig/analyze/pom_parser.py` `parse_pom_text` | **재현됨.** `encoding="EUC-KR"`(또는 MS949) 선언이 있는 pom 은 텍스트를 UTF-8 로 다시 인코딩한 뒤 expat 에 넘겨 `ValueError: multi-byte encodings are not supported` 가 난다. `parse_poms` 는 "pom skipped" 경고만 내고 그 모듈 전체가 프로필에서 빠진다. 한국 레거시 프로젝트에서 있을 법한 경우. `source_scanner._xml_root` / `webxml._parse_root` 는 선언을 먼저 떼어 내므로 문제 없음 — 같은 방식으로 고치면 된다. |
| A2 | `sbmig/build/repo_versions.py` `_verdict_line` | 같은 메이저에 jakarta 빌드가 없을 때 판정 줄은 상위 메이저 중 **가장 높은** 것(`higher_jakarta[-1]`)을 권한다(2.x·3.x·4.x 모두 jakarta 빌드가 있으면 4.x). "최소 점프" 가 정책이면 `[0]` 이어야 한다. **정책 결정 필요.** |
| A3 | `docs/v2/KB-AND-FLOW.md` §1·§6 ↔ `sbmig/review/reviewer.py` | 구조 검토 LLM 호출 한도가 문서는 최대 2회, 코드는 3회(검토 1 + 형식 재시도 `_ask` 1 + 적용 실패 재시도 `_apply_retry` 1). 문서나 코드 중 하나를 맞춰야 한다. 한국어 docstring 은 코드대로 "최대 3회" 로 썼다. |
| A4 | `sbmig/loop/fixloop.py:2069` | `edit_applier` 플러그인에 문서화된 시그니처(`fn(ws, edits, *, cfg)`) 외에 `errors=` 를 더 넘긴다. 문서대로 만든 사용자 applier 는 TypeError → 모든 편집이 "적용 불가" 로 실패. 기본 `apply_edits` 와 테스트 가짜는 받는다. |
| A5 | `sbmig/analyze/source_scanner.py` `SourceStats.jsp_javax_files` | 계산만 되고 보고서·Handover·검토 어디에서도 읽지 않는다. CLAUDE.md 는 JSP javax 를 "Handover only" 라고 하지만 실제 경로가 없다. |
| A6 | `sbmig/core/config.py` `LLMConfig.provider` | 클래스 기본값은 `openai_compatible`, `config/default.yml` 은 `company`. yml 없이 `Config()` 를 만들면 다른 provider 가 된다. |
| A7 | `sbmig/fleet/manifest.py:184` | 디렉토리 manifest 는 바로 아래 `pom.xml` 이 있는 폴더만 나열하는데 `_validate` 는 `*/pom.xml` 도 받는다. 모듈 pom 만 있는 프로젝트 폴더는 skipped 표시도 없이 빠진다. |
| A8 | `sbmig/fleet/runner.py:243` | 입력이 사라진 행은 `pending` / `skipped` 일 때만 skipped 로 바꾼다. `running` 이거나 `--retry-failed` 의 `incomplete` / `aborted` 행은 실행되어 crash → aborted. |
| A9 | `sbmig/fleet/runner.py:595, 605` | 재시도가 crash 하면 이전 시도의 `llm_calls` / `unresolved` / `blocked` 가 남아 SBMIG-RESULT 와 summary 가 옛 숫자를 보인다. 성공한 재시도는 `llm_calls` 를 덮어써 `max_llm_calls_total` 예산이 과소 계산된다. |
| A10 | `sbmig/web/runs.py:232` | `threading.Lock` 은 FIFO 를 보장하지 않는다. 업로드가 2개 이상 대기하면 올린 순서와 다르게 시작할 수 있다. |
| A11 | `sbmig/web/app.py:92` | Python < 3.13 은 `::ffff:127.0.0.1` 을 loopback 으로 보지 않는다. `::` 로 바인딩하고 토큰이 없으면 로컬 브라우저가 403 을 받을 수 있다(저위험). 역프록시 뒤에서는 모든 클라이언트가 loopback 으로 보여 토큰을 요구하지 않는다(함정). |
| A12 | `sbmig/report/writer.py:57` | `write_report` 가 `render_markdown` 에 `cfg` 를 넘기지 않아 javax jar 의 "사내 → Jakarta 빌드" 검사가 설정된 회사 그룹을 못 보고 Handover 부모의 그룹으로 대체한다. |
| A13 | `sbmig/report/writer.py:88` | `properties_console_line` 은 인벤토리를 `profile.properties` 로 대체하면서 플레이스홀더 파일 목록은 `report.properties` 에서만 읽는다. 그 경우 파일 목록이 빈다. |
| A14 | `sbmig/triage/scripts.py:142` | `script.reuse_versions` 를 1 보다 크게 해도 효과가 없다. `store.best()` 하나만 보므로 그 버전이 이번 실행에서 실패하면 두 번째 저장 버전은 쓰지 않는다. |
| A15 | `sbmig/errors/cases.py:730, 767` | `hunked` 가 hunk 보충과 fixer 귀속을 함께 세어 "entr(ies) given as-is/to-be hunks" 숫자(INFO 줄, `kb reindex` 출력)가 실제보다 크다. |
| A16 | `sbmig/errors/cases.py:951, 766` | `resolved_by` 보충이 `_backfill_hunks` 안에서만 돌아, 그 함수가 일찍 return 하면(`attach_hunks` import 실패, errors.jsonl 못 읽음) 건너뛴다. `_fixers_backfilled` 는 그 early return 뒤에만 초기화돼 같은 store 에서 `recount` 를 반복하면 옛 숫자를 보고할 수 있다. |
| A17 | `sbmig/errors/cases.py:1120` | `merge_from` 은 다음 버전 번호를 case.json 에 적힌 스크립트만 보고 정한다. 디스크에만 있는 `fix_v*.py` 를 덮어쓸 수 있다(`add_script` 는 디스크도 본다). |
| A18 | `sbmig/errors/cases.py:518` | `activate` 는 `s.project` 가 비어 있으면(옛 기록) 자기 프로젝트도 "다른 프로젝트" 로 세어 proven 처리한다. `credit` 는 run id 로 대체한다. |
| A19 | `sbmig/kb/cli.py:308` | `cases pin` 은 10자짜리 아무 키나 그대로 받는다. 오타 id 면 새 케이스 폴더가 만들어지고 고정된다. |
| A20 | `sbmig/fix/fixer.py:216` | `profile_summary` 가 "target: JDK 21, Spring Boot 4.1" 을 하드코딩한다(`cfg.target` 을 읽지 않음). |
| A21 | `sbmig/fix/fixer.py`, `sbmig/core/interfaces.py:74` | `verify_guide` 역할이 `LLMRole` 에 없다. 플러그인 어댑터는 `other` + `metadata.role_detail` 로 받고, replay 의 `replay_role: fix` 는 답하지 않는다(일관되지만 문서 없음). |
| A22 | `sbmig/loop/entries.py:76` | `_Guarded._call` 은 선택 키워드 이름이 든 TypeError 면 그 인자를 빼고 재시도한다. callee 안에서 난 TypeError 면 호출이 두 번 될 수 있다(`begin_try` 부작용 중복, 저위험). |
| A23 | `sbmig/loop/orchestrator.py:1457` | 검토가 고친 치명 항목마다 검토의 LLM 호출 수 전체를 기록한다. `FixTry.llm_calls` 를 합산하면 과다(저확신; 보고서의 사용량 숫자는 다른 데서 온다). |
| A24 | `sbmig/restructure/restructurer.py:2786` | 같은 목적지로 매핑되는 JSP 가 둘이면 `c.jsp_moved` 가 하나를 두 번 센다(예/아니오로만 쓰여 영향 적음). |
| A25 | `sbmig/analyze/classifier.py` `build_services` | 중복 서비스 이름을 `name-2` 로 바꿀 때 이미 `name-2` 인 서비스와 충돌할 수 있다(minor). |
| A26 | `sbmig/core/models.py` `GAV.parse` | `g:a:jar:1.0` 꼴이면 세 번째 조각(`jar`)을 버전으로 본다. 현재 호출자는 안전. |

## B. 안 읽는 설정 · 죽은 코드

- `config.py`: `review.record`, `openrewrite.fail_on_dry_run_results` 는 어디서도 읽지 않는다(한국어 주석에 적어 둠).
- `build/effective.py`: `effective_pom_provider`, `effective_version` 은 테스트만 쓴다(오케스트레이터는 `_effective_pom_after`).
- `build/log_parser.py:724` `if True:` 잔재; `rewrite/compose.py` `_source_files` 의 `recipes` 인자 미사용; `loop/fixloop.py:137`
  `SCRIPT_KEEP` 미사용; `kb/common.py` `rel_posix` / `changes_as_diff`, `kb/redact.py` `overlaps` / `clear_cache` 호출자 없음;
  `triage/cluster.py` `cluster_errors` 는 재export 만.
- `core/models.py`: `ScriptMeta` 미사용; "Error cases" 섹션 모델(`ErrorCaseRecord` / `FixAttemptRecord` / `CatalogCase*`)은
  v2.1 에서 채우지 않는다(`RunReport.error_cases` 항상 빔).

## C. 낡은 영어 docstring · 운영자에게 보이는 v1 문구

한국어 docstring 은 **지금 코드** 기준으로 썼고 영어 원문은 그대로 두었다. 아래는 영어가 v1(핸들러·레시피·SEARCH/REPLACE·
pom 합성) 시절 설명인 곳이다.

**운영자에게 보이는 문자열(코드라 안 고침)**: `analyze/webapp_inventory.py:564` ("source-encoding handler"),
`analyze/edges.py:42` ("handler jakarta-library-floor"), `analyze/edges.py:336` ("enable convert-utf8"),
`report/markdown.py:45` (`llm-budget-exhausted` 도움말의 "escalated" — v2 는 `budget_exhausted` 로 차단),
`plan/planner.py:259, 266` ("critical issues → one fix script" — 지금은 edits 와/또는 script),
`report/inventory.py:160` ("`sbmig synthesize-pom` will write the poms" — 2026-10-23 결정과 모순),
`review/inputs.py:406` (프롬프트의 "one `<script>`" — v2.1 은 `<edits>` 적용), `cli.py:269` (`--llm-provider` 도움말에 `company` 없음).

**docstring / 주석**:
- analyze: `libraries.py:12`, `webapp_inventory.py:17`, `source_scanner.py:187`, `properties.py:892, 897`.
- build / rewrite: `effective.py:23`, `classpath.py:455`, `depchanges.py:369`, `repo_versions.py:759`, `runner.py:104`,
  `log_parser.py:431`, `rewrite/compose.py:13, 41, 133`, `rewrite/runner.py:105, 466`, `rewrite/versions.py:154`.
- core: `models.py:177, 235, 516, 612, 798, 826, 1220, 1338, 1411, 1760, 1801, 1834`, `config.py:403, 519, 525, 532, 618`,
  `interfaces.py:25, 109, 137, 382`, `jakarta_libraries.py:45, 47`, `api.py:71`.
- errors / kb: `cases.py:917, 1020, 1076`, `kb/lock.py:13, 18`, `kb/common.py:11, 64`, `kb/redact.py:37`.
- fix / llm / review: `fix/client.py:16`, `fix/fixer.py:99, 121`, `review/reviewer.py:102`, `docs/ADAPTERS.md` tier-2 절(`propose` / `apply` / PatchApplier).
- fleet / web: `web/runs.py:12, 19, 34`, `web/app.py:14`, `fleet/cleanup.py:19`, `fleet/summary.py:57`, `fleet/__init__.py:14`.
- loop: `orchestrator.py:30, 213, 1322`, `blocked_registry.py:15`, `fixloop.py:25, 37, 396, 1396`, `entries.py:16`.
- report / triage / advisory / plan: `handover.py:22, 579`, `triage/decide.py:21, 249`, `triage/cluster.py:38`, `advisory/__init__.py:33, 51`.
- restructure: `restructurer.py:680, 846, 1030, 2666, 2676, 3249`.

## D. 주석 작업이 코드에 남긴 유일한 변화 (동작 변화 없음)

- docstring 이 생겨 불필요해진 `pass` 네 줄 삭제: `RestructureError`, `GitError`, `LLMConfigError`, `ManifestError`.
- `sbmig/__init__.py` 의 영어 파이프라인 줄(v1 설명)을 v2 흐름으로 고침.
- 규약 예외: FastAPI 라우트 핸들러와 Typer 콜백(`_main`)은 docstring 이 OpenAPI 설명 / `--help` 가 되므로 `#` 주석으로 달았다.
  Protocol 의 한 줄 `...` 스텁은 클래스 docstring 이 대신한다.

## 처리 (2026-10-28, 사용자 결정 → v2.1h)

| 항목 | 결정 | 처리 |
|---|---|---|
| A1 | 고친다 | `analyze/pom_parser.py` (`pom_encoding` / `xml_codec` / `xml_without_declaration`, `read_pom_text`), `edges.py`, `source_scanner.py`, `webxml.py` 가 같은 헬퍼를 쓴다. 실제로는 `x-windows-949` 선언이면 `LookupError` 로 분석 전체가 죽던 문제도 포함. 같은 약점이 있던 `restructure/pom_editor.py` 도 같은 방식으로 고침(선언은 그대로 두고 cp949 로 저장). |
| A2 | 이대로 간다 | 변경 없음(판정 줄은 상위 메이저 중 가장 높은 것). |
| A3 | 3회로 맞춘다 | 문서(`docs/v2/KB-AND-FLOW.md`)를 코드대로 3회(검토 1 + 형식 재시도 1 + 적용 실패 재시도 1)로. 코드 변경 없음. |
| A4 | (c) 둘 다 ("C로 가자") | 문서화된 모양을 `fn(ws, edits, *, cfg, errors=None)` 로 적고(`docs/ADAPTERS.md`), 루프는 `fixloop.call_edit_applier` 가 시그니처를 보고 `errors` 를 받을 때만 넘긴다. 아래 "A4 가 무엇인가" 참고. |
| A5 | JSP 도 javax → jakarta | `restructure/jsp_jakarta.py` + `layout.jsp_jakarta_rename`(기본 true): 모든 모드에서 재구성 끝에 JSP / JSPF / JSPX / tag 파일의 코드 영역(page import, useBean, 스크립틀릿)만 분석기의 표대로 바꾼다. 문자열·주석·taglib URI 는 그대로. 파일은 제 인코딩(UTF-8 / cp949)으로 다시 쓴다. 보고서 "What we changed" 에 한 줄, 끄면 Handover 항목. |
| A6–A26 | 버그는 고친다 | A6, A7, A8(실행될 행만 skipped — 성공한 행은 그대로), A9(상태의 LLM 호출·비용은 모든 시도 합산), A10, A11, A12, A13, A14, A15, A16, A17, A18, A19(`cases pin` 은 아는 id 또는 `--create`), A20, A21, A22, A23, A24, A25, A26 처리. §B(죽은 코드·안 읽는 설정)는 손대지 않음. |

### A4 가 무엇인가 (결정: (c))

수정 루프는 LLM 이 돌려준 `<edits>` 를 파일에 적용할 때 **edit applier** 라는 함수를 부른다. 기본 구현은 `sbmig.script.edits.apply_edits` 이고,
회사가 자기 것으로 바꿔 끼울 수 있는 플러그인 자리다(`docs/INTERFACES.md`·`orchestrator.py` 의 문서화된 모양: `fn(ws, edits, *, cfg)`).
그런데 루프는 실제로 `fn(ws, edits, cfg=cfg, errors=<이번 케이스의 오류 목록>)` 으로 인자 하나를 더 넘긴다(`fixloop.py:2069`).
기본 구현은 `errors=` 를 받으므로 지금 실행에는 문제가 없다. 문제는 **문서대로** `errors=` 없이 만든 회사 플러그인을 끼우면
Python 이 TypeError 를 내고, 루프는 그것을 "편집을 적용할 수 없었다" 로 처리해 그 시도가 전부 실패한다는 점이다.
선택지: (a) 문서를 코드에 맞춘다(`errors=` 를 선택 인자로 명시) — 가장 싸다; (b) 루프가 applier 의 시그니처를 보고 `errors=` 를 받을 때만
넘긴다(A22 와 같은 방식) — 옛 플러그인도 안전; (c) 둘 다. 추천: (c).
