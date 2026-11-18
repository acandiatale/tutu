# 실행하기, 결과 읽기, 진단하기

> 다루는 것: PC 준비(`.env`, JDK 21, Maven, 첫 점검 명령), `sbmig` 명령과 옵션, 실행 디렉터리(`out/<run id>/`)의 파일과 로그 줄, 저널 이벤트, 종료 코드와 `attn=` 코드, 차단 사유, 진단 순서와 요약 카드, 회사 Windows PC 에서 줄을 물어보는 법.
> 언제 읽나: sbmig 를 돌리기 전, 그리고 실행이 실패·미완(exit 2 / 3)으로 끝나 "왜" 를 찾아야 할 때.
> 함께 읽을 파일: [02](02-flow-and-nodes.md), [04](04-cases-scripts-kb.md), [05](05-llm-gateway-and-prompts.md), [07](07-config-and-knowledge.md), [../docs/RUNBOOK.md](../docs/RUNBOOK.md)

## 1. PC 준비

1. Python 3.10+, 저장소 루트에서 `uv venv && source .venv/bin/activate && uv pip install -e ".[dev]"`(Windows `.venv\Scripts\activate`).
   수정 스크립트 인터프리터(`script.python`, 비우면 sbmig 의 것)에 pydantic 과 lxml 이 있어야 한다.
2. **JDK 21 + Maven**. 실행 시작의 preflight 가 `mvn -v` 의 Java 주 버전을 본다(`build.require_java_major` 21, 다르면 중단).
   JDK 는 `build.java_home` 또는 `sbmig run --java-home`, 사내 Nexus 는 `build.settings_file` 또는 `--settings`.
3. **`.env`** 를 저장소 루트에 만든다(git 무시). 예시는 `.env.example`. 읽는 순서: `SBMIG_ENV_FILE`, 패키지 폴더에서
   git 루트까지, 현재 폴더에서 git 루트까지 — 가까운 파일이 이기고 셸 변수가 모두를 이긴다. 명령줄 옵션(`--settings`, `--java-home` …)은
   `SBMIG_<SECTION>__<KEY>` 환경 값보다 이긴다(INFO `config: <키> given explicitly replaces <변수> from the environment`). 변수 이름(값은 문서·로그에 쓰지 않는다):

| 변수 | 쓰임 |
|---|---|
| `LLM_BASE_URL` | 게이트웨이 전체 URL(그대로 씀; agent 게이트웨이는 `{asset_id}` 자리표시 가능) |
| `LLM_API_KEY` | Bearer 키(토큰 교환을 쓰면 앱 토큰 대용) |
| `LLM_MODEL`, `LLM_ESCALATION_MODEL`, `LLM_RECIPE_MODEL` | 수정 모델 / 더 센 모델 / 계획 조언용 모델(agent 게이트웨이에서는 `params.role` 값) |
| `LLM_RESPONSE_PATH` | 응답 본문 위치를 못 찾을 때만(점 경로) |
| `LLM_RECORD_DIR` | 호출마다 `llm-NNNN.json` 을 남길 폴더(`llm.record_dir`) |
| `LLM_GATEWAY`, `LLM_ASSET_ID`, `LLM_TOKEN_URL`, `LLM_APP_TOKEN` | 사내 GPT agent 게이트웨이 모드와 토큰 교환([05](05-llm-gateway-and-prompts.md)) |
| `JAVA21_HOME` | e2e 스크립트용 JDK 21 경로 |
| `SBMIG_WEB_OUT`, `SBMIG_MAX_UPLOAD_MB`, `SBMIG_WEB_CONFIGS` | 웹 UI(plain uvicorn 의 출력 폴더 / 업로드 상한 / 업로드가 돌 config 이름) |

4. **첫 점검 두 개**:
   - `sbmig llm-probe` — 게이트웨이에 작은 요청 하나: 설정 줄(provider / model / url / key_env / payload_extra), agent 모드면
     `gateway=… url=… asset_id=… token_url=…`, `request payload: …`, 보낸 헤더(토큰 가림)·프록시·응답, `extracted reply: …`, 끝에
     `LLM-PROBE ok …` 또는 `LLM-PROBE FAIL <kind> …`(config / transport / token / http / reply / rejected; 토큰 교환이면 `token exchange: …` 줄).
     `--bare` 는 sbmig 클라이언트 없이 `requests.post` 로 같은 payload. 종료 코드 0 / 1.
   - `sbmig repo-probe <groupId:artifactId[:version]>` — 묻는 저장소 URL 과 순서(`build.repository_url`, settings 미러, pom repositories,
     Central), URL 마다 답(ok / 404 / 401 / timeout), jakarta 판정, 끝 줄 `REPO-PROBE ok|none …`. `--project <dir>` 면 그 pom 들의 `<repositories>` 도.

## 2. 명령

모든 명령은 `--config/-c <yml>` 를 여러 번 받는다(뒤가 이김; 기본 `config/default.yml` 위에 겹침). 최상위 옵션 `-v`(DEBUG), `--log-file <path>`.

| 명령 | 하는 일과 옵션 |
|---|---|
| `sbmig run <dir|zip>` | 한 프로젝트를 끝까지. `--out/-o`(기본 `out`), `--run-id`, `--stop-after intake|analyze|plan|transform|restructure|verify`, `--dry-run`(결정만 저널, 적용 없음), `--flow full|remediation`([11](11-remediations.md)), `--llm-provider`, `--llm-plugin pkg.mod:attr`, `--fix-plugin pkg.mod:attr`, `--java-home`, `--settings`, `--repo-root` |
| `sbmig serve` | 웹 UI(FastAPI + uvicorn, [09](09-deploy-and-ops.md)). `--host`(127.0.0.1), `--port`(8765), `--out`(`out/web`), `--graceful-timeout`, `--max-upload-mb`, `--token`(루프백이 아닌 host 면 필수, `SBMIG_WEB_TOKEN`) 또는 `--open`(토큰 없이 공개, `SBMIG_WEB_OPEN=1`), `--mode in-process|k8s-job|local-job`(`SBMIG_WEB_MODE`), `--config`(업로드가 돌 `config/*.yml` 이름, 여러 번; `SBMIG_WEB_CONFIGS`) |
| `sbmig analyze <dir>` | 분석만(변경 없음): 종류, Java·Boot 버전, 라이브러리, 권고. `--json` |
| `sbmig plan <dir>` | 분석 + 계획 단계 목록. `--json` |
| `sbmig build <dir>` | 그 폴더에서 Maven 한 번 + 파싱된 오류와 L1 서명(디버깅). `--goal/-g`(기본 compile), `--out` |
| `sbmig parse-log <logfile>` | 이미 있는 Maven 로그를 오류 목록으로. `--root`, `--json` |
| `sbmig inventory <dir|zip>` | pom 도 build.xml 도 없는 웹앱 트리의 설명서만(거부 안내용). `--out`(기본 `out/inventory`), `--json` |
| `sbmig clean` | 끝난 실행의 프로젝트 복사본·업로드 zip·LLM 기록 지우기(실행 중·대기 중인 실행의 업로드는 남김). `--out`, `--fleet`, `--older-than DAYS`, `--keep-failed`, `--dry-run`, `--records` |
| `sbmig kb …` | KB 명령([04](04-cases-scripts-kb.md) §8) |
| `sbmig fleet run <manifest>` | 여러 프로젝트를 KB 하나로 차례로. `--out`, `--order manifest|simple-first`, `--max-projects`, `--retry-failed`, `--stop-on-abort`, `--max-llm-calls-total`, `--dry-run`, `--llm-provider`. `sbmig fleet resume <out>`, `sbmig fleet status [out] [--attention] [--json]` |
| 그 밖 | `sbmig rewrite`(OpenRewrite 단독), `sbmig recipes …`, `sbmig advisories …`, `sbmig version`(빌드 식별값 `0.1.0+<12자>`) |

예: `sbmig run D:\in\app.zip -c config\artifacts.yml -o D:\sbmig\out`. 웹 UI 는 `sbmig serve`(= `uvicorn sbmig.web.app:app`).
플릿은 `<out>/fleet/` 에 `state.json`, `summary.md`, `summary.csv`(attention 칸), `fleet/sbmig.log` 를 쓴다. 플릿 시작 전 점검은
`preflight: <점검> ok|FAIL|skip …` 줄(disk / maven / java / settings / repository / llm, `sbmig/fleet/preflight.py`)이다.

## 3. 실행 디렉터리 지도 — `out/<run id>/`

실행 id 는 `run-YYYYMMDD-HHMMSS-xxxxxx` 꼴. 남는 것(`sbmig/core/workspace.py` 의 `RUN_DIR_FILES` + `logs/`, `project/`):

| 경로 | 무엇 | 언제 본다 |
|---|---|---|
| `out/<run id>/report.md`, `out/<run id>/report.json` | 보고서(§3.1, ko 면 한국어 안내서) | 항상 먼저 |
| `out/<run id>/errors.md`, `out/<run id>/errors.jsonl` | 오류 목록: 오류 한 건 = 한 항목, 시도(`tries`) 전부 | 오류마다의 시도 |
| `out/<run id>/journal.jsonl` | 이벤트 한 줄씩 `{"ts", "run_id", "event", …}`(§4) | 결정·채점·차단 근거 |
| `out/<run id>/sbmig.log` | DEBUG 로그, 줄마다 단계 태그 `[fix r3 7a52b1cea8]`(§3.3) | 시간 순서로 |
| `out/<run id>/logs/NNN-<label>.log` | Maven / OpenRewrite / deptree / classpath / effective-pom 호출마다, 번호 순 | **실제 오류 메시지** |
| `out/<run id>/logs/effective-depmgmt.json`, `…/logs/effective-depmgmt-after-restructure.json` | as-is / 재구성 뒤 관리 버전 | 버전 케이스 |
| `out/<run id>/logs/bundled-jars.json` | pom 이 선언하지 않은 트리 안 jar: 경로, pom.properties 좌표(없으면 null), 제공 패키지 | `package … does not exist` 일 때 |
| `out/<run id>/logs/review-input.md` | 구조 검토에 보낸 user 프롬프트(`review.save_input`) | 검토가 이상할 때 |
| `out/<run id>/logs/rewrite-N.yml`, `…/logs/rewrite-artifact-versions.json`, `deptree-*.txt`, `classpath-*.txt` | 레시피, 해석된 레시피 버전, 의존성 트리, 클래스패스 | 레시피·의존성 |
| `out/<run id>/project/` | 작업 복사본 = git 저장소(브랜치 `sbmig/<run id>`) — §3.4 | 시도가 바꾼 것 |
| `out/<run id>/changes.patch`, `out/<run id>/result.zip` | as-is 대비 diff, 이관된 프로젝트 | 담당팀 전달 |
| `profile.json` / `plan.json` (실행 폴더) | 분석 프로필, 계획 | 분류가 이상하면 |
| `llm.record_dir` 아래 `llm-NNNN.json` | LLM 호출마다 하나(§3.5) | LLM 이 무엇을 받고 답했나 |

웹 실행은 `web-status.json` 이, 다중 모듈 병합은 `merge-conflicts/` 가 더 생긴다.

### 3.1 보고서 절 순서

`report.language: ko`(회사 설정) = 요청자용 안내서(`sbmig/report/guide_ko.py`; 중단된 실행의 결과는 빌드가 하나도 없으면 "빌드까지 진행하지
못했습니다", 빌드가 있었으면 "실행 중단 (마지막 빌드는 통과했습니다 / 통과하지 않았습니다)"): 머리 표(결과, `SBMIG-RESULT`, 전환 후, 실행 ID,
시작 / 종료, 플래그) → 사내 프레임워크 프로젝트 → 전환 내용 → **확인해 주세요**(권고 + 인수인계 할 일) → 코드 수정 내역(자동 변환,
직접 수정한 파일마다 순변경 diff) → 해결하지 못한 오류(남은 것만) → 진행하지 않은 항목(차단) → 특수 절(운영자 확인, pom 합성 (Ant),
ADF, 포함된 jar, javax API jar, 외부 설정 값, 취약점 조치) → 참고(접힘: 실행 정보·구조 점검 판정·플래그 설명, LLM 비용, 산출물,
**진단용 기록** = 단계 메모와 루프가 **왜 끝났는지**). 해결된 오류는 `errors.md` 에만 있다.
`en`(`sbmig/report/markdown.py`): 머리 표 → Handover → ADF sizing → Error list → Fixes by case → Structure review → Bundled jars →
Blocked → javax API jars → Manual follow-ups → Flags → What we changed → LLM usage and cost → Notes → Outputs.

루프 종료 문장(진단용 기록 / Notes 의 `verify: …`): `max iterations (50) reached with N error(s) left` / `run time budget exhausted: stopping the
build-fix loop` / `build N: only blocked cluster(s) remain …` / `build N: no actionable fixes (all clusters skipped/escalated/blocked/
failed to apply) — rebuilding would not change anything`. 그 밖에 `build N: error set equals an earlier one (oscillation #k) — treated
as a stall`, `build N: regressed attempt(s) reverted → rebuilding the reverted tree`, `<n> attempt(s) that did not help rolled back before the final build`,
`<단계>: final build not repeated — the fix loop left on a stop request …`.

### 3.2 오류 목록 (`out/<run id>/errors.md` / `errors.jsonl`)

errors.md 표의 열: `# | seen | status | case | file:line | message | tries | resolved`. 그 아래 "Tries" 절에 시도마다 원인 / 해결 / `수정:`
줄 / 바뀐 파일 / diff. 값의 뜻:

- `seen`: 처음 본 곳 — `compile` / `restructure` / `review` / `build` / `fix rN`.
- `status`: `open`(마지막 빌드에 있음) / `pom-verify`(재구성의 pom 수정, 다음 빌드가 검증) / `resolved` / `blocked` / `unresolved`(끝까지 남음) / `note`(검토 경고 같은 메모).
- `tries` 칸: `n 출처 vN 적용 결과 (수정 요지)` 를 `→` 로 이음(예 `1 llm-new v1 script partial (…) → 2 llm-augment v2 script build_ok (…)`).
- `resolved`: `rN try M`(그 시도로 해결) / `build`(시도 없이 사라짐) / `review`.
- jsonl 한 줄 = `ErrorEntry`(`sbmig/core/models.py`): `id`, `run_id`, `project`, `case_id`, `kind`, `file`, `line`, `module`, `message`,
  `raw`(600자), `seen`, `status`, `tries[]`, `resolved`, `resolved_by[]`, `script`, `note`, `at`. `tries[]` 한 칸 = `FixTry`: `n`, `round`,
  `source`, `script_version`, `analysis`(원인), `solution`(해결), `no_script_reason`, `applied`(script / edits / none), `files`, `edits`(수정 설명),
  `major_upgrades`, `verify`, `diff`(`errors.diff_chars` 6000자까지; 전체는 changes.patch), `commit`, `result`, `note`, `llm_calls`, `seconds`.

### 3.3 `out/<run id>/sbmig.log` 의 핵심 줄

| 줄 (그대로 grep) | 뜻 |
|---|---|
| `SBMIG-RESULT run=… exit=… build=ok|FAIL unresolved=… blocked=… open=… resolved=… llm=… attn=… [review=…] [major=…] [scope=jdk-jakarta]` | 옮겨 적는 결과 한 줄(콘솔 끝, report.md 머리). `scope=`는 [13](13-framework-projects.md) |
| `preflight: …` (ERROR) | 실행 시작 점검 실패(JDK / Maven). 플릿은 `preflight: <점검> FAIL …` |
| `[intake]` `[analyze]` `[rewrite]` `[restructure]` `[review]` `[build]` `[fix r2]` `[fix r2 <case_id>]` `[kb]` `[report]` | 줄마다 붙는 단계 태그 |
| `N open → M case(s); 순서: …` | 라운드의 케이스와 순서(근본 원인 먼저) |
| `KB 탐색: …` | 케이스마다 사다리 결정(예 `KB 탐색: 저장된 스크립트 없음 (처음 보는 케이스) → LLM 에 새 수정 요청`, `… → 한도 도달 … → blocked`) |
| `의존성 버전 조회: …` | LLM 에 보내는 저장소 버전 사실 |
| `script prompt for case:… packed: N chars, … sections [...]` | 프롬프트 크기와 절 |
| `llm call provider=company … attempt n/m → POST <url> (prompt N chars, timeout Ts)` | LLM 요청. 끝나면 `llm call provider=company model=… in=… out=… latency=… finish=…` |
| `llm company transport error (attempt n/m): … cause=<예외>` | 전송 실패. `cause=` 뒤가 원인 클래스 |
| `thread turn (session <id>): sent N chars in K message(s) → prompt_tokens=T` | 케이스 스레드 턴 측정(`llm.case_thread`) |
| `repository lookup: <id> (<url>) is blocked by Maven's HTTP blocker (mirror 'maven-default-http-blocker', …)` | http 차단 미러가 사내 Nexus(http) 를 덮었다. §5 `infra:http-blocked` |
| `repository lookup: settings <경로> (~/.m2/settings.xml): missing` / `WARNING settings <경로> could not be read (…)` | 조회가 못 쓴 settings.xml — Nexus 가 조회에 없을 때 먼저 볼 줄(`sbmig repo-probe g:a -c <회사 yml> --project <실행>/project` 도) |
| 시도 메모 `the build advanced past the fixed pom problem: … not a regression of this attempt` | pom 문제가 풀려 다음 단계 오류가 보임 — 개선 |
| 시도 메모 `regressed: structural error kind(s) … appeared after this attempt edited a pom.xml → reverted` | pom 편집 뒤 새 구조적 오류 종류 — 되돌림 |
| `수정: <path> <줄> — <변경>` | 적용된 변경 하나(INFO) |
| `메이저 업그레이드: … — 운영자 확인 필요` | 메이저 버전 변경(되돌리지 않음, 보고서 「운영자 확인」) |
| `build N: K error(s); 새 에러 X건 추가; resolved …` / `build N: BUILD SUCCESS` | 라운드 빌드 요약 |
| `결과 (build N): resolved a/b → <결과>` | 직전 시도 채점(`regressed → 되돌림`, `inconclusive (빌드가 확인 못 함)` …) |
| `LLM 의 거절을 받지 않음: …` | 거절이 저장소 사실과 모순(`decline_rejected`) |
| `config: … is ignored since …` / `config: <키> is not a known key and is ignored (… did you mean build.x?)` | 없어진 키 / 들여쓰기·철자가 틀린 키 |
| `config: <파일>: key '<키>' appears twice (lines A and B)` | 같은 최상위 블록을 두 번 적음: 마지막 블록만 남는다 |
| `repository lookup: repository set incomplete: …` / probe `build.repository_url: not set` | 저장소 집합 구성 실패 / yml 의 URL 이 조회에 안 옴(-c 누락, 키 위치) |

### 3.4 `project/` 의 git

첫 커밋 `sbmig: as-is snapshot`. 그 뒤 커밋 하나 = 단계 하나 또는 시도 하나:
`intake: pom.xml synthesized from build.xml`, `intake: generated root aggregator pom (…)`, `rewrite: transform.rewrite`,
`restructure: …`(여러 개), `review: fix critical structure issues (n)`, 시도는 `fix(script:<case_id>:v<N> <new|augment|reuse>): case:<case_id>`,
`fix(edits:<case_id> <출처>): case:<case_id>`, LLM 없는 사전 시도 `fix(sbmig-version|sbmig-resources|sbmig-jakarta|sbmig-bundled): case:<case_id>`.
되돌리기는 대개 `git reset --hard`(커밋이 사라짐; 실패한 재구성 단계도), 뒤 커밋이 있으면 `git revert`(→ `Revert "…"`). 되돌린 시도의 diff 는
`errors.jsonl` 의 `tries[].diff` 에서 본다. 쓸 명령: `git log --oneline`, `git show <sha> --stat`, `git show <sha> -- <file>`,
as-is 조회 `git rev-list --max-parents=0 HEAD` → `git grep -l <패턴> <sha>`, `git show <sha>:<경로>`.

### 3.5 LLM 기록 `llm-NNNN.json`

`llm.record_dir`(= `LLM_RECORD_DIR`) 아래 호출마다 하나(번호는 기존 파일 다음). 키: `messages`(system / user 전문 — 오류,
소스 발췌, pom, `Dependency versions` 사실, `Previous attempts`, `Project after the restructure`), `response`(답 전문), `session_id`,
`model`, `usage`, `finish_reason`, `latency`, `prompt_hash`, `error`(실패한 호출), `role`(fix / fix_escalation / structure_review /
verify_guide …), `metadata`(run_id, 케이스 등). 키·헤더는 없다. 재생 입력이 된다([04](04-cases-scripts-kb.md) §9).

## 4. 저널 이벤트 (`out/<run id>/journal.jsonl`)

모두 `Workspace.journal` 로 쓴다(`sbmig/core/workspace.py`). 진단에 쓰는 것:

| 이벤트 | 뜻 |
|---|---|
| `intake`, `intake_refused`, `preflight`, `build_tool`, `pom_synthesized`, `aggregator_generated`, `no_root_pom`, `reactor_dedupe`, `stray_pom_ignored` | 접수: 입력, ZIP 거절(풀기 한도), 점검, 빌드 도구, Ant pom 합성, 루트 집계 pom, 리액터 중복 제거, 떠도는 pom 복사본 |
| `analyze`, `hollow_reactor`, `sources_outside_reactor`, `adf_project`, `adf_traces`, `bundled_jars`, `framework_project`, `framework_check`, `advisories` | 분석 판정 |
| `plan`, `step_start`, `step_end`, `phase_start`, `phase_end`, `graph_node`, `stop_after` | 계획과 단계 진행 |
| `rewrite`, `rewrite_download_retry(_result)`, `rewrite_oom_retry(_result)`, `rewrite_parse_problems`, `asis_parent_aligned` (`where: rewrite`), `rewrite_skipped_asis_parent` | OpenRewrite (as-is 부모가 저장소에 없음: 맞춤 / 레시피 없이 계속) |
| `restructure`, `restructure_moves`, `restructure_failed`, `restructure_reset`(실패한 단계를 되돌림), `versions_pinned`, `parent_chain_facts`, `parent_chain_no_boot_bom`, `bom_property_overrides`, `non_filtered_extensions`, `jsp_jakarta`, `bundled_jar_wired` (`stage: restructure`), `bundled_jars_skipped` | 재구성 결과(건너뛴 jar 의 이유: superseded / container / unused / declared / skip) |
| `structure_review` | 구조 검토 판정 |
| `verify`, `code_changes_base`(diff 기준 커밋), `verify_loop_start`, `loop_start`, `build`, `build_file_lock_retry`, `build_negative_cache_retry` | 빌드 |
| `triage` | 라운드의 케이스 목록과 순서 |
| `decision` | 케이스별 SCRIPT / BLOCKED 와 `rationale`(사다리 단계, KB 탐색 결과) |
| `library_hints` (`lines` = LLM 이 받은 사실), `library_lookup`, `library_mapped`, `library_unmapped` | 라이브러리 → 저장소 조회 |
| `version_try`, `resources_try`, `jakarta_try`, `bundled_jar_wired` (`stage: loop`), `asis_parent_aligned` (`where: fix loop`) | LLM 없는 사전 시도 |
| `scopes_restored`, `source_encodings` | [13](13-framework-projects.md) 4절 |
| `llm_call`, `llm_script`, `llm_verify_guide`, `llm_cooldown`, `llm_unavailable`, `llm_config_error` | LLM 호출과 장애 |
| `attempt`, `attempt_scored`, `script_stored`, `script_credit`, `script_non_idempotent`, `framework_guard` | 시도, 재빌드 채점, KB 저장·실적 |
| `revert`, `script_rollback`(`where: "loop end"` = 루프 끝에 도움 안 된 시도), `script_rollback_undone`, `reround` | 되돌림, 같은 빌드로 다음 단계 묻기 |
| `stall`, `oscillation` (`count`, `stall_level`) | 같은 오류 집합 반복 / 예전 집합으로 되돌아감 |
| `blocked`, `blocked_resolved`, `declined`, `decline_rejected`, `same_cause_blocked` | 차단, LLM 거절, 거절 거부, 같은 원인 형제 차단 |
| `only_blocked`, `budget_exhausted`, `loop_end`, `verify_loop_end`, `stop_request`(`after` / `ignored`), `abort`, `run_aborted` | 루프·실행 종료 |
| `error_list`, `handover`, `operator_checks`, `javax_api_inventory`, `report`, `code_diff_skipped` | 마무리 |
| `job_pod_pending`, `job_deleted_before_start`, `worker_refused` | Job 모드: 빌드 파드가 못 뜨는 이유, 시작 전 멈춤의 Job 삭제, Worker 거절([09](09-deploy-and-ops.md)) |

전체 목록: `grep -rhoE '"event": *"[a-z_]+"' sbmig | sort -u`.

## 5. 종료 코드, `attn=` 코드, 차단 사유

종료 코드(`sbmig/report/result_line.py` `report_exit_code`): **0** = 최종 `mvn clean package` 성공이고 차단 없음, **2** = 끝났지만 차단·미해결
오류가 남음, **3** = 중단(`aborted[:…]` 또는 `mvn-not-found`: Maven 없음, Maven 프로젝트 아님(Ant 는 `intake.ant_message`), ZIP 풀기 한도 초과, 설정된 사내 parent 해석 불가, ADF, 멈춤 요청),
**1** = 예외(`SBMIG-RESULT … exit=1 … attn=crash`) 또는 명령 인자 오류. `--flow remediation`: 0 = 조치 SUCCEEDED, 2 = 그 밖, 3 = 시작 못 함.

`attn=` 코드는 중요한 것부터 쉼표로(`-` = 볼 것 없음): `infra:<kind>`(llm / maven / jdk / repository / file-lock / http-blocked …; 초록 실행에는 안 붙음),
`crash`, `aborted[:<reason>]`, `owner-decision`(needs_owner 차단이 있음 = 소유팀 결정 필요), `blocked:<reason>`(사유마다), `unresolved:<n>`,
같은 이름의 플래그 `restructure-failed`, `llm-budget-exhausted`, `time-budget-exhausted`, `javax-api-on-classpath`, `tests-skipped`,
`test-compile-skipped`, `tests-not-compiled`, `major-upgrade`, `jar-coordinates-unknown`, `parent-chain-no-boot-bom`, `hollow-reactor`,
`adf-project`, 그 무엇도 아니면 `incomplete`. 플릿은 `skipped:<why>` 도 쓴다.

`infra:http-blocked,aborted:http-blocked`(exit 3): Maven 3.8.1+ 의 차단 미러(`maven-default-http-blocker`)가 `http://` 저장소를 거부했다(`Blocked mirror for repositories`). 부모 맞추기·재시도·LLM 없이 멈춘다. 「확인해 주세요」 첫 항목대로 settings 를 고친다(https URL, 또는 차단 미러 `<mirrorOf>` 에 `,!<저장소 ID>`). 그 id 를 실제 주소로 재정의한 settings 의 `Could not find … in maven-default-http-blocker (<실주소>)` 는 차단이 아니다(덮인 저장소가 그 주소로 간 것; `,!ID` 로 뺀다). 저널 `maven_http_blocked`, 사전 점검 경고 `maven_http_blocker_active`.

차단 사유 = `BlockReason`(`sbmig/core/models.py`). LLM 시도 허용 횟수는 `loop.blocked_policy`:

| 사유 | 뜻 | LLM 허용 |
|---|---|---|
| `external_javax_api` | 외부 라이브러리 API 가 아직 javax 타입 | 2 (그 전에 `sbmig-jakarta` 가 저장소의 jakarta 빌드로 교체를 시도; 대체 노트 `replacement: true` 가 맞으면 차단 없음) |
| `external_api_incompatible` | 의존성 API 변경(javax 아님) | 2 |
| `artifact_not_found` | 의존성·플러그인이 어느 저장소에도 없음(저장소가 "없다" 고 답함) | 2 |
| `repository_unreachable` | 저장소가 답하지 않음(시간 초과, 연결 거부·끊김, 모르는 호스트, PKIX, 5xx; 401 / 403 아님). 저장 스크립트 재사용 전에 정해 KB 실적 없음. 저장소·네트워크를 보고 다시 실행 | 0 |
| `plugin_incompatible` | JDK 21 에서 깨지는 빌드 플러그인 | 2 |
| `parent_not_found` | parent 해석 불가. 설정 버전이면 중단(`run aborted: Maven cannot resolve the parent <g:a:v>, which is the configured version …`), 사내 부모의 as-is 버전이면 `sbmig-parent` 가 먼저 맞춘다(`openrewrite.unresolvable_asis_parent`) | 0 |
| `unsupported_source`, `encoding` | Java 아닌 소스, 인코딩 판정 불가 | 0 |
| `reactor_duplicate` | 리액터에 같은 모듈 두 번(`duplicated in the reactor`) | 0 |
| `file_lock` | Windows 에서 `target/` 이 잠김(재시도 `build.file_lock_retries` 뒤) | 0 → `infra:file-lock` |
| `needs_owner` | LLM 이 `<blocked>` 로 "프로젝트 밖의 일" 이라고 거절 | `script.max_declines_per_case` 1 |
| `repeated_failure` | 사다리 소진(LLM 요청 `script.max_llm_tries_per_case` 20, 직접 수정 `script.max_edit_tries_per_case` 4) | — |
| `oscillation` | 예전 오류 집합으로 `loop.oscillation_limit`(2) 번 되돌아감(`loop.oscillation_window` 4 안에서) | — |
| `budget_exhausted` | 실행 시간 `loop.max_total_minutes`(240) 또는 `loop.max_llm_calls`(0 = 무제한) 소진 | — |
| `llm_unavailable` | 전송·설정 실패 연속 2번(실행에서 처음이면 `llm.cooldown_seconds` 쉬고 한 번 더) | — |

## 6. 진단 순서

1. `SBMIG-RESULT` 한 줄: `exit`, `build`, `unresolved`, `blocked`, `attn=`. `infra:` 나 `blocked:repository_unreachable` 이면 코드가 아니라 환경(LLM / Maven /
   저장소 / 잠금)부터. 플래그 `framework-unchecked` = 사내 프레임워크 검사를 못 함([13](13-framework-projects.md)), 결과를 쓰기 전에 확인. 웹 증상(이미지 불일치, ZIP 거절, 507, 403)은 [09](09-deploy-and-ops.md) 10절.
2. `out/<run id>/report.md` 의 「확인해 주세요」, 「해결하지 못한 오류」, 「진행하지 않은 항목」(사유·제안), 참고 → **진단용 기록**(루프 종료 문장).
3. `out/<run id>/errors.md` 의 그 항목 `tries` 와 Tries 절(원인 / 해결 / 수정 / diff; 되돌린 시도 포함).
4. `out/<run id>/journal.jsonl`: 그 케이스의 `decision`(rationale), `attempt_scored`, `revert`, `stall` / `oscillation`, `library_hints.lines`, `declined` / `decline_rejected`.
5. 마지막 빌드 로그: `out/<run id>/logs/` 의 `NNN-build-verify*.log` 중 번호가 가장 큰 것(첫 빌드 `build-verify`, 라운드 `build-verify-2` …): `[ERROR]` 줄, `symbol:` / `location:`.
6. `project/` 의 `git log --oneline` 으로 시도 커밋과 되돌림을 짝짓고 `git show` 로 확인. 사라진 클래스는 as-is 스냅샷에서
   (루트 pom 이 없던 트리는 집계에 안 걸린 pom 이 빠졌을 수 있다 — 「확인해 주세요」).
7. LLM 기록 `llm-NNNN.json`: 프롬프트에 **사실이 빠졌나**, LLM 이 사실을 **무시했나**, **거절**인가.
8. 원인 분류(카드의 "진짜 원인" 칸):
   - **a 사실 부족** — 저장소·부모 체인·레이아웃·라이브러리 사실이 프롬프트에 없거나 틀림(예: 조회 저장소가 로컬뿐) → sbmig 개선.
   - **b LLM 반복/능력** — 같은 변경을 되풀이, 사실을 받고도 다른 접근을 못 함 → 한도·노트·프롬프트 규칙.
   - **c 프로젝트 밖** — jakarta 빌드가 정말 없음, 컨테이너 전용 API, 공급업체 결정 → 인수인계가 맞는 결말.
   - **d sbmig 버그** — 잘못된 분류·차단, 이관 누락(`hollow-reactor`), 서명 분리 등. 트리 안 jar 가 주는 패키지인데 `needs_owner` 면
     버그 — `out/<run id>/logs/bundled-jars.json` 의 `provides` 부터.
9. 로컬 검증: `project/` 의 그 모듈에 가설 수정을 넣고 `mvn -q -Dmaven.test.skip=true package`. 통하면 그 diff 가 "검증된 수정".

### 6.1 요약 카드 (15줄 안팎, 로그를 못 보는 쪽에 보내는 양식)

```
[sbmig 진단 카드]
run: <run-id> / 프로젝트: <이름> / 케이스 유형: <pure_java|servlet_tomcat|spring_legacy|spring_boot|msa> / sbmig: <git short sha>
RESULT: <SBMIG-RESULT 한 줄 그대로>
종료 사유(진단용 기록): <문장 그대로>
대상 케이스: <L2 또는 case_id> @ <file:line>
javac: <메시지 한 줄> | symbol: <...> | location: <...>
시도 요약: r1 new no_change / r2 augment regressed(reverted) / ... (총 N회, LLM M회, pre-try 있으면 표기)
토글/차단: <oscillation N회: A={오류 집합} B={오류 집합}> 또는 <차단 사유 + 제안 문장>
LLM 이 받은 사실 중 빠진 것: <Dependency versions / Previous attempts / 레이아웃 사실에서 없던 것, 또는 "없음">
LLM 응답 핵심(마지막 2회): <무엇을 바꾸려 했나 / 거절이면 그 이유>
진짜 원인: <a 사실 부족 | b LLM 반복 | c 프로젝트 밖 | d sbmig 버그> - <한 줄>
검증된 수정: <diff 요지 또는 "없음"> / 로컬 빌드: <ok|fail|안 해봄>
스크립트 초안: <있으면 def fix(p) 한 덩어리, 두 번 돌려도 변하지 않게>
sbmig 에 바라는 것: <사실 추가 / 규칙 / 한도 / 버그 수정> 한 줄
```

같은 L2 가 여러 카드에 나오면 묶어 보낸다. KB 등록(스크립트 버전·고정)은 sbmig 쪽 `sbmig kb …` 로.

## 7. 회사 PC 제약 (Windows, 복사·붙여넣기 불가)

- 파일 통째가 아니라 **특정 줄**을 묻는다: `SBMIG-RESULT`, `preflight:`, `cause=`, `KB 탐색`, `LLM-PROBE`, `REPO-PROBE`, 특정 `build N:`.
- 콘솔은 cp949 — `SBMIG-RESULT` / `LLM-PROBE` 줄은 ASCII 만이라 손으로 옮겨 적을 수 있다.
- PowerShell: `Select-String -Path out\<run id>\sbmig.log -Pattern 'SBMIG-RESULT|cause=|KB 탐색' | Select-Object -Last 20`,
  `Get-Content out\<run id>\sbmig.log -Tail 40`, `Get-ChildItem out\<run id>\logs | Sort-Object Name | Select-Object -Last 3`(`-Encoding utf8`).
- 멈춘 것 같으면: 수정 루프 감시견이 60초 조용하면 경고, 300초면 실행 폴더에 스레드 덤프(`dump-threads` 파일을 만들어도 덤프).

## 확인하는 법

```
.venv/bin/sbmig --help; .venv/bin/sbmig run --help; .venv/bin/sbmig kb --help; .venv/bin/sbmig fleet run --help
grep -n "RUN_DIR_FILES" -A2 sbmig/core/workspace.py; grep -n "L += " sbmig/report/guide_ko.py | tail -12
grep -n "| # | seen" sbmig/errors/*.py sbmig/report/*.py; grep -rhoE '"event": *"[a-z_]+"' sbmig | sort -u
grep -n "EXIT_\|def report_exit_code" -A8 sbmig/cli.py sbmig/report/result_line.py | head -40
grep -n "class BlockReason" -A30 sbmig/core/models.py; grep -n "blocked_policy" -A14 config/default.yml
grep -n "f\"fix(\|self.ws.commit(" sbmig/loop/fixloop.py sbmig/loop/orchestrator.py
.venv/bin/python -m pytest tests/docs/test_agent_doc.py -p no:cacheprovider -q
```
