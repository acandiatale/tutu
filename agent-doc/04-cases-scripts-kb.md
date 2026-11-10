# 케이스, 수정 스크립트, 지식베이스(KB)

> 다루는 것: 오류 서명(L1 / L2)과 케이스 ID, 라운드마다 케이스로 묶고 순서를 정하는 법, 케이스당 사다리(재사용 → 보강 → 신규 → 차단)와 한도, LLM 없는 사전 시도 네 가지, 재빌드 채점, 스크립트 실행 환경과 도우미 API `p`, KB 파일과 `sbmig kb` 명령, 매칭의 알려진 함정, 재생(replay) 픽스처.
> 언제 읽나: "이 오류가 왜 이 케이스로 묶였나 / 왜 이 스크립트가 돌았나 / 왜 차단됐나" 를 볼 때, 스크립트나 KB 를 손볼 때.
> 함께 읽을 파일: [03-run-debug-and-logs.md](03-run-debug-and-logs.md), [05-llm-gateway-and-prompts.md](05-llm-gateway-and-prompts.md), [02-flow-and-nodes.md](02-flow-and-nodes.md), [../docs/ERROR-HANDLING.md](../docs/ERROR-HANDLING.md)

## 1. 한눈에

검증 빌드(`mvn clean package -Dmaven.test.skip=true`)가 실패하면 수정 루프(`sbmig/loop/fixloop.py` 의 `BuildFixLoop`)가 돈다.
라운드 하나 = 빌드 → 직전 시도 채점(나빠졌으면 되돌림, 저장 스크립트에 실적 기록) → 새 오류를 오류 목록에 추가 → 오류를 **케이스**로 묶기 →
케이스마다 사다리 한 단계 → 커밋. 라운드 수는 `loop.phases.verify` 의 `max_iterations`(기본 50). 루프는 언제나 빌드 **뒤에서** 끝난다
(BUILD SUCCESS / 라운드 소진 / 손댈 케이스 없음 / 차단만 남음 / 시간 초과 `loop.max_total_minutes`).

## 2. 서명과 케이스 ID (`sbmig/build/signatures.py`, `sbmig/triage/cluster.py`, `sbmig/errors/common.py`)

로그 파서가 오류를 만들 때마다 `fill_signatures` 가 두 서명을 채운다: `BuildError.signature` = **L1**(구체), `signature_generic` = **L2**(일반).

| 오류 종류 | L2 (케이스 키) | L1 |
|---|---|---|
| 컴파일 `package X does not exist` | `compile|package_does_not_exist|<패키지 전체>` | 같음 |
| 컴파일 `cannot find symbol` | `compile|cannot_find_symbol|<단순 이름>` | 심볼 + 위치 |
| 그 밖의 javac 분류(`_JAVAC_CLASSES`) | `compile|<분류>|<핵심어>` (`_javac_key`: @Override 는 메서드 이름, `illegal_start` 는 javac 문구 …) | 정규화 메시지 + 심볼 + 위치 |
| 애노테이션 프로세서 붕괴 | `compile|annotation_processor|lombok` 등 | |
| 파일·줄·심볼 없는 빌드 단위 메시지 | `compile|build|<앞 6단어>` → 묶기 전에 `refine_signature` 가 원문의 첫 의미 있는 줄로 세분 | |
| 의존성 / parent / 플러그인 해석 실패 | `<kind>|<groupId>:<artifactId>` (버전 없음) | 거기에 버전 |
| 버전 누락 | `pom_invalid|missing_version` | `…|<g:a>` |
| 플러그인 실행 실패 | `plugin_execution|<목표>|<정규화한 원인 10단어>` | |
| 나머지 | `<kind>|<정규화 메시지 앞 6단어>` | 정규화 메시지 전체 |

- **넣지 않는 것**: 절대경로, 줄·열 번호, URL, 16진수, 버전, 숫자, 모듈 이름. 리액터의 artifactId 는 `<module>` 로 가린다
  (`set_reactor_modules`, `on project x` → `on project <module>`). L1 에서는 프로젝트 소유 패키지·타입을 `*` 로 가린다(라이브러리 접두어
  `EXTERNAL_PACKAGE_PREFIXES` + `build.external_package_prefixes` + 사내 groupId 는 남긴다).
- **케이스 ID** = `sha1(L2)[:10]`(`case_id_for`). L2 가 없으면 `sha1(kind|file|L1)[:10]`. 클러스터 id 는 `case:<case_id>`(범위가 있으면 `@scope`).
- **포괄 L2 세분**(`refine_signature`): 둘째 칸이 `build` / `other` 이거나 셋째 칸이 빈 컴파일 L2 는 `<kind>|<분류>|<원문 첫 줄 정규화>` 로 바뀐다
  (아니면 무관한 실패가 한 케이스가 되어 엉뚱한 스크립트가 재사용된다). 같은 값을 오류 목록·KB·채점·클러스터가 모두 쓴다.
- 같은 L2 = 프로젝트가 달라도 같은 케이스 → 같은 저장 스크립트가 LLM 없이 재사용된다. 이것이 500 개 프로젝트를 도는 이유다.

## 3. 라운드 안의 순서와 미루기

- 우선순위(낮을수록 먼저): PARENT_RESOLUTION 10, POM_INVALID 15, DEPENDENCY_RESOLUTION 20, PLUGIN_RESOLUTION 25, ENFORCER 30, REWRITE 35,
  COMPILE 50 + 리액터 순번, TEST_COMPILE 80 + 순번, PLUGIN_EXECUTION 90, TEST_FAILURE 95, UNKNOWN 99.
- 같은 우선순위 안에서는 **근본 원인 먼저**(`CAUSE_RANK`): `package_does_not_exist` 0 → `cannot_access` / `class_not_found_in_package` 1 →
  import 줄의 `cannot_find_symbol@import` / `static_import_only` 2 → `cannot_find_symbol` 3 → 타입 불일치·호출 불일치 4 → override 계열 5 → 나머지 6.
  그다음 오류 수(많은 것 먼저), id.
- **미루기**(`_deferral`, 시도로 세지 않음): 오류 파일이 **전부** 이번 라운드에 이미 손댄 케이스의 파일이면, 또는 이번 라운드의 수정이 그 모듈의
  pom.xml 을 바꾼 컴파일 케이스면 다음 빌드까지 기다린다. 사전 시도도 같은 pom(또는 루트 pom)을 다른 시도가 바꿨으면 기다린다.
- 한 라운드에 다루는 케이스 수 상한 `loop.max_clusters_per_iteration`(8). 아무것도 트리를 바꾸지 않았으면 다시 빌드하지 않고 같은 빌드로
  다음 단계를 묻는다(저널 `reround`).

## 4. 케이스당 사다리 (`sbmig/triage/scripts.py` `plan_script_step`)

한 실행에서 케이스마다, 이 순서:

1. **KB 탐색 → 저장본 재사용**(출처 `kb-script`, LLM 0회): KB 의 best 버전(proven 최신, 없으면 unverified 최신; retired·non_idempotent 제외)을
   그대로 돌린다. 실행당 `script.reuse_versions`(1)개까지. `script.reuse: false` 면 건너뛴다.
2. **LLM 보강**(`augment` → `llm-augment`): 이 실행에서 그 케이스의 마지막 스크립트(실패한 저장본이나 LLM 스크립트)를 LLM 이 늘린 **완전한** 새
   스크립트. 실패한 시도는 되돌린 뒤 묻는다(프롬프트가 "되돌린 트리에서 돈다" 고 말함). 케이스당 `script.max_augments_per_case`(15)번.
3. **LLM 신규**(`new` → `llm-new`): 늘릴 스크립트가 없거나 보강을 다 썼을 때. 답의 `<edits>` 를 바로 적용하고 `<script>` 는 unverified 로 저장.
4. **차단**: 케이스의 LLM 요청(신규 + 보강)이 `script.max_llm_tries_per_case`(20)에 닿으면 `repeated_failure` → Handover.

그 밖의 규칙:

- 한 답의 첫 선택이 안 먹으면(편집 실패·변화 없음, 스크립트 실패) 같은 답의 다른 절반을 적용한다.
- **직접 수정만**(`<no_script>` + `<edits>` → 출처 `llm-edit`): 이 프로젝트 코드에만 맞는 수정(EOL 라이브러리를 프로젝트 코드로 대체 등). 스크립트로
  저장하지 않는다. 케이스당 `script.max_edit_tries_per_case`(4)번, 넘으면 `repeated_failure`.
- **거절**(`<blocked>` 또는 편집·스크립트가 모두 없는 답): `script.max_declines_per_case`(1)번 뒤 차단. 트리아지가 이미 구체 사유
  (`external_javax_api` / `external_api_incompatible` / `artifact_not_found` / `plugin_incompatible`)를 정했으면 그 사유를, 아니면 `needs_owner`.
  같은 원인의 형제 케이스는 LLM 호출 없이 함께 차단(`same_cause_blocked`). 단 거절이 sbmig 가 찾은 저장소 사실(예: jakarta 빌드가 있음)이나
  동봉 jar 가 그 패키지를 제공한다는 사실과 모순이면, 또는 프로젝트 코드로 대체하는 법을 적은 노트(`knowledge/notes` 의 `replacement: true`,
  예: spring-mobile — 오류 글과 오류 파일의 import 줄에 키워드를 맞춘다)가 그 케이스에 맞으면 **거절을 받지 않는다**(`decline_rejected`,
  거절로 세지 않고 사실과 함께 다시 묻는다; 거절 답에 든 edits / script 는 버리지 않고 적용한다). 그런 노트가 맞는 라이브러리는 차단 정책
  (`external_javax_api` / `external_api_incompatible`)으로도 차단하지 않는다 — 직접 수정 한도가 끝을 정한다.
- **차단 정책** `loop.blocked_policy.<사유>`: `classify_blocked`(`sbmig/triage/blocked.py`)가 "프로젝트 밖" 으로 분류한 케이스에 LLM 시도를
  몇 번 줄지(0 = 바로 Handover). 사유 표는 [03](03-run-debug-and-logs.md) §5. 사람이 `block` 으로 고정한 케이스는 시도 없이 차단.
- **정체 / 진동**: 오류 집합이 직전 빌드와 같으면 `stall`, 더 이전 빌드(`loop.oscillation_window` 4 개 안)와 같으면 `oscillation`. 둘 다 정체
  단계를 올리고, 정체 단계가 `loop.stall_limit`(2) 이상이면 더 센 모델을 쓴다. 진동이 `loop.oscillation_limit`(2)번이면 관련 케이스를 `oscillation` 으로 차단.
- **더 센 모델**(`llm.escalation_model`): 정체 단계가 `loop.stall_limit` 이상일 때, 그리고 케이스의 **세 번째** LLM 요청부터(보강이든 신규든).
- 개선되지 않은 시도(`improved` / `build_ok` 가 아님)는 그 케이스의 다음 시도 **전에** 되돌린다(HEAD 면 reset, 아니면 `git revert`). 되돌린 시도의
  결과와 이유는 다음 요청의 "Previous attempts" 절로 간다.

## 5. LLM 없는 사전 시도 (`_pre_tries`, 이 순서로 처음 맞는 것 하나)

트리아지가 SCRIPT 를 정했을 때(또는 LLM 불가·예산 소진으로만 막혔을 때) LLM 보다 먼저 돈다. 사다리 단계로 세지 않고 저장 스크립트도 남기지 않는다.
커밋은 `fix(<출처>): case:<case_id>`, 같은 대상은 실행당 한 번.

| 출처 | 언제 | 하는 일 |
|---|---|---|
| `sbmig-version` | L2 `pom_invalid|missing_version` | 빠진 버전을 `VersionResolver` 순서로 채움: 새 부모 체인이 관리 → 버전 없음; as-is 빌드의 버전; 방금 추가된 것만 저장소의 최신 릴리스 |
| `sbmig-resources` | 리소스 / war 플러그인이 필터링되는 바이너리에서 실패(`MalformedInputException`, `Input length = 1`) | 그 pom 의 maven-resources-plugin `nonFilteredFileExtensions` 에 확장자 추가(`sbmig/restructure/resources_filtering.py`) |
| `sbmig-bundled` | 케이스가 잃은 패키지를 트리 안 동봉 jar 가 제공(`out/<run id>/logs/bundled-jars.json` 의 `provides`) | 저장소에 그 jar 자신의 좌표가 있으면 보통 의존성, 없으면 `<module>/lib/<이름>` 으로 복사 + `system` 스코프 + boot 모듈 `includeSystemScope`(`sbmig/restructure/system_jars.py`); 목표 체인이 관리하는 g:a 의 jar(superseded)는 절대 안 붙임 |
| `sbmig-jakarta` | javax 를 말하는 케이스이고 저장소에서 jakarta 빌드를 **확인**했을 때(`libraries.discover_jakarta_builds`) | 의존성 선언 교체(다른 좌표면 replace, 같은 좌표면 버전·classifier). 같은 좌표의 상위 메이저면 메이저 변경으로 Handover ⚠ |

LLM 이 적용한 시도도 커밋 전에 `_fill_versions` 가 버전 없는 의존성을 같은 순서로 채운다(LLM 은 버전을 모르면 `null` 로 둔다).

## 6. 채점 — 재빌드만이 판정한다

`AttemptOutcome`(`sbmig/core/models.py`): `pending`, `improved`, `partial`, `no_change`, `regressed`, `reverted`, `build_ok`, `apply_failed`,
`blocked`, `inconclusive`.

| 결과 | 조건 | 뒤처리 |
|---|---|---|
| `build_ok` | 빌드 성공 | 유지, KB 성공 |
| `improved` | 대상 오류가 **전부** 사라지고 빌드가 그 오류를 보고하는 단계까지 감("안 보임 ≠ 해결") | 유지, KB 성공 |
| `partial` | 일부만 사라짐, 손댄 파일에 새 오류 없음 | 사라진 항목은 해결 처리, 다음 시도 전에 되돌림(사전 시도의 partial 은 유지), KB 실패도 성공도 아님 |
| `no_change` | 그대로 | 되돌림, KB 실패 |
| `regressed` | 손댄 파일에 새 오류, 오류 수가 `loop.regression_ratio`(1.5)배 초과, 또는 pom 수정 뒤 구조적 오류(`loop.structural_regression_kinds`)·더 이른 단계 오류 | `loop.revert_on_regression` 이면 되돌림 → `reverted` |
| `inconclusive` | 빌드가 말해 주지 못함(타임아웃, 파싱된 오류 없음, 원인과 무관한 이른 단계에서 멈춤) | KB 실적 없음, 시도로는 셈 |
| `apply_failed` | 스크립트·편집이 적용되지 않음 | 커밋 안 된 변경 버림 |

## 7. 스크립트 실행 환경 (`sbmig/script/`)

- **정적 검사**(`check.py` `check_script`): 모듈 수준 `def fix(p)` 하나(위치 인자 정확히 하나); import 는 `re`, `json`, `textwrap`, `fnmatch`,
  `posixpath`, `itertools`, `collections`, `functools`, `string`, `math` 만(`script.extra_allowed_imports` 로 추가 가능, 단 `os`, `sys`,
  `subprocess`, `socket`, `pathlib`, `importlib`, `shutil`, `ctypes`, `threading`, `multiprocessing`, `builtins`, `io` 는 설정으로도 불가);
  내장 `open`, `exec`, `eval`, `compile`, `__import__`, `globals`, `locals`, `vars`, `getattr`, `setattr`, `delattr`, `input`, `breakpoint`,
  `help`, `memoryview` 금지; `_` 로 시작하는 속성 금지. 코드 길이 `script.max_code_chars`(30000).
- **실행**(`runner.py` `ScriptRunner`): 새 임시 폴더(프로젝트 밖)에서 `<python> -I -X utf8 sbmig/script/harness.py <payload>`, 자기 프로세스 그룹,
  `script.timeout_seconds`(60) 넘으면 프로세스 트리째 죽임(Windows `taskkill /T /F`). 자식 환경에서 KEY / TOKEN / SECRET / `LLM_*` 변수는 뺀다.
  상태 `ScriptRunStatus`: `ok` / `error` / `timeout` / `rejected`. 변경은 커밋하지 않고 남긴다(커밋·되돌리기는 루프 몫).
- **멱등성 검사**(`script.idempotency_check`): `ok` 직후 한 번 더 돌려 또 바뀌면 non_idempotent → 재사용 안 함(`script_non_idempotent`).
- **직접 수정**도 같은 길: `sbmig/script/edits.py` 가 `<edits>` 를 리터럴만 든 `def fix(p)` 로 합성해 같은 러너로 돌린다.
- 이것은 실수를 막는 가드이지 악의적 작성자를 막는 샌드박스가 아니다.

도우미 API `p`(`sbmig/script/api.py`, 경로는 모두 프로젝트 기준 상대 경로, 결과는 `/`):

| API | 하는 일 |
|---|---|
| `p.errors` | 이 케이스의 모든 오류 dict 목록(`file`, `line`, `column`, `message`, `symbol`, `location`, `kind`, `module`, `signature`, `signature_generic`) — 프롬프트에 안 보인 것까지 |
| `p.files(glob)` | 맞는 파일의 정렬된 경로(`.git/`, `node_modules/`, Maven 빌드 출력 제외) |
| `p.exists(path)` / `p.read(path)` | 있는지 / 감지한 인코딩으로 읽은 텍스트(줄끝 `\n`) |
| `p.write(path, text)` | 원래 인코딩·줄끝·BOM 을 지켜 씀(새 파일은 UTF-8) |
| `p.replace(path, old, new, count)` / `p.sub(path, pattern, repl, count, flags)` | 평문 / 정규식 치환, 횟수 반환 |
| `p.create(path, text)` / `p.delete(path)` / `p.move(src, dst)` | 새 파일(있으면 오류) / 삭제 / 이동 |
| `p.pom(path)` | pom 편집기: `add_dependency`, `add_managed_dependency`, `remove_dependency`, `remove_managed_dependency`, `set_dependency_version`(None = 버전 삭제), `add_exclusion`, `set_property`, `remove_property`, `get_property`, `set_plugin_version`, `has_dependency`, `dependencies`, `text`, `save` |
| `p.log(msg)` | 로그 한 줄(시도와 함께 KB 에 남음, `script.log_lines` 200 줄까지); `print` 도 로그로 |

못 하는 것: 절대경로·드라이브 문자·폴더를 벗어나는 `..`·`.git/` 아래·`target/` 쓰기·실제 위치가 프로젝트 밖인 심볼릭 링크(→ `PathRefused`, 잡아도 실패로 보고),
네트워크, 다른 프로세스, 다른 코드 실행.

## 8. KB 파일과 `sbmig kb` (`sbmig/errors/cases.py` `CaseStore`, `sbmig/kb/store.py`)

KB 는 파일뿐이다(DB 금지). 위치 `kb.dir`(기본 `kb-v2`). 원본은 `errors.jsonl` 이고 나머지는 파생이다 — 손으로 맞추지 말고 `sbmig kb reindex`.

| 파일 | 내용 |
|---|---|
| `kb-v2/errors.jsonl` | 모든 실행의 오류 항목(실행 끝에 그 실행의 errors.jsonl 을 덧붙임, 이미 있는 id 는 건너뜀) |
| `kb-v2/cases.md` | 사람용 케이스 표: case, name, projects, seen / resolved, scripts(`v1 proven 3/0; v2 unverified 0/1 — 마지막 실패`), pin, last result |
| `kb-v2/cases/<case_id>/case.json` | `CaseInfo`: `case_id`, `l2`, `name`, `kind`, `projects`, `seen`, `resolved`, `scripts[]`, `last_analysis`, `last_solution`, `updated_at` + 모델 밖 키 `pin`(`block[:<reason>]`), `last_result` |
| `kb-v2/cases/<case_id>/fix_v<N>.py` | 저장 스크립트(같은 코드는 한 번만) |
| `kb-v2/cases/<case_id>/errors/<run id>-<entry id>.json` | 오류 한 건씩 읽기 쉬운 JSON(`errors.json_files`); 케이스 없는 항목은 `cases/_other/` |
| `kb-v2/.lock` | 프로세스 간 쓰기 잠금(웹·플릿·`sbmig kb`·동시에 도는 빌드 파드가 공유, `kb.lock_timeout_seconds`). 옆에 잠깐 생기는 `.lock.probe.*`(파일 시스템 시계 재기)와 케이스 폴더의 `.fix.*.tmp` 는 임시 파일이다 |
| `kb-v2/libraries.yml`, `kb-v2/jar_names.yml` | 사람이 더하는 라이브러리 표 / jar 이름 표(`libraries.company_file`, `intake.company_jar_names_file`) |

`scripts[]` 한 칸 = `CaseScript`: `version`, `file`, `status`(`unverified` 저장 시 → `proven` 만든 프로젝트가 **아닌** 곳에서 재사용으로 해결했을 때 → `retired`
사람이 끔), `source`, `created_by`, `project`, `successes`, `failures`, `projects`, `last_result`, `parent`(보강의 원본 버전), `reason`, `last_failure`,
`runs[]`(실행 기록, `errors.script_runs` 30개), `non_idempotent`. 비밀 값이 든 스크립트는 가려서 저장하고 곧바로 retired. 마스킹 규칙 `kb.redact_file`.
예전 판의 `reviews.jsonl`, `fix_attempts.jsonl`, catalog, views, audit, quarantine, `scripts/*/meta.json` 은 지금 코드가 읽지도 쓰지도 않는다.

| 명령 | 사람이 언제 |
|---|---|
| `sbmig kb errors list [--run --case --status --project --limit --json]` | 실행·케이스별 오류 항목 훑기 |
| `sbmig kb errors show <id>` | 항목 하나와 모든 시도(원인 / 해결 / 수정 전·후 / diff) |
| `sbmig kb cases list [--json]` | 케이스 표(널리 퍼진 것 먼저) |
| `sbmig kb cases show <case_id> [--no-code --last N --diff]` | case.json, 스크립트 코드·계보·실행 기록, 최근 항목, `KB 탐색:` 줄 |
| `sbmig kb cases retire <case_id> <N>` | 나쁜 스크립트 버전을 다시 안 쓰게 |
| `sbmig kb cases activate <case_id> <N>` | retire 취소(다른 프로젝트에서 해결한 적 있으면 proven, 아니면 unverified) |
| `sbmig kb cases pin <case_id> block[:REASON] [--create]` / `sbmig kb cases unpin <case_id>` | 케이스를 시도 없이 차단으로 고정(Handover 가 이유를 보여 줌) / 풀기 |
| `sbmig kb stats [--json]`, `sbmig kb validate` | 숫자 / 파일 점검(문제 있으면 exit 1) |
| `sbmig kb export <OUT.zip>` / `sbmig kb import <IN.zip> [--replace]` | 다른 PC 로 옮기기(기본 합치기, `--replace` 는 예전 것을 `.bak-*` 로) |
| `sbmig kb reindex` | errors.jsonl 로 케이스 수치·실행 기록을 다시 셈 |

KB 위치는 그룹 옵션으로 바꾼다: `sbmig kb --kb-dir <dir> cases list`(`--config` 도 같은 자리). 좋은 스크립트를 "고정 사용" 하는 명령은 없다: best 는 proven 최신이므로, 나쁜 버전을 retire 하면 좋은 버전이 선택된다.

## 9. 알려진 매칭 함정

- 케이스 ID 가 L2 의 해시라서 `sbmig/build/signatures.py` 나 `sbmig/triage/cluster.py` 의 규칙을 바꾸면 같은 오류가 새 ID 를 받고, 저장된 케이스는
  **고아**가 된다. `sbmig kb reindex` 는 저장된 `case_id` 로 다시 셀 뿐 L2 를 다시 계산하지 않는다(저장된 `raw` 로 재서명하는 명령은 아직 없다).
- `compile|cannot_find_symbol|<단순 이름>` 은 이름이 같은 다른 문제(같은 메서드 이름)를 한 케이스로 합칠 수 있다.
- 사내 아티팩트처럼 프로젝트마다 이름이 다른 것이 L2 에 들어가면 케이스가 갈라진다 — 리액터 artifactId 를 `<module>` 로 가리는 이유.

## 10. 재생(replay) 픽스처 — LLM 없이 같은 답을 다시

- 설정: `llm.provider: fake` + `llm.base_url: "replay:<dir>"`. 픽스처는 `fixtures/projects/<이름>/sbmig.e2e.yml` 에 이것과 자기 `kb.dir`(`out/e2e/kb-<이름>`)을 둔다.
- `<dir>/llm-*.json` 을 이름 순으로 읽는다(픽스처는 `llm-001.json` …, 기록 파일은 `llm-0001.json` …). 키: `replay_role`(`fix` / `structure_review`),
  `match`(`case: <case_id>`), `response`(답 전문), `note`. 아직 안 쓴 첫 일치 항목을 쓰고, 마지막 일치는 반복, 일치가 없으면 "no fix".
- 실제 실행의 `llm.record_dir` 기록을 복사해 `match` 를 붙이면 재생 입력이 된다. 예: `fixtures/projects/boot2-fix-failures/replay`.
- e2e: `scripts/e2e.sh`(JDK 21, Maven, Central 필요) → `out/e2e/summary.md`.

## 확인하는 법

```
grep -n "def signature_for" -A25 sbmig/build/signatures.py; grep -n "def case_id_for" -A6 sbmig/errors/common.py
grep -n "^CAUSE_RANK\|^BASE_PRIORITY" -A12 sbmig/triage/cluster.py
sed -n 1,40p sbmig/triage/scripts.py                        # 사다리
grep -n "def _pre_tries" -A10 sbmig/loop/fixloop.py          # 사전 시도 순서
grep -n "AttemptOutcome\|TrySource\|ScriptRunStatus\|CaseScriptStatus" sbmig/core/models.py
grep -n "ALLOWED_IMPORTS\|REFUSED_MODULES\|REFUSED_NAMES" -A2 sbmig/script/check.py
grep -n "    def [a-z]" sbmig/script/api.py                  # p 의 메서드
sed -n 1,40p sbmig/errors/cases.py; sed -n 1,20p sbmig/kb/store.py   # KB 배치
grep -n "max_\|reuse\|idempotency" config/default.yml | sed -n 1,60p
.venv/bin/sbmig kb cases --help; .venv/bin/sbmig kb errors --help
.venv/bin/python -m pytest tests/docs/test_agent_doc.py -p no:cacheprovider -q
```
