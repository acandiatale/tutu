# sbmig 진단 브리핑 — 실행 로그를 보는 사내 LLM(또는 사람)용

> 2026-11-02. 용도: 회사 PC 에서 실행 로그를 볼 수 있는 **사내 LLM** 에게 이 문서와 실행 디렉터리를 주고 "왜 이렇게 끝났는지" 를
> 파악하게 한 뒤, 맨 끝의 **요약 카드(§5)** 를 채워 sbmig 개발 쪽에 전달한다. 로그를 직접 못 보는 쪽은 카드만 보고도
> 다음 수를 정할 수 있어야 한다. 더 깊은 설명은 `docs/v2/KB-AND-FLOW.md`(흐름·KB), `docs/v2/CASE-AND-STATE-KO.md`(속성 사전),
> `docs/RUNBOOK.md`(운영).

## 0. sbmig 가 무엇인가 — 한 문단

사내 Maven(Ant 는 pom 을 합성) 프로젝트를 **JDK 21 + Spring Boot 4.1** 로 옮기는 에이전트. 범위는 **빌드 통과까지**
(`mvn clean package -Dmaven.test.skip=true`, 테스트는 전부 스킵); 런타임·화면 확인은 담당팀의 몫이다. 흐름은
`intake → analyze → plan → OpenRewrite 1회 → 회사 레이아웃 재구성({name}-boot / -back / -front) → LLM 구조 점검 → 빌드 →
수정 루프 → 최종 빌드 → 리포트 + KB`. 수정 루프는 빌드 오류를 **케이스**(오류의 일반 서명 L2)로 묶고, 케이스마다 LLM 이 쓴
**파이썬 수정 스크립트**를 적용 → 재빌드로 채점 → 도움이 안 됐으면 되돌린다. 성공한 스크립트는 KB(`kb-v2/`)에 남아 다음
프로젝트에서 LLM 없이 재사용된다. 원칙: 버전은 추측하지 않는다(새 부모 체인 → 목표 Boot BOM → as-is → 저장소 순), 회사
고유값은 코드가 아니라 YAML 에, KB 는 파일, 모든 시도는 재빌드가 채점한다.

## 1. 실행 디렉터리 지도 (`<run dir>` = `out/<run-id>/` 또는 웹 업로드의 실행 폴더)

| 파일 | 무엇 | 언제 본다 |
|---|---|---|
| `report.md` / `report.json` | 사람용 보고서. 절 순서: **인수인계(Handover)** 맨 앞 → `Blocked`(차단 사유·제안) → `Fixes by case`(케이스별 시도) → 오류 목록 → `Structure review` → `Flags` → `What we changed` → `Notes` → `Outputs` → `LLM usage` (한국어 보고서면 수동 후속 조치 / 플래그 / 변경 내역 / 메모 / 산출물) | 항상 먼저. `Notes` 에 **루프가 왜 끝났는지** 한 문장이 있다 |
| `errors.md` / `errors.jsonl` | 오류 한 건 = 한 줄. 열: `# \| seen \| status \| case \| file:line \| message \| tries \| resolved`. `tries` = `n 출처 결과`(예 `3 llm-augment regressed`). jsonl 에는 시도마다 `analysis` / `solution` / `diff` / `edits` 전문 | 특정 오류에 무슨 시도가 있었는지 |
| `journal.jsonl` | 이벤트 한 줄씩 `{"event": …}` (§1.1) | 결정·채점·차단의 근거 |
| `sbmig.log` | DEBUG 로그. 단계 태그 `[fix r3]`. 핵심 줄: `KB 탐색: …`(케이스의 사다리 결정), `llm call provider=… attempt n/m`, `build N: …`, `regressed`/`reverted`, `config: … since v2.1z`(설정 경고) | 시간 순서로 따라갈 때 |
| `logs/NNN-<라벨>.log` | Maven 호출 로그(번호 순: effective-pom, rewrite, build-verify …). `[ERROR]` 줄과 javac 블록(`symbol:` / `location:`) | **실제 오류 메시지**는 여기서 |
| `logs/effective-depmgmt.json`, `logs/depmgmt-after-restructure.json` | as-is / 재구성 뒤 관리 버전 캡처 | 버전 관련 케이스 |
| `logs/review-input.md` | 구조 점검에 보낸 프롬프트(사실 절 포함) | 점검 판단이 이상할 때 |
| `project/` | 작업 복사본 = **git 저장소**(브랜치 `sbmig/<run_id>`). 커밋 하나 = 단계 또는 시도 하나: `intake: …`, `rewrite: …`, `restructure: …`, `review: …`, `fix(script:<case>:v<N> new\|augment): case:<id>`, `fix(sbmig-version\|sbmig-resources\|sbmig-jakarta): …`, 되돌림 커밋 | `git log --oneline`, `git show <sha> --stat`, `git show <sha> -- <file>` 로 시도가 실제로 무엇을 바꿨는지 |
| `changes.patch`, `result.zip` | 전체 diff, 산출물 | 담당팀 전달용 |
| LLM 기록 `llm-NNNN.json` | `.env` 의 `LLM_RECORD_DIR`(= `llm.record_dir`) 아래, 호출마다 한 파일: 요청(system/user 프롬프트 전문 — 오류, 파일 발췌, `Dependency versions` 사실 줄, `Previous attempts`)과 응답(analysis / solution / edits / script, 또는 `<blocked>` 거절) | LLM 이 **무엇을 받았고 무엇을 답했는지** |
| KB `kb.dir`(기본 `kb-v2/`) | `cases/<case_id>/case.json`(케이스 통계, `scripts[]` 버전·상태, `runs[]`), `fix_v<N>.py`, `errors/<run>-<id>.json`, `cases.md`(한눈 표), `errors.jsonl`(모든 실행) | 다른 프로젝트에서 같은 케이스가 어떻게 풀렸는지 |

### 1.1 저널 이벤트 — 진단에 쓰는 것들

| 이벤트 | 뜻 |
|---|---|
| `triage` | 라운드의 클러스터 목록과 순서(근본 원인 먼저), 가려진 클러스터 |
| `decision` | 케이스별 결정 `SCRIPT` / `BLOCKED` 와 근거(`rationale`: 사다리 단계, `KB 탐색` 결과, 차단 정책 n/m) |
| `llm_call`, `llm_script` | LLM 호출과 답(스크립트 유무, 해시) |
| `attempt`, `attempt_scored` | 시도와 재빌드 채점(`improved` / `partial` / `no_change` / `regressed` / `build_ok` / `apply_failed` / `inconclusive`) |
| `revert`, `script_rollback`, `script_rollback_undone` | 되돌림(regressed 등)과 그 취소 |
| `stall`, `oscillation` | 같은 오류 집합 반복(stall) / A↔B 토글(oscillation: `count`, `stall_level`) |
| `only_blocked`, `budget_exhausted`, `loop_end` | 루프 종료 사유 |
| `blocked`, `declined`, `decline_rejected`, `same_cause_blocked` | 차단(사유·제안), LLM 거절, 거절 거부(저장소 사실과 모순), 형제 케이스 동반 차단 |
| `library_hints`, `library_lookup`, `library_mapped`, `library_unmapped`, `jakarta_try` | 라이브러리 → 의존성 매핑, 저장소 조회(시도한 URL·상태·버전), jakarta 빌드 교체 pre-try |
| `version_try`, `resources_try` | LLM 없는 pre-try(빠진 버전 채움 / 필터링 바이너리 확장자) |
| `restructure`, `restructure_moves`, `structure_review` | 재구성 결과·이동 목록, 구조 점검 판정 |
| `aggregator_generated`, `no_root_pom`, `stray_pom_ignored`, `hollow_reactor`, `sources_outside_reactor`, `adf_project`, `adf_traces` | intake / analyze 판정(루트 pom 생성, 떠도는 pom 복사본 무시, 속 빈 리액터, ADF) |
| `run_aborted`, `report` | 중단 사유, 보고서 작성 |

## 2. 케이스와 사다리 — 진단에 필요한 만큼만

- **케이스** = 오류의 일반 서명 L2(예 `compile\|cannot_find_symbol\|FilterConfig`), `case_id = sha1(L2)[:10]`. 프로젝트가 달라도 같은 케이스면 같은 스크립트를 재사용한다. 메시지의 경로·줄·모듈 이름은 서명에 들어가지 않는다.
- **라운드마다 케이스당 시도 1회**, 순서: 저장본 재사용(LLM 0회) → 보강(이 실행의 마지막 스크립트를 늘림, 최대 `script.max_augments_per_case` 15) → 새로 작성 → 케이스당 LLM 요청이 `script.max_llm_tries_per_case` 20 에 닿으면 `repeated_failure` 로 차단.
- **채점**은 재빌드가 한다: 대상 오류가 줄면 improved / partial, 그대로면 no_change, 새 오류가 늘면 regressed → **되돌림**(diff 는 errors.jsonl 에 남는다), 빌드 성공이면 build_ok. 되돌린 시도의 실패 이유는 다음 요청의 `Previous attempts` 에 들어간다.
- **LLM 전에 sbmig 가 직접 고치는 것(pre-try, LLM 0회)**: `sbmig-version`(빠진 의존성 버전), `sbmig-resources`(필터링되는 바이너리 확장자), `sbmig-jakarta`(저장소에서 확인한 jakarta 빌드로 의존성 교체).
- **차단 사유와 허용 횟수**

| 사유 | 뜻 | LLM 허용 |
|---|---|---|
| `external_javax_api` | 외부 라이브러리가 아직 javax 타입을 요구 | 2 (`loop.blocked_policy`); 단 v2.1z-e 가 먼저 저장소의 jakarta 빌드를 찾아 교체 |
| `external_api_incompatible`, `artifact_not_found`, `plugin_incompatible` | 라이브러리 API 변경 / 저장소에 없는 아티팩트 / JDK 21 에서 죽는 플러그인 | 2 |
| `parent_not_found`, `unsupported_source`, `encoding`, `reactor_duplicate`, `file_lock` | 환경·구조 문제 | 0 (바로 Handover) |
| `needs_owner` | LLM 이 "프로젝트 밖의 일" 이라고 **거절**(`<blocked>`) | `script.max_declines_per_case` 1; 저장소 사실과 모순이면 거절을 받지 않음 |
| `repeated_failure` | 사다리 소진 | 20 |
| `oscillation` | 같은 오류 집합 A↔B 를 `loop.oscillation_limit`(기본 2) 번 오감 — 핑퐁 | 창 `loop.oscillation_window` 4 안에서 |
| `budget_exhausted` | `loop.max_llm_calls`(기본 무제한) 또는 `loop.max_total_minutes` 240분 소진 | — |
| `llm_unavailable` | 전송 실패 연속 2번(쿨다운 1회 뒤) | — |

- **루프 종료 문장**(report `Notes`): `max iterations (50) reached …` / `run time budget exhausted …` / `build N: only blocked cluster(s) remain …` / `build N: no actionable fixes (all clusters skipped/escalated/blocked/failed to apply) …`.

## 3. 진단 절차 — 이 순서로

1. `SBMIG-RESULT` 한 줄(sbmig.log 끝, 콘솔): `exit`, `build`, `unresolved`, `blocked`, `attn=` 플래그.
2. `report.md` → `Notes` 의 종료 문장 → **왜 멈췄나**.
3. `Blocked` 절 → 사유·제안·연결된 오류; `Fixes by case` → 케이스별 시도 수와 마지막 결과.
4. 대상 케이스의 **실제 오류**: 마지막 `build-verify` 로그에서 `file:line` 의 javac 메시지 + `symbol:` / `location:` 두 줄.
5. `errors.jsonl` 에서 그 항목의 `tries[]`: 시도마다 `source` / `result` / `analysis` / `solution` / `diff` — 무엇을 바꿨고 왜 그 결과로 채점됐는지. 되돌린 시도도 보인다.
6. `journal.jsonl`: 그 케이스의 `decision`(rationale), `attempt_scored`, `revert`, `stall` / `oscillation`(토글한 두 오류 집합), `library_hints` 의 `lines`(LLM 이 받은 사실).
7. LLM 기록: 해당 시도의 `llm-NNNN.json` 에서 프롬프트의 `Dependency versions` / `Previous attempts` / 레이아웃 사실 절과 응답 — **사실이 빠졌는지**, LLM 이 사실을 **무시했는지**, **거절**인지.
8. `project/` git: `git log --oneline` 으로 시도 커밋과 되돌림 짝을 보고 `git show` 로 실제 변경 확인.
9. **분류** — 카드의 "진짜 원인" 칸에 하나를 고른다:
   - **(a) 사실 부족**: 저장소·부모 체인·레이아웃·라이브러리 사실이 프롬프트에 없거나 틀림 → sbmig 개선 대상(예: `(local repository only)` 라 Nexus 의 jakarta 빌드를 못 봄).
   - **(b) LLM 반복/능력**: 같은 변경을 되풀이, 사실을 받고도 다른 접근을 못 함 → 한도·노트·프롬프트 규칙.
   - **(c) 프로젝트 밖**: 라이브러리 jakarta 빌드가 정말 없음, 컨테이너 전용 API, 공급업체 결정 → Handover 가 맞는 결말.
   - **(d) sbmig 버그**: 잘못된 분류·차단, 이관 누락(속 빈 리액터), 서명 분리 등.
10. 가능하면 **로컬 검증**: `project/` 의 해당 모듈에서 가설 수정을 넣고 `mvn -q -Dmaven.test.skip=true package` 를 돌려 통하는지 본다. 통하면 그 diff 가 카드의 "검증된 수정" 이다.

### 3.1 예시: `oscillation` @ `…Filter.java:74` (FilterConfig)

- 흔한 가설: (1) `javax.servlet.FilterConfig` ↔ `jakarta.servlet.FilterConfig` 의 import / `init(FilterConfig)` 시그니처가 시도마다 번갈아 바뀜(`@Override` 와 상위 타입 불일치); (2) 필터가 상속·호출하는 **라이브러리** 클래스가 javax `FilterConfig` 를 요구(사실상 `external_javax_api` — 라이브러리의 jakarta 빌드가 필요, v2.1z-e 가 저장소를 찾는다); (3) sbmig 가 web.xml 의 필터를 생성 클래스 `WebXmlRegistrations` 로 등록하면서 init-param / FilterConfig 사용이 달라짐; (4) 두 케이스가 같은 파일을 번갈아 고침.
- 확인할 것: javac 메시지 전문; 필터의 import 와 `init` 시그니처; 상위 클래스/인터페이스가 **어느 jar** 에서 오는지(`mvn dependency:tree -Dincludes=<g>`); 시도별 diff 가 정확히 무엇을 토글했는지; `oscillation` 이벤트의 두 오류 집합.

## 4. 검증된 수정을 다음 실행에 재사용시키기

KB 등록(스크립트 버전·케이스 연결)은 sbmig 쪽에서 한다. 사내 LLM 은 카드에 **검증된 diff** 와, 가능하면 아래 꼴의 **스크립트 초안**을
넣어 주면 된다(파일 하나, `def fix(p)` 하나, 두 번 돌려도 변하지 않게):

```python
def fix(p):
    for path in p.files("**/*Filter.java"):
        n = p.replace(path, "import javax.servlet.FilterConfig;", "import jakarta.servlet.FilterConfig;")
        if n:
            p.log(f"{path}: FilterConfig import → jakarta ({n})")
```

헬퍼: `p.files(glob)`, `p.exists`, `p.read`, `p.write`, `p.replace(path, old, new)`, `p.sub(path, 정규식, repl)`, `p.create`, `p.delete`,
`p.move`, `p.pom(path).add_dependency / remove_dependency / add_managed_dependency`, `p.log`. 프로젝트 폴더 밖과 허용되지 않은
import 는 막힌다. 버전·좌표는 사실(부모 체인·BOM·저장소)에서만, 추측 금지.

## 5. 요약 카드 — 사내 LLM 이 채워서 전달하는 양식 (15줄 안팎)

```
[sbmig 진단 카드]
run: <run-id> / 프로젝트: <이름> / 케이스 유형: <pure_java|servlet_tomcat|spring_legacy|spring_boot|msa> / sbmig: <git short sha>
RESULT: <SBMIG-RESULT 한 줄 그대로>
종료 사유(Notes): <문장 그대로>
대상 케이스: <L2 또는 case_id> @ <file:line>
javac: <메시지 한 줄> | symbol: <…> | location: <…>
시도 요약: r1 new no_change / r2 augment regressed(reverted) / … (총 N회, LLM M회, pre-try 있으면 표기)
토글·차단: <oscillation N회: A={오류 집합} B={오류 집합}> 또는 <차단 사유 + 제안 문장>
LLM 이 받은 사실 중 빠진 것: <Dependency versions / Previous attempts / 레이아웃 사실에서 없던 것, 또는 "없음">
LLM 응답 핵심(마지막 2회): <무엇을 바꾸려 했나 / 거절이면 그 이유>
진짜 원인: <a 사실 부족 | b LLM 반복 | c 프로젝트 밖 | d sbmig 버그> — <한 줄>
검증된 수정: <diff 요지 또는 "없음"> / 로컬 빌드: <ok|fail|안 해봄>
sbmig 에 바라는 것: <사실 추가 / 규칙 / 한도 / 버그 수정> 한 줄
```

카드 한 장이면 로그 없이도 다음 증분(사실 추가, 규칙, 한도, 수정)을 정할 수 있다. 같은 모양이 여러 프로젝트에서 반복되면
카드마다 "대상 케이스" 가 같은 L2 로 나오므로 묶어서 보낼 수 있다.

## 6. 자주 만지는 설정 키 (회사 yml 에서 덮어쓴다)

| 키 | 기본 | 뜻 |
|---|---|---|
| `loop.max_total_minutes` | 240 | 실행 전체 시간 한도(넘으면 남은 케이스 `budget_exhausted`) |
| `loop.phases.verify.max_iterations` | 50 | 수정 라운드 수 |
| `script.max_augments_per_case` / `script.max_llm_tries_per_case` | 15 / 20 | 케이스당 보강 횟수 / LLM 요청 총수 |
| `script.max_declines_per_case` | 1 | LLM 거절 허용 횟수(그 뒤 `needs_owner`) |
| `loop.blocked_policy.<사유>` | 2 또는 0 | 사유별 LLM 허용 횟수 |
| `loop.oscillation_limit` / `loop.oscillation_window` | 2 / 4 | 핑퐁 차단 임계값 / 비교 창(창을 넓히면 더 빨리 잡힌다) |
| `llm.max_retries` / `llm.cooldown_seconds` / `llm.timeout_seconds` | 3 / 60 / 180 | 전송 재시도 / 연속 실패 뒤 쿨다운 / 호출 타임아웃 |
| `build.repository_url` | null | sbmig 의 저장소 조회 URL(명시하면 settings 미러보다 우선; `sbmig repo-probe <g:a>` 로 확인) |
| `libraries.discover_jakarta_builds` | true | jakarta 빌드 탐색 + LLM 전 교체 |
| `intake.adf_policy` | stop | ADF 프로젝트는 분석 단계에서 종료 |
