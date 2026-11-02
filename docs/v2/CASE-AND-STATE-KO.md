# sbmig v2.1 — 케이스에 남는 속성과 노드별 state (사내 설명용)

> 2026-11-02 (v2.1z): 토큰 비교용 스위치(`fix.mode: edits`, `fix.edits_max_asks_per_case`, `--no-kb`, 웹 `fix_mode` / `no_kb`,
> `RunReport.fix_mode`, `SBMIG-RESULT … fix=`)는 없어졌다. 사다리 한도는 케이스당 실행당 **LLM 요청 20회**(`script.max_llm_tries_per_case`,
> 신규 + 보강) 중 **보강 15회**(`script.max_augments_per_case`, 보강만 센다). L1 / L2 에서 reactor 모듈 artifactId 는 전부 `<module>`.
> 계약은 `docs/INTERFACES.md` "v2.1z".

> 2026-11-02, 브랜치 `flow-v2`(`3f0a250`, v2.1u…y 포함). `docs/v2/KB-AND-FLOW.md` 가 "흐름과 사다리" 를 설명한다면 이 문서는
> **속성 사전**이다. A부: 케이스 하나에 무엇이 어디에 남는가(실행 메모리 → 실행 디렉터리 → KB → 다음 프로젝트). B부: LangGraph
> 노드가 주고받는 state 와 노드마다 오케스트레이터·보고서·파일에 남기는 것. 샘플은 전부 로컬 e2e 실물이다
> (`out/e2e/run-20261031-193430-30da3d` = `boot2-javax-lib` → BLOCKED, `out/e2e/kb-jaxb` = `pure-java` → `java6-legacy` 재사용).
> 모델 정의는 `sbmig/core/models.py`, 그래프는 `sbmig/graph/engine.py`, KB 케이스 저장소는 `sbmig/errors/cases.py`, 오류 목록은
> `sbmig/errors/entries.py`. 필드 이름은 JSON 에 보이는 그대로 적었다.

## 0. 한 장 요약

- **케이스 = L2 시그니처 하나**, `case_id = sha1(L2)[:10]`. 한 빌드에서 같은 L2 를 가진 오류(파일이 여럿이어도)가 한 케이스로 묶여 한 번에 고쳐진다.
- 케이스 기록은 **네 겹**이다.
  1. 실행 중 메모리: `ErrorCluster`(case_id) · `FixAttempt`(루프의 시도) · `BlockedItem`(차단) · 루프 내부 상태(시도 수·거절·되돌림). 실행이 끝나면 `FixAttempt` 와 `BlockedItem` 만 `report.json` 에 남는다.
  2. 실행 디렉터리: `errors.jsonl` / `errors.md`(오류 1건 = `ErrorEntry`, 그 안의 시도 = `FixTry`), `report.json`(`phases[].attempts[]`, `blocked[]`, `structure_review`), `journal.jsonl`.
  3. KB(`kb-v2/`): `cases/<case_id>/case.json`(`CaseInfo` + `scripts[]`=`CaseScript` + `runs[]`=`ScriptRun` + 추가 키 `pin` / `last_result`), `fix_v<N>.py`, `errors/<run_id>-<id>.json`, `errors.jsonl`, `cases.md`.
  4. 다음 프로젝트로 가는 것: `KBMatch`(저장본·힌트·핀) → `ScriptStep`(재사용/보강/신규) → `FixContext`(LLM 프롬프트 재료).
- **그래프 state 는 작은 JSON 8개 키뿐**(`run_id`, `stop_after`, `dry_run`, `phases`, `phase`, `aborted`, `stopped`, `summary`). 프로필·계획·보고서·수정 루프 같은 무거운 것은 전부 오케스트레이터(`Orchestrator`)에 있고, 노드는 `step_*` 메서드 하나를 부를 뿐이다. 그래서 `loop.engine: graph` 와 `inline` 의 보고서가 같다.

---

## A. 케이스에 남는 속성

### A.1 케이스 키 — L1 / L2 / case_id

빌드 오류 한 건마다 로그 파서가 두 시그니처를 붙인다(`sbmig/build/signatures.py`, `BuildError.signature` / `signature_generic`).

| 이름 | 꼴 | 예 | 쓰임 |
|---|---|---|---|
| **L1** (구체) | `<kind>\|<class>\|<symbol>\|<location>` | `compile\|cannot_access\|cannot access javax.servlet.http.HttpServletRequest` | 같은 문제의 같은 오류. 프로젝트 소유 패키지·타입은 `*` 로 가린다. 차단 등록부의 키(L1, 파일) |
| **L2** (일반) | `<kind>\|<class>\|<key>` | `compile\|cannot_access\|HttpServletRequest`, `compile\|package_does_not_exist\|jakarta.xml.bind`, `plugin_execution\|resources:resources\|malformedinputexception …` | **케이스의 정의.** 프로젝트가 달라도 같은 종류의 문제 |
| **case_id** | `sha1(L2)[:10]` | `f2b133edd9` | KB 폴더 이름, `ErrorEntry.case_id`, `ErrorCluster.case_id`(클러스터 id 는 `case:<case_id>`), 로그 단계 `fix r3 f2b133edd9` |

- `<kind>` = `ErrorKind`: `compile`, `test_compile`, `dependency_resolution`, `plugin_resolution`, `parent_resolution`, `plugin_execution`, `enforcer`, `pom_invalid`, `rewrite`, `test_failure`, `unknown`.
- `<class>` 는 javac 메시지 첫 줄의 분류(`package_does_not_exist`, `cannot_find_symbol`, `cannot_access`, `incompatible_types`, `method_does_not_override`, `illegal_start` …); `plugin_execution` 은 `플러그인:목표` + 정규화한 원인 10단어.
- 규칙: **절대경로·줄 번호·모듈 이름·프로젝트 소유 위치는 넣지 않는다.** v2.1z: 오류 자신의 모듈뿐 아니라 이번 빌드 로그에 나온 reactor artifactId
  전부(pom 트리의 artifactId + `< g:a >` 머리줄, `--- … @ <artifactId> ---`, `on project <a>`)를 `<module>` 로 가린다. 오류 자신의 모듈은 이름이
  무엇이든 언제나 가리고, 다른 모듈 이름은 artifactId 처럼 생긴 것(하이픈·밑줄·점·숫자·대문자 중 하나라도 있는 `pricing-core`, `app_web`, `svc2`,
  `DEV`)만 — `user` / `config` / `model` 같은 소문자 한 단어와 `web` / `core` / `common` 같은 평범한 낱말(stoplist), 3자 미만은 메시지의 보통 단어를
  먹기 때문에 가리지 않는다(대소문자 구분, 단어 경계, 긴 이름부터). 예: `enforcer|requirejavaversion: pricing-core must be built with` → `enforcer|requirejavaversion: <module> must be built with`. 뭉뚱그려진 컴파일 키(`build` / `other`)는 묶기 전에 `sbmig.triage.cluster.refine_signature` 가 오류 원문 줄로 세분한다.
- 왜 해시인가: L2 에 `|` `<` `>` `:` 한글·공백이 들어가고, 매칭은 L2 문자열로 하므로 폴더 이름은 키일 뿐이다(KB-AND-FLOW §2). **`signatures.py` 를 바꾸면 기존 KB 케이스와 매칭이 끊긴다.**
- 케이스가 없는 레코드도 있다: 재구성의 `pom-verify`, 구조 점검 warning 메모 → `case_id` / `l2` 가 `""` 이고 KB 에서는 `cases/_other/errors/` 에 들어간다. 구조 점검이 **직접 고친** critical 은 고정 L2 `review|structure`(`sbmig/loop/entries.py REVIEW_L2`)의 케이스다.

### A.2 어디에 무엇이 남나 (지도)

| 장소 | 파일 · 객체 | 모델 | 수명 | 보는 법 |
|---|---|---|---|---|
| 실행 메모리(수정 루프) | 클러스터 `case:<id>`, 시도, 차단 등록부, 라운드 상태 | `ErrorCluster`, `FixAttempt`, `BlockedRegistry`/`BlockedItem`, `_Round` | 실행 동안 | `sbmig.log` `[fix r3 <case>]` 줄, `journal.jsonl` `triage` / `decision` / `attempt` / `blocked` |
| 실행 디렉터리 `out/<run>/` | `errors.jsonl`, `errors.md` | `ErrorEntry` → `tries[]` = `FixTry` → `edits[]` = `EditNote` → `hunks[]` = `EditHunk` | 영구(실행 폴더) | `errors.md`, `report.md` "Error list" |
| 실행 디렉터리 | `report.json`, `report.md` | `RunReport.phases[].attempts[]` = `FixAttempt`, `RunReport.blocked[]` = `BlockedItem`, `RunReport.error_entries[]`, `RunReport.structure_review` | 영구 | `report.md` "Fixes by case" / "Blocked" / "Structure review" |
| KB `kb-v2/` | `cases/<case_id>/case.json` | `CaseInfo` + `scripts[]` = `CaseScript` + `runs[]` = `ScriptRun` + 추가 키 | 500개 프로젝트 공유 | `sbmig kb cases show <id>`, `cases.md` |
| KB | `cases/<case_id>/fix_v<N>.py` | 스크립트 원문(`def fix(p)`) | 영구(번호 재사용 없음) | `sbmig kb scripts list` |
| KB | `cases/<case_id>/errors/<run_id>-<id>.json`, `errors.jsonl` | `ErrorEntry`(실행 디렉터리 것과 같은 레코드) | 영구(원본은 `errors.jsonl`) | `sbmig kb errors show <id>` |
| 다음 프로젝트 | `KBMatch` → `ScriptStep` → `FixContext` | §A.12 | 라운드마다 새로 만든다 | 로그 `KB 탐색: …` 줄, `llm.record_dir` 의 프롬프트 기록 |

### A.3 KB `case.json` — `CaseInfo`

실물(`out/e2e/kb-boot2-javax-lib/cases/f2b133edd9/case.json`, 스크립트 없이 차단된 케이스):

```json
{
  "case_id": "f2b133edd9",
  "l2": "compile|cannot_access|HttpServletRequest",
  "name": "cannot access javax.servlet.http.HttpServletRequest",
  "kind": "compile",
  "projects": ["member-api"],
  "seen": 1,
  "resolved": 0,
  "scripts": [],
  "last_analysis": "LegacyRequestHelper (com.company.legacy:legacy-web-lib:1.0.0) takes javax.servlet.http.HttpServletRequest; … No script.",
  "last_solution": "",
  "updated_at": "2026-10-31T10:35:07.396859Z",
  "last_result": "blocked 1 — member-api run-20261031-193430-30da3d"
}
```

| 속성 | 뜻 | 누가 언제 쓰나 |
|---|---|---|
| `case_id` | `sha1(l2)[:10]`, 폴더 이름과 같아야 한다(다르면 `kb validate` 가 알려 주고 읽을 때 건너뜀) | 첫 저장 |
| `l2` | 일반 시그니처(매칭 키) | 첫 저장; 비어 있으면 뒤에 채움 |
| `name` | 사람용 이름: 오류 메시지 첫 줄, 메시지에 L2 의 핵심어가 없으면 `메시지: 핵심어` | 첫 저장 |
| `kind` | `ErrorKind` | 첫 저장 |
| `projects[]` | 이 케이스가 나온 프로젝트들(중복 없음) | `record_run`(실행 끝) |
| `seen` / `resolved` | 모든 실행에 걸친 오류 건수 누적 / 그중 `resolved` 로 끝난 건수. `note` / `pom-verify` 레코드는 세지 않는다 | `record_run` |
| `scripts[]` | 저장된 스크립트 버전들(§A.4) | `add_script`, `credit`, `mark_non_idempotent`, `retire` / `activate` |
| `last_analysis` / `last_solution` | 마지막 LLM 답변의 **원인** / **해결 방법**(마스킹, 4,000자). 재사용 실행이면 "stored fix script … (reused, no LLM call)" | `add_script`, `record_run`(그 실행의 가장 늦은 시도 중 글이 있는 것) |
| `updated_at` | 마지막 변경 시각(UTC) | 모든 쓰기 |
| `last_result` (추가 키) | `"<status n, …> (<무엇이 해결>) — <project> <run_id>"` 예 `resolved 1 (r2 try 1) — ledger-utils run-…` | `record_run` |
| `pin` (추가 키, 사람) | `block` / `block:<이유>` — 이 케이스는 스크립트도 LLM 도 시도하지 않고 Handover 로 간다(`sbmig kb cases pin`). 다른 값은 무시되고 `kb validate` 가 알려 준다 | 사람 |
| 그 밖의 키 | 사람이 메모장으로 넣은 키는 **보존**된다(`CaseInfo` 에 없는 키는 따로 들고 있다가 저장 때 되돌려 넣음; BOM 붙은 파일도 읽는다) | 사람 |

원본과 파생: `errors.jsonl` 과 `fix_v*.py` 가 원본이고 `case.json` 의 통계·`cases.md`·에러별 JSON 은 파생이다. 손으로 맞추지 말고 `sbmig kb reindex`(`recount`)로 다시 만든다(§A.13).

### A.4 `scripts[]` — `CaseScript`

실물(`out/e2e/kb-jaxb/cases/7a52b1cea8/case.json` 의 한 버전):

```json
{ "version": 1, "file": "fix_v1.py", "status": "proven", "source": "llm-new",
  "created_by": "run-20261028-003026-835d61", "project": "inventory-core",
  "successes": 2, "failures": 0, "projects": ["inventory-core", "ledger-utils"],
  "last_result": "improved", "created_at": "2026-10-27T15:30:49Z",
  "parent": null, "reason": "", "last_failure": "", "non_idempotent": false,
  "runs": [ { "run_id": "run-…-835d61", "project": "inventory-core", "round": 1, "n": 1, "result": "build_ok", "reason": "",
              "errors_before": 23, "errors_left": 0, "errors_new": 0, "files": ["module/comp-jar/pom.xml"], "at": "…" } ] }
```

| 속성 | 뜻 |
|---|---|
| `version` / `file` | 1, 2, … 케이스 안에서 증가. `file` = `fix_v<N>.py`. 번호 = 1 + max(case.json 의 버전, 폴더의 `fix_v*.py`) — **번호는 재사용하지 않는다**(목록에 없는 파일을 덮어쓰지 않으려고). 같은 코드(sha1)는 한 번만 저장되고 기존 버전을 돌려준다 |
| `status` | `unverified`(저장 직후: 만든 프로젝트에서만 통함) → `proven`(**다른** 프로젝트에서 케이스를 해결함) → `retired`(사람이 끔, `sbmig kb cases retire`; `activate` 로 되돌림). 비밀값이 든 코드는 마스킹해 저장하고 **바로 retired**(마스킹본은 실제로 돈 코드가 아님) |
| `source` | `llm-new` 처음 작성 · `llm-augment` 보강본 · `review` 구조 점검이 남긴 것 (`TrySource`) |
| `created_by` / `project` | 만든 실행 id / 프로젝트 |
| `successes` / `failures` | 채점 누적: `improved` / `build_ok` → 성공 +1, `no_change` / `regressed` / `reverted` / `apply_failed` / `blocked` → 실패 +1, `partial` / `inconclusive` 는 `last_result` 만 |
| `projects[]` | 이 버전이 케이스를 해결한 프로젝트들 |
| `last_result` | 마지막 채점 결과(`AttemptOutcome`) |
| `parent` / `reason` | 보강본이면 이은 버전 번호 / **왜 만들었는지**(부모의 실패 문장, `v1: no_change: 3 of 3 targeted error(s) remained — …`, 600자) |
| `last_failure` | 가장 최근 실패 이유 한 줄(빠른 보기; 전부는 `runs[]`). `non_idempotent` 표시 때도 그 사유가 여기 들어간다 |
| `runs[]` | 실행 이력(§A.5) |
| `non_idempotent` | v2.1y: `ok` 직후 같은 스크립트를 한 번 더 돌렸더니 트리를 또 바꿈(`script.idempotency_check`). 기록은 남지만 `best` 가 다시 고르지 않는다(보강의 바탕 `latest` 로는 남는다) |

선택 규칙(`CaseStore`): **`best`** = 가장 새 `proven`, 없으면 가장 새 `unverified`; `retired`·`non_idempotent`·파일 없는 버전은 제외 → 재사용(`kb-script`)에 쓰인다. **`latest`** = retired 가 아닌 가장 새 버전 → 보강 요청의 바탕.

사다리(v2.1z, 케이스당 실행당): 저장본 재사용(`script.reuse_versions` 1, LLM 0회) → 이 실행에서 코드가 있었던 마지막 시도를 **보강**(보강이
`script.max_augments_per_case` **15** 회 남아 있는 동안; 재사용이 꺼져 있으면 저장본 `best`) → 보강을 다 쓰면 처음부터 **새로 작성**(`new`) →
LLM 요청(신규 + 보강)이 `script.max_llm_tries_per_case` **20** 회에 닿으면 `repeated_failure` 차단. 두 값 모두 그대로의 상한(0 = 보강 안 함 /
LLM 에 묻지 않음). v2.1z 전에는 `max_augments_per_case`(3) 하나가 신규 + 보강을 함께 셌다.

### A.5 `scripts[].runs[]` — `ScriptRun`

| 속성 | 뜻 |
|---|---|
| `run_id` / `project` | 어느 실행, 어느 프로젝트에서 돌았나 |
| `round` / `n` | 몇 라운드의 케이스 몇 번째 시도(`FixTry.round` / `n` 과 같은 값) |
| `result` | 채점 결과(`AttemptOutcome`) |
| `reason` | 못 푼 이유 한 줄(`improved` / `build_ok` 면 `""`): 루프의 머리말(`no_change: 3 of 3 targeted error(s) remained — …`) + 시도 note 중 '실패 이유' 부분(재사용 배너·에스컬레이션 요청·병합 힌트는 뺌) |
| `errors_before` / `errors_left` / `errors_new` | 케이스 오류 수: 실행 전 / 재빌드 뒤 남은 수 / 손댄 파일에 새로 생긴 수 |
| `files[]` | 바꾼 파일(50개까지) |
| `at` | 기록 시각 |

같은 `(run_id, round, n)` 기록은 **교체**된다(판정 불가 시도가 되돌림 재빌드 뒤 다시 채점되므로). 버전마다 `errors.script_runs`(30)개만 남긴다(최신이 뒤).

### A.6 오류 레코드 — `ErrorEntry`

실행 디렉터리 `errors.jsonl` 의 한 줄 = KB `errors.jsonl` 의 한 줄 = `cases/<case_id>/errors/<run_id>-<id>.json` 하나. 실물(차단된 케이스, `tries` 는 §A.7):

```json
{ "id": "2a9a9bab8562", "run_id": "run-20261031-193430-30da3d", "project": "member-api",
  "case_id": "f2b133edd9", "l2": "compile|cannot_access|HttpServletRequest", "kind": "compile",
  "file": "module/comp-jar/src/main/java/com/example/members/web/ClientIpFilter.java", "line": 24, "module": "member-api-comp",
  "message": "cannot access javax.servlet.http.HttpServletRequest",
  "raw": "[ERROR] module/comp-jar/src/main/java/…/ClientIpFilter.java:[24,70] cannot access javax.servlet.http.HttpServletRequest\n  class file for … not found",
  "seen": "build", "status": "blocked",
  "tries": [ { "n": 1, "round": 1, "source": "llm-new", "result": "blocked", "llm_calls": 1, "…": "…" } ],
  "resolved": "", "resolved_by": [], "script": "",
  "note": "blocked: external_javax_api: `com.company.legacy:legacy-web-lib:1.0.0` (class `…LegacyRequestHelper`) still takes `javax.servlet.http.HttpServletRequest`; upgrade … or wrap the call with a shim; left unchanged",
  "at": "2026-10-31T10:34:57.396551Z" }
```

| 속성 | 뜻 |
|---|---|
| `id` | `sha1(run_id \| case_id \| file \| line \| message)[:12]` — 에러별 JSON 파일 이름의 뒷부분 |
| `run_id` / `project` | 어느 실행, 어느 프로젝트 |
| `case_id` / `l2` / `kind` | 케이스 키 · 일반 시그니처 · 종류. 케이스 없는 레코드는 `""` / `unknown` |
| `file` / `line` / `module` / `message` / `raw` | 위치(프로젝트 상대 경로, 재구성 뒤 경로로 따라감), Maven 메시지 첫 줄, 로그 원문(`errors.raw_chars` 600자) |
| `seen` | 처음 나온 단계: `compile`(`plan.compile_after_rewrite: true` 일 때만) · `restructure`(pom-verify) · `review` · `build`(첫 `clean package`) · `fix rN`(N라운드 빌드에서 새로 나옴) |
| `status` | `open`(마지막 빌드에 아직 있음) → `resolved` / `blocked` / `unresolved`(끝까지 남음; `end_of_run` 이 open 을 바꿈); `pom-verify`(재구성이 고친 pom, 다음 빌드가 검증) · `note`(점검 warning·정보, 고치지 않음) |
| `tries[]` | 이 오류를 품은 케이스에 대한 시도들(§A.7). **한 시도는 케이스의 모든 오류 레코드에 똑같이 기록된다** |
| `resolved` | 무엇이 해결했나: `r3 try 1`(3라운드 1번째 시도) · `build`(시도 없이 다른 수정으로 사라짐) · `review` |
| `resolved_by[]` | `build` 로 닫힌 오류를 **누가 사라지게 했는지** 추정(`ResolvedBy`): 같은 라운드에 이 파일(또는 위쪽 pom)을 바꾼 시도 — `case_id` · `round` · `n` · `commit` · `files` · `match`(`file` / `pom` / `reindex`) · `text` 한 줄 |
| `script` | 해결한 저장 스크립트 경로 `cases/<case_id>/fix_v2.py` |
| `note` | pom-verify: 재구성이 한 일 한 줄(`parent … → …`, `scope restored: …`) · 차단: `blocked: <reason>: <suggestion>` · note 레코드: 본문 · 다시 나타나면 `reappeared in …` |
| `at` | 레코드 시각 |

판정 규칙(round 10): **빌드에 안 보인다고 해결이 아니다.** 열린 오류가 실패한 빌드에 없을 때, 그 빌드가 그 오류를 보일 수 있었을 때만(모듈이 빌드됐거나, 오류의 단계 pom → resolution → enforcer → compile → test-compile 까지 갔을 때) `resolved` 가 된다; 타임아웃·오류를 못 읽은(`void`) 빌드는 아무것도 해결하지 않는다. 다시 나온 오류는 같은 `id` 로 다시 `open`.

### A.7 시도 — `FixTry` (`ErrorEntry.tries[]`)

| 속성 | 뜻 |
|---|---|
| `n` / `round` | 케이스 안의 시도 번호(1, 2, … 실행마다) / 수정 라운드(0 = 구조 점검) |
| `source` | 사다리 단계(`TrySource`): `kb-script` 저장본 실행(LLM 0회) · `llm-new` 새 답변 · `llm-augment` 보강 답변 · `llm-edit` 스크립트 없이 직접 수정 · `review` 구조 점검 · `sbmig-version` 누락 버전을 sbmig 가 채움(LLM 0회) · `sbmig-resources` `nonFilteredFileExtensions` 를 sbmig 가 적음(LLM 0회) |
| `script_version` | 돌린/만든 저장 버전(0 = 없음) |
| `analysis` / `solution` / `no_script_reason` | LLM 의 **원인** / **해결 방법** / 스크립트로 못 만드는 **이유** |
| `applied` | 실제로 적용된 것: `script` / `edits` / `none` |
| `files[]` / `commit` | 바뀐 파일, git 커밋(`fix(script:<case>:v<N> <mode>)` / `fix(edits:<case> <source>)`) |
| `edits[]` | **수정 기록**(`EditNote`): `path` · `where`(LLM 이 쓴 위치) · `change`(무엇을 어떻게, 한국어) · `lines`(`~12` 바뀜, `+42–47` 추가, `-3` 삭제) · `source`(`llm` / `diff`: LLM 이 설명 안 한 변경은 diff 에서 만든 메모) · `hunks[]` |
| `edits[].hunks[]` | **as-is / to-be**(`EditHunk`): `lines` · `before` · `after` · `truncated`(`errors.snippet_lines` 0 = 자르지 않음) |
| `major_upgrades[]` | 이 시도가 의존성 **메이저를 올리거나 라이브러리를 바꿨으면** 그 목록(`DependencyChange`: `path` · `key` · `before` · `after` · `kind` major/minor/added/removed/replaced · `replaced_by` · `section` · `approved`(libraries.yml) · `note`) |
| `verify` / `verify_source` | 운영자 확인 가이드(LLM `<verify>` = `llm`, sbmig 생성 = `sbmig`, 없음 = `""`) → 보고서 "운영자 확인 필요", `SBMIG-RESULT major=` |
| `diff` | 적용된 unified diff(`errors.diff_chars` 6,000자; 전체는 `changes.patch`) |
| `result` | 재빌드 채점(`AttemptOutcome`): `improved`(케이스 오류 전부 사라짐) · `build_ok` · `partial`(일부 사라짐, 새 오류 없음) · `no_change` · `regressed`(새 오류 → 되돌려 `reverted`) · `inconclusive`(빌드가 판단 못 함) · `apply_failed` · `blocked` · `pending`(아직 빌드 전) |
| `note` | 실패·되돌림·거절 설명(`declined: …`, `reused stored v1 …`) |
| `llm_calls` / `seconds` | 이 시도의 LLM 호출 수와 소요 시간(점검이 한 번에 고친 critical 여러 건은 첫 항목에만 호출 수) |

결과가 가져오는 일: `improved` / `build_ok` → 케이스의 열린 레코드 `resolved`, 저장 버전에 성공 크레딧, 커밋 유지. `partial` → 유지하되 다음 시도 전 되돌리지 않음(개선으로 침). 나머지 → **그 케이스의 다음 시도 전에 되돌린다**, 저장 버전에 실패 크레딧(`inconclusive` 는 크레딧 없음).

### A.8 차단 — `BlockedItem`

차단은 "이 프로젝트 안에서는 못 고침" 이지 실행 실패가 아니다(회사 정책: block 으로 실행을 멈추지 않는다). 루프의 `BlockedRegistry` 에 실행 전체로 모이고(키 = L1 시그니처 + 현재 파일 경로, 재구성이 옮긴 파일을 따라감), 끝에 `report.blocked[]` 가 된다. 실물(`report.json`):

```json
{ "id": "blk-12b207af03", "reason": "external_javax_api", "phase": "verify", "cluster_id": "case:f2b133edd9",
  "signatures": ["compile|cannot_access|cannot access javax.servlet.http.HttpServletRequest"],
  "files": ["module/comp-jar/src/main/java/com/example/members/web/ClientIpFilter.java:24"],
  "errors": [ { "kind": "compile", "message": "…", "file": "…", "line": 24, "signature": "…", "signature_generic": "compile|cannot_access|HttpServletRequest" } ],
  "artifact": { "group_id": "com.company.legacy", "artifact_id": "legacy-web-lib", "version": "1.0.0" },
  "owner_class": "com.company.legacy.web.LegacyRequestHelper",
  "required_type": "javax.servlet.http.HttpServletRequest", "found_type": "jakarta.servlet.http.HttpServletRequest",
  "suggestion": "`com.company.legacy:legacy-web-lib:1.0.0` (class `…LegacyRequestHelper`) still takes `javax.servlet.http.HttpServletRequest`; upgrade … to a Jakarta build (add it to artifacts.optional with that version) or wrap the call with a shim",
  "attempts": 1, "first_seen_iteration": 1, "notes": ["blocked: external_javax_api — a dependency API requires javax.servlet.http.HttpServletRequest …"] }
```

| 속성 | 뜻 |
|---|---|
| `id` | `blk-<해시>` — 같은 사유의 같은 오류는 단계가 바뀌어도 같은 id |
| `reason` | `BlockReason`(아래 표) |
| `phase` / `cluster_id` | 차단된 단계 / 클러스터(`case:<case_id>`) |
| `signatures[]` / `files[]` / `errors[]` | L1 시그니처들, `파일:줄`, 오류(마스킹, 10건 정도) |
| `artifact` / `owner_class` / `required_type` / `found_type` / `via[]` / `via_direct` | 원인 라이브러리(classpath 에서 찾음), 그 안의 클래스, 요구 타입 / 실제 타입, 의존 경로 / 직접 의존성 |
| `suggestion` | 사람이 할 일 한 줄(보고서 "Blocked" 와 Handover) |
| `attempts` / `first_seen_iteration` / `notes[]` | 차단 전 시도 수, 처음 본 라운드, 메모 |

`BlockReason` 과 LLM 시도 허용 수(`loop.blocked_policy`, `config/default.yml`; 저장본 재사용은 정책과 무관하게 먼저 돈다):

| reason | 뜻 | 허용 LLM 시도 |
|---|---|---|
| `external_javax_api` | 의존성이 아직 `javax.*` API 를 노출 | 2 |
| `external_api_incompatible` | 의존성 API 가 바뀜/없어짐(javax 아님) | 2 |
| `artifact_not_found` | 저장소에 없는 의존성/플러그인 | 2 |
| `plugin_incompatible` | JDK 21 에서 깨지는 빌드 플러그인 | 2 |
| `parent_not_found` | 사내 parent 를 못 받음(settings / Nexus) | 0 |
| `unsupported_source` / `encoding` | kotlin·groovy 등 / 인코딩 판별 불가 | 0 |
| `reactor_duplicate` | reactor 에 두 번 닿는 모듈(접수가 보통 지움) | 0 |
| `file_lock` | Windows `target/` 파일 잠금(환경 오류) | 0 |
| `needs_owner` | v2.1y: LLM 이 `<blocked>` 로 "프로젝트 밖의 일" 이라 답함 → 소유팀 결정 | 0 (거절 `script.max_declines_per_case` 1회 뒤) |
| `repeated_failure` | 저장본 1 + LLM `script.max_llm_tries_per_case`(20)회(그중 보강 ≤ `script.max_augments_per_case` 15)를 다 씀 | — |
| `budget_exhausted` / `llm_unavailable` / `oscillation` | LLM 한도 소진 / LLM 장애 / 수정이 같은 오류 집합 사이를 오감 | — |

거절(`<blocked>why</blocked>`, 또는 수정도 스크립트도 없이 원인만 쓴 답)은 `FixTry.result = blocked`, `note = declined: …` 로 남고, 트리아지가 이미 분류한 사유(`external_javax_api` 등)가 있으면 그 사유로, 없으면 `needs_owner` 로 차단된다. 같은 원인의 형제 케이스는 LLM 호출 없이 "same cause as <case>" 로 함께 차단된다(저널 `same_cause_blocked`). 운영자 줄에는 `attn=blocked:<reason>`(needs_owner 는 `owner-decision`)로 보인다.

### A.9 루프의 기록 — `FixAttempt` (`report.json` → `phases[].attempts[]`)

같은 시도를 **루프 쪽에서** 적은 객체. 오류 목록의 `FixTry` 와 1:1 이지만 루프 내부 값(결정, 커밋 앞뒤, 프롬프트 해시, 스크립트 원문)까지 든다.

| 속성 | 뜻 |
|---|---|
| `id` | `<phase>-<iteration>-<n>` 예 `verify-1-1`(저널 `attempt` 이벤트의 `attempt`) |
| `phase` / `iteration` / `cluster_id` | 단계 / 라운드 / `case:<case_id>` |
| `decision` | 트리아지 결정 `TriageDecision`: `kind`(`script` / `blocked` / `skip` / `escalate`) + `rationale`(`new fix for case … (nothing stored); looks like external_javax_api …`) |
| `source` / `script_mode` | `TrySource` / `new` · `reuse` · `augment` (v2.1z: 한도 계산은 `script_mode` 로 — `new` + `augment` = LLM 요청 수, `augment` = 보강 수). 시도 이름은 언제나 `script:<case>:v<N>`(v2.1i 의 `edits:<case>#<n>` 는 없어짐); 스크립트 없는 답은 `source = llm-edit` |
| `script_case_id` / `script_version` / `script_parent` / `script_code` / `script_result` | 케이스, 돌린·만든 버전(0 = 저장 안 됨), 보강의 부모, 스크립트 원문, 실행 결과(`ScriptRunResult`: `status` ok/error/timeout/rejected, `changed_files`, `log`, `error`, `problems`, `duration_s`) |
| `edits_applied` | 직접 적용한 편집 수(0 = 스크립트가 돌았음) |
| `analysis` / `solution` / `no_script_reason` / `verify` / `verify_source` / `major_upgrades[]` | §A.7 과 같은 값 |
| `applied` / `apply_errors[]` / `base_commit` / `commit` | 적용 여부, 적용 오류, 시도 전 HEAD / 시도 커밋(`git diff base_commit..commit` = 이 시도의 변경) |
| `errors_before` / `errors_after` / `introduced_errors[]` / `cluster_resolved` | 채점 재료: 케이스 오류 수 전/후, 손댄 파일의 새 오류, 케이스 시그니처가 사라졌는가 |
| `outcome` / `notes[]` | `AttemptOutcome` / 메모(되돌림 등) |
| `model` / `prompt_hash` / `llm_usage` / `advisory_ids[]` | LLM 모델, 프롬프트 해시(같은 400 오류 반복 감지), 토큰, 프롬프트에 넣은 권고 id |
| `cluster_signatures[]` / `cluster_errors[]` | 클러스터의 L2 들, 오류(마스킹) |

### A.10 세 기록의 대응

| 같은 시도를 | `FixAttempt`(루프, `report.json`) | `FixTry`(오류 목록, `errors.jsonl`) | `ScriptRun`(KB 스크립트 버전, `case.json`) |
|---|---|---|---|
| 찾는 키 | `id` = `verify-1-1`, `cluster_id` = `case:<id>` | `ErrorEntry.case_id` + `tries[].round` + `n` | `runs[].run_id` + `round` + `n` |
| 공통 값 | `commit`, `outcome` = `result`, `source`, `script_version`, `analysis` / `solution` | ← | `result`, `files` |
| 그것만 가진 것 | `decision`, `base_commit`, `prompt_hash`, `script_code`, `errors_before/after` | `edits[]`(수정 기록 + as-is/to-be), `diff`, `resolved_by`, `llm_calls`, `seconds` | `reason`(실패 문장), `errors_before/left/new`, 버전의 누적 성공/실패 |
| 수명 | 실행 하나 | 실행 하나 → KB 에 복사 | KB(버전마다 30건) |

### A.11 케이스 한 건의 수명 (한 실행 안에서, 누가 어디에 적나)

1. `mvn clean package` 실패 → 로그 파서가 `BuildError` 마다 L1/L2 를 붙인다(`fill_signatures`).
2. 오류 목록 `add_build(build, "build")`: 새 오류만 `ErrorEntry`(status `open`, `seen` = `build` 또는 `fix rN`)로; 이미 있는 것은 같은 `id`(줄이 밀렸거나 재구성으로 경로가 바뀌어도 매칭). `pom-verify` 중 그 pom 이 오류를 안 내면 `resolved`(`verify_poms`).
3. 트리아지가 L2 로 묶는다 → `ErrorCluster`(id `case:<case_id>`, 근본 원인 먼저; 이번 라운드에 이미 손댄 파일·pom 의 케이스는 다음 빌드까지 대기). 저널 `triage`.
4. KB 조회 `kb.match(cluster)` → `KBMatch`(사람 핀이면 바로 차단; 저장본 `best` 가 있으면 `CaseHit(action=script, version)`; `last_solution` 힌트). 사다리 `plan_script_step` → `ScriptStep`(`reuse` / `augment` / `new`, `kb_line` 로그). 저널 `decision`.
5. 시도 시작: `FixAttempt` 생성 + 오류 목록 `begin_try(case, round, source, version)` → 케이스의 모든 열린 레코드에 `FixTry n`. sbmig 가 LLM 없이 먼저 해 보는 케이스(`sbmig-version`, `sbmig-resources`)도 같은 길.
6. 적용: 저장본 실행 / LLM 답변의 edits 적용 / 새 스크립트 실행(`sbmig.script`, 별도 프로세스) → 버전 없는 의존성 채우기(`_fill_versions`) → **멱등성 2회 실행**(ok 면 같은 스크립트를 한 번 더; 또 바꾸면 `mark_non_idempotent`) → 커밋. 새 스크립트는 `add_script` 로 `unverified` 저장(`script_stored`). 거절이면 적용 없이 §A.8.
7. 다음 라운드 빌드가 채점(`_score`): `attempt_scored` → `FixAttempt.outcome`, 오류 목록 `finish_try(…, result)` + `resolve(case, round, n)`, KB `credit(case, version, result, …)` → `ScriptRun` 추가·`successes/failures`·`proven` 승격. 개선이 아니면 `revert`(되돌림, `reverted`).
8. 한도(저장본 1 + LLM 20 그중 보강 15, 거절 1)에 닿으면 `block(case, reason)` → 열린 레코드 `blocked` + `note`, `BlockedItem` 등록. 저널 `blocked`.
9. 루프 끝 → 최종 빌드 → `_finalize`: `end_of_run`(남은 `open` → `unresolved`) → `errors.jsonl` / `errors.md` → `report.error_entries` → `kb.record_run(entries)`: KB `errors.jsonl` 덧붙임(이미 있는 id 는 건너뜀), 케이스마다 `seen` / `resolved` / `projects` / `last_analysis` / `last_solution` / `last_result` 갱신(`note` / `pom-verify` 제외), 에러별 JSON, `cases.md` 재작성. 저널 `error_list`.

### A.12 다음 프로젝트에 전달되는 것

| 객체 | 만드는 곳 | 케이스 관련 속성 |
|---|---|---|
| `KBMatch` | `FileKnowledgeBase.match(cluster)` 라운드마다 | `case_hits[]` = `CaseHit`(`case_id`, `name`, `status` active=proven / candidate=unverified / observed, `action` = `CaseAction(kind=script, script_version)` 또는 `(kind=block, block_reason)` + `pinned`, `errors`, `hints`), `hints[]` = `case <id> (<name>; seen in N project(s)): last solution — …` |
| `ScriptStep` | `sbmig.triage.scripts.plan_script_step` | `case_id`, `mode`(`reuse` / `augment` / `new` / None), `version` + `code` + `meta`(저장 `CaseScript`), `base_attempt`(이 실행의 보강 바탕), `llm_used` / `llm_limit`(신규 + 보강 / `script.max_llm_tries_per_case` 20), `stored_used`(`script.reuse_versions` 1), `augments_used` / `augment_limit`(보강 / `script.max_augments_per_case` 15; 2번째 보강부터 에스컬레이션 모델), `why`, `kb_line` / `kb_found`("저장된 스크립트 없음 (처음 보는 케이스)" — 처음 보는 프로젝트가 KB 적중처럼 읽히면 안 된다) |
| `FixContext` | `BuildFixLoop` → `Fixer.propose_script` | `case_id`, `signature_generic`, `script_mode`(`new` / `augment`), `script_base` = `ScriptBase`(`case_id`, `version`, `code`, `outcome`, `status`, `error`, `log`, `changed_files`, `remaining_errors`, `introduced_errors`, `history` "v1: 2 ok / 1 failed — …"), `kb_hints`(위 힌트), `previous_attempts` / `previous_edits`(이 실행의 앞 시도), `library_hints`(저장소 버전 사실), `advisory_hints`, `reference_files`(병합에서 버려진 사본, 읽기 전용), `layout_facts`(v2.1v "Project after the restructure" — 재구성 결과에서 만든 사실 줄, 자르지 않음), `other_errors_summary` (v2.1z: `fix_mode` 필드 삭제) |

한도 한 줄: 케이스당 실행당 **저장본 1회 + LLM 20회(그중 보강 15회)**, 거절은 **1회** 뒤 차단; 멱등성 검사는 `script.idempotency_check`.

### A.13 사람이 보는 파생 뷰와 손대도 되는 것

- `cases.md` 열: `| case | name | projects | seen / resolved | scripts | pin | last result |` — `scripts` 칸은 `v1 proven 3/0; v2 unverified 0/1 — no_change: 3 of 3 targeted error(s) remained …` 처럼 **마지막 실패 이유**까지, `non-idempotent` 표시 포함. 널리 퍼진 케이스가 먼저.
- CLI: `sbmig kb cases list|show <id>|pin <id> "block:<이유>"|retire <id> <v>|activate <id> <v>`, `sbmig kb errors show <id>`, `sbmig kb scripts list`, `sbmig kb reindex`(통계·`runs[]`·as-is/to-be 조각을 `errors.jsonl` 에서 다시 셈), `sbmig kb validate`(깨진 파일·잘못된 핀·폴더/케이스 불일치), `sbmig kb export|import`(zip, `merge_from` 이 버전 번호를 다시 매김).
- 손대도 되는 것: `pin`, `retire` / `activate`, `libraries.yml`, 사람이 넣은 추가 키. **손대면 안 되는 것**: `seen` / `resolved` / `successes` / `failures` / `runs[]`(reindex 로 만든다), `fix_v*.py` 번호.
- 모든 쓰기는 `<kb>/.lock` 잠금 + 원자적 쓰기, 텍스트는 마스킹(`knowledge/kb/redact.yml`); 읽기는 잠그지 않고 깨진 파일에 예외를 내지 않는다(경고 + `validate`).

### A.14 관련 설정 키

| 키 | 기본 | 영향 |
|---|---|---|
| `errors.diff_chars` / `raw_chars` / `snippet_lines` | 6000 / 600 / 0 | `FixTry.diff`, `ErrorEntry.raw`, `EditHunk` 길이 |
| `errors.md_view` / `json_files` / `script_runs` | true / true / 30 | `errors.md` + `cases.md`, 에러별 JSON, `runs[]` 개수 |
| `kb.dir` / `redact_file` / `lock_timeout_seconds` | `kb-v2` / `knowledge/kb/redact.yml` / 300 | KB 위치, 마스킹 규칙, 잠금 대기 |
| `script.reuse` / `reuse_versions` / `max_augments_per_case` / `max_llm_tries_per_case` / `max_declines_per_case` / `idempotency_check` | true / 1 / 15 / 20 / 1 / true | 사다리 한도(보강 전용 15, 신규 + 보강 20 → `repeated_failure`), 거절 → `needs_owner`, 비멱등 표시 |
| `script.timeout_seconds` / `max_code_chars` / `log_lines` / `extra_allowed_imports` | 60 / 30000 / 200 / [] | 스크립트 실행 `ScriptRunResult` |
| `loop.blocked_policy.<reason>` | §A.8 표 | 사유별 LLM 시도 허용 수 |
| `fix.plugin` / `plugin_options` | null / {} | 어댑터 2층(`docs/ADAPTERS.md`). v2.1z 에서 `fix.mode` / `fix.edits_max_asks_per_case` 삭제 |

---

## B. 노드별 state

### B.1 두 겹의 상태

**(1) 그래프 state `MigrationState`** (`sbmig/graph/engine.py`, TypedDict, 작은 JSON 값만):

| 키 | 타입 | 뜻 | 쓰는 노드 |
|---|---|---|---|
| `run_id` | str | 실행 id(`run-YYYYMMDD-HHMMSS-xxxxxx`) | 초기 상태 |
| `stop_after` | str \| None | `--stop-after` 단계 이름(`intake` … `verify`, `report` 제외) | 초기 상태(읽기만) |
| `dry_run` | bool | 드라이런(빌드·재구성·LLM 없음) | 초기 상태(읽기만) |
| `phases` | list[str] | **아직 돌지 않은** 단계 묶음 이름들(MSA 는 서비스마다 반복) | plan 이 채우고 phase 가 하나씩 뺀다 |
| `phase` | str \| None | 방금 돈/도는 노드·묶음 이름 | 모든 노드 |
| `aborted` | str \| None | 접수·분석의 중단 사유(단계 안의 중단은 예외로 올라간다) | intake, analyze |
| `stopped` | bool | `stop_after` 에 닿음 | intake, analyze, plan |
| `summary` | dict | UI 용 요약(§B.4) | 모든 노드 |

**(2) 오케스트레이터 상태** (`sbmig/loop/orchestrator.py`, 노드 함수가 클로저로 잡고 있음):

| 속성 | 타입 | 뜻 | 채우는 단계 |
|---|---|---|---|
| `report` | `RunReport` | 실행 보고서(결국 `report.json`) — 모든 노드가 여기에 쓴다 | `_run_guard` 가 만들고 `_finalize` 가 닫는다 |
| `profile` | `ProjectProfile` | 분석 결과(`profile.json`) | analyze |
| `plan` | `MigrationPlan` | 계획(`plan.json`) | plan |
| `errors` | `RunErrors` | 실행의 오류 목록 어댑터(`errors.jsonl`) | plan 에서 열림(`_start_error_list`) |
| `loop` | `BuildFixLoop` | 수정 루프(실행당 하나; 단계 묶음들이 공유) | plan |
| `blocked` | `BlockedRegistry` | 실행 전체의 차단 등록부 | 생성 시; 루프가 채움 |
| `budget` | `LoopBudget` | `max_llm_calls` / `max_minutes` / `llm_calls` / `started_at` / `exhausted_reasons` | 생성 시; LLM 호출마다 |
| `_run_steps` / `_verify_done` / `_verify_results` | list | 계획 단계(필요하면 `verify.build` 덧붙임), 최종 빌드가 끝난 검증 단계 id, 최종 `BuildResult` 들(성공 판정 근거) | plan / verify |
| `_restructure_results` / `_restructure_modes` / `_restructure_notes` | list | 이 실행의 `RestructureResult` 들과 모드(`layout` / `boot_pom` / `msa_pom`) → 구조 점검 입력 + 루프의 `layout_facts` | restructure |
| `_review_usage` / `_review_commit_pre` / `_review_guide_source` | dict / str | 점검의 LLM 사용량(모델별), 점검 커밋 전 HEAD, 운영자 가이드 출처 | review |
| `_phase_failed` / `abort_reason` / `_pending_notes` / `_dry_run` | — | 비검증 단계 실패 여부, 첫 중단 사유, 다음 단계에 붙일 메모, 드라이런 | 각 단계 |
| (그래프 전용) `holder = {"groups": [...], "planned": bool}` | 클로저 | plan 이 만든 `(Phase, [(i, PlanStep)])` 묶음 큐와 "계획까지 갔는가" | plan / phase / finalize |

왜 이렇게 나눴나: 그래프 state 는 JSON 이어야 하고(SSE 로 흘림, LangGraph 가 노드를 다른 스레드에서 돌릴 수 있음), 노드는 오류를 처리하지 않아야 중단·예외가 그대로 `_run_guard` 로 올라가 **빌드 게이트(불변조건 1)** 와 보고서 작성이 지켜진다. 웹 UI 의 Stop 은 노드 사이에서만 반영된다.

### B.2 그래프 모양과 라우팅

```
START → intake ─┬→ analyze ─┬→ plan ─┬→ phase ⟲ (holder.groups 가 빌 때까지) ─┬→ finalize → END
                │           │        │                                       │
                └ stopped / aborted ──┴──────────────── finalize ←────────────┘
```

- `after_step`(intake, analyze): `stopped` 또는 `aborted` 면 `finalize`, 아니면 다음 노드.
- `after_plan`: 멈춤/중단이면 `finalize`; 묶음이 있으면 `phase`, 없으면 `finalize`.
- `after_phase`: 묶음이 남았고 멈춤/중단이 아니면 다시 `phase`, 아니면 `finalize`.
- 노드 호출 한 번 = 그래프 한 걸음; `RECURSION_LIMIT = 1000`(MSA 가 서비스마다 `transform@svc` / `verify@svc` 를 반복해 LangGraph 기본 25 로는 모자람).
- 노드가 시작할 때 저널 `{"event": "graph_node", "node": "<이름>", "phase": "<단계>"}`; 끝나면 웹 UI 로 SSE `node` 이벤트 `{"node": "<이름>", "update": <반환한 state 갱신>}`(`sbmig/web/runs.py`, `stream_graph` 의 `stream_mode="updates"`).
- 로그 단계(`sbmig.core.stage`): 노드 안에서 `intake` / `analyze` / `plan` / `<묶음 이름>` / `report` 로 설정하고 끝나면 되돌린다; 단계 함수가 더 가는 태그(`rewrite`, `restructure`, `review`, `build`, `fix r3 …`)를 안에서 건다.

### B.3 노드별 — 읽는 것, 하는 일, 남기는 것

| 노드 | 읽는 state | 부르는 것 | 돌려주는 state 갱신 | 오케스트레이터·보고서에 남는 것 | 파일·커밋 | 저널 이벤트 |
|---|---|---|---|---|---|---|
| **intake** | `stop_after` | `step_intake`: git 스냅샷(`git_init`), 인코딩 후보, 사전 점검(Maven 실행 가능·Java 21·parent 설정), 빌드 도구 판별(Ant → pom 합성, Gradle/없음 → 거부), aggregator 생성·reactor 중복 제거·EAR 거부, as-is effective dependencyManagement 캡처 | `stopped` / `aborted`, `phases: []`, `phase: "intake"`, `summary` | `report.flags`(`mvn-not-found`, `jdk-not-21`, `unsupported-build-tool:<t>`, `pom-synthesized`, `aggregator-generated`, `reactor-duplicate-removed`, `ear-without-modules`, `aborted`), `report.manual_followups`, `report.pom_synthesis`, `report.outputs.effective_depmgmt`, `report.timings_s.intake` | `project/`(git 저장소, 커밋 `intake: …`), `logs/effective-depmgmt.json`, `sbmig.log` | `intake`, `preflight`, `build_tool`, `pom_synthesized`, `aggregator_generated`, `reactor_dedupe`, `ear_without_modules`, `effective_depmgmt`, `abort` |
| **analyze** | `stop_after` | `step_analyze`: 분석기 → `ProjectProfile`; 권고 평가; Maven 아님·pom 없음·루트 pom 없음(MSA 제외)이면 중단 | `stopped` / `aborted`, `phase: "analyze"`, `summary` | `self.profile`, `report.profile`, `report.case_type`, `report.project_name`, `report.advisories`, 플래그 `no-root-pom`, `timings_s.analyze` | `profile.json` | `analyze`(case_type, poms, java, spring_boot), `advisories`, `no_root_pom` |
| **plan** | `stop_after`, `dry_run` | `step_plan`: 계획기(레시피·재구성 모드; 권고 전달) + 저장소 메모 → `MigrationPlan`; 멈춤이면 None; 아니면 오류 목록 열기, `BuildFixLoop` 만들기, 재구성 결과 넘기기(`_handoff_restructure`), `always_final_verify` 면 `verify.build` 덧붙임, 같은 단계끼리 묶기 | `stopped`, `phases: ["transform","restructure","verify"]`(남은 묶음), `phase: "plan"`, `summary`; `holder.groups` / `planned` | `self.plan`, `report.plan`, `report.manual_followups`(`followup:` 메모), `self.errors`(메모리에 열림; 파일은 첫 레코드부터), `self.loop`, `_run_steps`, `timings_s.plan` | `plan.json` | `plan`(steps, notes), `advisories_planned`, `plan_advice` |
| **phase** ×N | `stop_after`; `holder.groups` 의 첫 묶음 | `step_phase(report, phase, items)`: 묶음의 단계들을 차례로(§B.5); `stop_after` 뒤 묶음이면 None(건너뜀); 루프가 중단했으면 `_RunAborted` 를 올림 | `phases`(남은 묶음), `phase: "<묶음>"`, `summary`(건너뛰면 `summary.last = {phase, skipped: "stop_after"}`) | `report.phases[]` 에 `PhaseResult` 추가(예외가 나도 그때까지 한 일은 붙인 뒤 다시 올림), `_phase_failed`, `timings_s.<phase>` | 묶음별 커밋·로그(§B.5) | `phase_start`, `step_start` / `step_end` …, `phase_end`, `phase_skipped`, `run_aborted` |
| **finalize** | `stop_after` | `step_finish`: 계획까지 갔으면 `stop_after` 플래그(`stopped-after-<phase>`)만. **진짜 마무리는 `_run_guard` 의 `_finalize`**(§B.6) — 노드 밖에서, 예외가 났어도 반드시 돈다 | `phase: None`, `summary` | 보고서 전체 완성 | `report.md` / `report.json`, `errors.jsonl` / `errors.md`, `changes.patch`, `result.zip`, KB 기록 | `stop_after`, `error_list`, `handover`, `operator_checks`, `report` |

`_halted` 규칙: 단계 함수가 `False` 를 돌려주면 `report.flags` 에 `aborted` 가 있을 때 `aborted = <사유>`, 아니면 `stopped = True`.

### B.4 `summary` — UI 가 받는 요약

```json
{ "case_type": "spring_boot",
  "phases": [ { "phase": "transform",   "ok": true,  "build_ok": false, "skipped": false, "remaining_errors": 0 },
              { "phase": "restructure", "ok": true,  "build_ok": false, "skipped": false, "remaining_errors": 0 },
              { "phase": "verify",      "ok": false, "build_ok": false, "skipped": false, "remaining_errors": 3 } ],
  "llm_calls": 3, "blocked": 2, "unresolved": 3 }
```

`phases` 는 `report.phases` 의 축약, `llm_calls` 는 `budget.llm_calls`, `blocked` 는 등록부 항목 수, `unresolved` 는 마지막 `PhaseResult.remaining_errors` 수. 웹 화면은 `LLM 호출 N회` 와 노드 진행만 그린다; 나머지는 `journal` / `log` / `report` SSE 이벤트로 간다.

### B.5 phase 노드 안 — 단계 종류별로 남기는 것

한 묶음 = 같은 `Phase` 의 연속 계획 단계. v2 계획은 보통 `transform` [`transform.rewrite`] → `restructure` [`restructure.apply`, `restructure.review`] → `verify` [`verify.build`]; MSA 는 서비스마다 `transform@svc` / `restructure` / `verify@svc`. 각 단계는 `_run_step` 이 종류(`StepKind`)로 나눈다.

| 단계 (kind) | 하는 일 | `PhaseResult` 에 | 오케스트레이터·보고서에 | 오류 목록에 | 파일·커밋 | 저널 |
|---|---|---|---|---|---|---|
| `transform.rewrite` (`openrewrite`) | OpenRewrite 한 번(빌드 없음); 레시피가 글자 그대로의 버전을 남기면 되돌리고 한 번 더 | `rewrite_results[]` = `RewriteResult`(`ok`, `recipes`, `changed_files`, `recipes_applied`, `parse_problems`, `log_path`); 레시피 id 없음 → `ok=False`, 묶음 중단 | `timings_s.transform` | (없음; `plan.compile_after_rewrite: true` 일 때만 `transform.compile` 의 오류가 `seen=compile` 로) | 커밋 `rewrite: transform.rewrite`, `logs/NNN-rewrite-*.log` | `rewrite`, `rewrite_parse_problems`, `rewrite_unresolved_versions`, `rewrite_oom_retry` |
| `restructure.apply` (`restructure` / `msa_pom`) | 회사 레이아웃(부모·모듈·필수/선택·JSP·web.xml·legacy context D1–D12·스코프 복원·sourceEncoding·nonFilteredFileExtensions·버전 핀) | `notes[]`(요약 줄, 병합 메모, JSP 메모) | `_restructure_results[]` / `_restructure_modes[]`(→ 점검 입력, 루프 `layout_facts`), `report.manual_followups`, `report.flags`(`web-xml-registered` …), `report.merge_conflicts`, `loop.merge_conflicts`, 차단 등록부의 파일 이동(`note_moves`), 실패 시 `restructure-failed`(최종 빌드가 통과해도 `success=False`) | pom 교정마다 **`pom-verify` 레코드**(`seen=restructure`: `parent … → …`, `explicit version dropped`, `scope restored: …`, `source encoding written: …`, `nonFilteredFileExtensions`, 생성 pom …) | 커밋 `restructure: restructure.apply`, `logs/depmgmt-after-restructure.json` | `restructure`(모드·커밋·개수·bom_imports·…), `restructure_moves`, `placeholders`, `legacy_context`, `legacy_xml_imports`, `web_xml_registrations`, `jsp_jakarta`, `versions_pinned`, `bom_imports`, `non_filtered_extensions`, `parent_chain_facts`, `restructure_failed` |
| `restructure.review` (`review`) | LLM 구조 점검(≤3회: 점검 + 형식 재시도 + 적용 실패 재시도): critical 은 edits 로 직접 고치고 커밋, 불변식 검사 뒤 되돌릴 수 있음; warning 은 보고서 | `notes[]` | `report.structure_review` = `StructureReview`(`verdict` ok/issues/skipped/failed, `issues[]` = `ReviewIssue`(`severity`, `file`, `problem`, `fix_hint`, `unsupported`), `analysis`, `answer`, `commit`, `reverted`, `major_upgrades`, `llm_usage`, `note`), `_review_usage`, `report.operator_checks`(메이저 변경) | warning → `note` 레코드(`seen=review`); 재료에 없는 지적 → `… (not in the review material, not applied)` 메모; 직접 고친 critical → `review|structure` 케이스의 레코드 + `FixTry`(source `review`, round 0) → `resolved: review` | 커밋 `review: fix critical structure issues (<n>)`, `logs/review-input.md`(점검에 준 재료; v2.1v) | `structure_review`(verdict, issues, critical, commit), `review_script_stored`, `major_upgrade`, `llm_call` |
| `verify.build` (`build`, phase verify) | `mvn clean package -Dmaven.test.skip=true` → 실패면 수정 루프(아래) → **최종 빌드 한 번 더** | `ok` = `build_ok` = 모든 최종 빌드 성공, `iterations`, `attempts[]`(`FixAttempt`), `iteration_records[]`, `remaining_errors[]`, `blocked[]`, `notes[]` | `_verify_results[]`, `_verify_done[]`, `report.final_build`, 루프의 `flags` / `followups`(마무리 때 보고서로) | `add_build(build, "build")`(새 오류만 `open`), `verify_poms`(pom-verify → resolved), 라운드마다 `add_build(…, "fix rN")`, `begin_try` / `finish_try` / `resolve` / `block` | `logs/NNN-build-verify.log`, 라운드 로그, 시도 커밋 `fix(script:<case>:v<N> <mode>)` / `fix(edits:<case> <source>)`, 되돌림 커밋 | `build`, `verify_loop_start`, `loop_start`, 라운드마다 `build` · `triage` · `decision` · `layout_facts` · `llm_call` · `attempt` · `script_stored` · `attempt_scored` · `script_credit` · `revert` · `declined` · `blocked` · `same_cause_blocked` · `version_try` / `resources_try`, `loop_end`, `verify_loop_end` |

수정 루프의 라운드 상태(`BuildFixLoop`, 메모리에만): `_Round`(`iteration`, `round`, `build`, `target`, `stall_level`, `pending[]`(채점 대기 시도 + 전후 sha), `decisions[]`, `covered`(오류 파일 → 이번 라운드에 손댄 케이스), `poms`(pom → 케이스), `hopeless`, `restored`(되돌림이 되살린 오류 = 다음 채점 기준선)); 시도별 사전 `_tries`(attempt → (case, n, round)), `_diffs`, `_credited`, `_rolled_back`, `_script_ran`, `_edit_results`, `_seconds`, `_tally`, `_answers` / `_notes`(수정 메모), `_introduced`(파일·시그니처 → 그것을 만든 시도), `_declines` / `_owner_blocked`(v2.1y 거절·형제 차단), `_pre_tried` / `_pre_failures`(sbmig 선시도), `history`(이 실행의 모든 `FixAttempt`, 사다리와 한도가 본다). 끝 조건은 언제나 빌드 뒤: BUILD SUCCESS · 라운드 소진(`loop.phases.verify.max_iterations` 50) · 손댈 케이스 없음 · 차단만 남음 · 시간 초과(`loop.max_total_minutes`).

### B.6 마무리 `_finalize` — 보고서에 남는 것 (순서대로)

1. `report.finished_at`; LLM 사용량 합산(`llm_usage`, `llm_usage_by_model`, `llm_cost`; 시도 + 점검 + 계획 조언).
2. 루프의 `flags` / `followups` → `report.flags` / `manual_followups`; `llm-budget-exhausted`; 빌드가 돌았으면 `tests-not-compiled` + 후속 조치.
3. **성공 판정** `report.success` = 최종 빌드가 있고 전부 성공 ∧ `restructure-failed` 없음 ∧ 중단 아님 ∧ 빠진 최종 빌드 없음(MSA 의 svc-b 가 죽은 실행은 실패).
4. `report.unresolved` = 실패한 최종 빌드의 오류(차단된 것 제외; 최종 빌드가 없으면 마지막 단계의 `remaining_errors`); `report.blocked` = 등록부 + 단계들의 차단 항목(id 로 중복 제거; 초록 빌드면 더 이상 안 보이는 항목은 뺌); javax API 인벤토리; infra 플래그(`infra:<kind>`).
5. `report.outputs`: `run_dir`, `project_dir`, `journal`, `logs`, `profile`, `plan`, `diff`, `zip`, `report_md`, `report_json`, `errors`, `errors_md`, `effective_depmgmt`.
6. `_finalize_operator_checks`(메이저 변경 → "운영자 확인 필요"), `_finalize_advisories`(권고 검증 + `report.handover` = `HandoverSummary`: `parent_before/after`, `java_*`, `spring_boot_*`, `changes[]`, `added[]`, `removed[]`, `carried_over[]`, `from_here[]`, `properties_to_set`, `notes`, `merge_conflicts`).
7. `_finalize_errors`: `end_of_run`(open → unresolved) → `errors.jsonl` / `errors.md` → `report.error_entries`(diff 캡) → **KB `record_run`**(§A.11 9) → 저널 `error_list`.
8. 보고서 작성(`report.md` / `report.json`; 섹션: Handover → Error list → Fixes by case → Structure review → Blocked → Manual follow-ups → Flags → What we changed → LLM usage and cost → Notes → Outputs) → 느린 `changes.patch` / `result.zip` 은 **맨 끝**(zip 도중 죽어도 중요한 것은 이미 남아 있다).
9. 콘솔·로그에 `SBMIG-RESULT run=… exit=<0|2|3> build=<ok|FAIL> unresolved=<n> blocked=<n> open=<n> resolved=<n> llm=<n> attn=<codes>[ review=<v>][ major=<n>]` (v2.1i 의 ` fix=<mode>` 는 v2.1z 에서 없어짐). 종료 코드: 3 = `aborted` / `mvn-not-found`, 0 = 성공이고 차단 없음, 그 밖은 2. 실물: `SBMIG-RESULT run=run-20261031-193430-30da3d exit=2 build=FAIL unresolved=0 blocked=2 open=0 resolved=7 llm=3 attn=blocked:external_javax_api,tests-not-compiled review=issues`.

`report.error_cases` / `error_case_counts` 는 v1 시절 레코드라 v2.1 에서는 비어 있다(`0` / `{}`); 오류 기록은 `error_entries` 다.

### B.7 `PhaseResult` 와 `RunReport` — 핵심 필드

`PhaseResult`(묶음 하나, `report.phases[]`): `phase`, `step_ids[]`, `ok`, `build_ok`, `iterations`, `rewrite_results[]`, `attempts[]`(`FixAttempt`), `iteration_records[]`(`IterationRecord`: `iteration`, `build_ok`, `error_count`, `signatures`, `cluster_count`, `decisions`, `attempt_ids`, `stalled`, `stall_level`, `build_log`), `remaining_errors[]`(`BuildError`), `blocked[]`(`BlockedItem`), `notes[]`, `skipped`, `started_at` / `finished_at`.

`RunReport`(`report.json`): `run_id`, `project_name`, `case_type`, `started_at` / `finished_at`, `success`, `profile`, `plan`, `phases[]`, `final_build`, `llm_usage` / `llm_usage_by_model` / `llm_cost`, `unresolved[]`, `blocked[]`, `manual_followups[]`, `advisories[]`, `handover`, `merge_conflicts[]`, `properties`, `flags[]`, `javax_api_jars[]`, `structure_review`, `error_entries[]`, `operator_checks[]`, `pom_synthesis`, `outputs{}`, `timings_s{}`(실물: `intake 1.949, analyze 0.083, plan 0.001, transform 19.359, review 0.07, restructure 2.389, verify 7.296, total 31.487`).

### B.8 멈춤 · 중단 · 드라이런이 state 에 보이는 모양

| 상황 | 어디서 | state | 보고서 | 종료 코드 |
|---|---|---|---|---|
| `--stop-after <phase>` | intake / analyze / plan 뒤: `stopped=True` → finalize; phase: `stop_after` 뒤 묶음은 `phase_skipped`(None) | `stopped: true`, `summary.last.skipped: "stop_after"` | 플래그 `stopped-after-<phase>`, 빌드 없음 | 2(성공 아님) |
| 접수·분석의 중단(Maven 없음, JDK 21 아님, Gradle, pom 없음, 루트 pom 없음, EAR) | `_abort` → 플래그 `aborted` + 후속 조치 `run aborted: <why>` + ERROR 한 줄 | `aborted: "<why>"` → finalize | `flags` 에 `aborted`, 진단 플래그(`mvn-not-found` …) | 3 |
| 단계 안의 중단(설정 parent 를 못 받음 등 `loop.aborted`) | `step_phase` 가 `_RunAborted` 를 올림 → 그래프 밖 `_run_guard` 가 받음 | (노드가 끝나지 않음) | 그때까지의 `PhaseResult` 는 붙어 있음, `run_aborted` 저널, 실패한 빌드가 그대로 결과 | 3 |
| 드라이런 | 재구성·점검·빌드를 건너뜀(`skipped` 점검, 메모) | `dry_run: true` | `report.md` 만, `errors.jsonl` 없음, KB 기록 없음 | — |
| 웹 UI Stop | 노드 사이에서만 | 다음 노드로 가지 않음 | `_run_guard` 가 보고서를 마무리 | — |

### B.9 실물 저널 순서 (boot2-javax-lib, BLOCKED 실행)

```
intake → graph_node(intake) → preflight → graph_node(analyze) → analyze → graph_node(plan) → plan
→ graph_node(phase, transform) → phase_start → step_start(transform.rewrite) → step_end → phase_end
→ graph_node(phase, restructure) → phase_start → step_start(restructure.apply) → restructure → step_end
                                              → step_start(restructure.review) → structure_review(issues 1, critical 0) → step_end → phase_end
→ graph_node(phase, verify) → phase_start → step_start(verify.build) → build(FAIL, 3 errors) → verify_loop_start → loop_start
    → build(iteration 1, reused) → triage(case:f2b133edd9, case:61d819f63f)
    → decision(script, "new fix … looks like external_javax_api") → layout_facts → declined(declines 1, limit 1) → attempt(verify-1-1, blocked) → blocked(blk-12b207af03, external_javax_api)
    → decision → declined → attempt(verify-1-2, blocked) → blocked(blk-cd7797d799)
  → loop_end(llm_calls_used 3) → verify_loop_end → build(final, FAIL) → step_end → phase_end
→ graph_node(finalize) → error_list(resolved 7, note 1, blocked 3; kb_recorded 11) → report(success false, blocked 2, llm_calls 3)
```

세 번째 LLM 호출은 구조 점검이다(`review=issues`). 두 케이스는 각각 거절 1회 뒤 트리아지가 이미 붙인 사유 `external_javax_api` 로 차단됐고, 오류 목록의 레코드 3건은 `blocked`, 재구성의 pom-verify 7건은 첫 빌드가 `resolved` 처리했다.
