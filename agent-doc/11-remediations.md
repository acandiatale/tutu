# 취약점 조치 (remediations)

> 다루는 것: 이관에 이어 빌드 파드에서, 또는 실행 방식 `remediation` 으로 따로 도는 두 번째 기능(별도 그래프) — Maven 프로젝트를 IVAS 로 검사하고 취약한 라이브러리의 **버전 글자만** 고쳐(예외 하나: 관리 버전 override 항목) 다시 빌드·재검사하는 흐름. 노드·상태·운영자 할 일·API·진단·설정 키·고칠 자리.
> 언제 읽나: 이관 실행 폴더의 `remediation/` 이나 `/api/remediations` 작업이 왜 그렇게 끝났는지 볼 때, 조치 규칙·IVAS 연동을 바꾸기 전에.
> 함께 읽을 파일: [00-what-sbmig-is.md](00-what-sbmig-is.md), [07-config-and-knowledge.md](07-config-and-knowledge.md), [09-deploy-and-ops.md](09-deploy-and-ops.md)

## 1. 무엇을 하나

입력은 Maven 프로젝트 ZIP, 출력은 결과 ZIP(`result.zip`)과 보고서. 순서: 기준 빌드 → IVAS 기준 검사 → 발견 항목을 Maven 이 고른
의존성과 pom 의 버전 줄에 연결 → 새 버전 제안 → 격리된 후보 사본에 적용 → 후보 빌드·검증 → 전달 ZIP 고정 → **같은 바이트** 재검사 → 판정 →
보고서. 코드는 `sbmig/remediations/`, 이관 KB 와 섞이지 않고 LLM 을 부르지 않는다. 이관 그래프(`sbmig/graph/`)와는 노드 하나로 이어진다(1.1, 따로 돌 때는 1.2).

### 1.1 어디서 도나 — 빌드 파드, 이관에 이어서

- 이관이 **BUILD SUCCESS** 로 끝나고 `remediation.after_migration` 이 true 면, 이관 그래프의 `remediation` 노드(`finalize` 뒤)가 같은
  `sbmig run` 프로세스 안에서 조치 그래프를 부른다(chain.py 의 `run_after_migration`). 코드 기본값은 false,
  매니페스트는 ConfigMap `sbmig-env` 에 `SBMIG_REMEDIATION__AFTER_MIGRATION: "true"` 로 켜서 내보낸다(두 파드가 같은 값이어야 환경 대조 통과 — 09).
- 파일은 **`<이관 실행 폴더>/remediation/`**, 조치 id = 이관 run id. 체인이 이관된 프로젝트 트리로 ZIP 을 만들어 접수(`intake.accept_zip`)에 넣는다.
- 이관과 **같은 빌드**: 최종 빌드 대상(`verify.build` 의 target; 루트 한 번이면 빈 목록)을 `state.build_targets` 로 받아 같은 방식으로 빌드·해석
  (루트 aggregator 없는 MSA = 서비스마다, 루트 pom 없이 접수). 명령 앞부분·settings·로컬 저장소·환경(IVAS_/LLM_ 만 뺌)이 같고, 캐시된 해결 실패는 `-U` 로 한 번 더
  (`remediation_negative_cache_retry`), 줄마다 `remediation mvn … (cwd=…)`. 이관은 통과했는데 baseline 이 실패하면 메시지가 그 사실로 시작한다
  (`remediation_baseline_differs`; 빌드 사본에는 `.git/` 이 없다).
- 조치의 결과는 이관의 성공 판정·종료 코드·보고서 판정을 **바꾸지 않는다**. `SBMIG-RESULT … remediation=<상태|skipped>`.
  IVAS 세 값이나 Maven 준비가 없으면 건너뛰고 저널에 `remediation_skipped`(이름만). `remediation.trusted_projects` 는 묻지 않는다.
- 기본 설정은 모든 제안을 승인 없이 적용하므로 조치가 이관과 같은 파드에서 끝난다(3절). 빌드 파드는 사람을 기다리지 못한다:
  `AWAITING_CONFIRMATION`, 승인을 켠 때의 `AWAITING_APPROVAL` 이면 상태를 저장하고 끝난다. Job 모드의 resume 은
  **새 빌드 파드**(같은 Job 틀·실행 폴더, Worker 동작 `remediation-resume` → `chain.resume`)이고 업로드와 같은 FIFO 줄·자리를 기다린다.
  줄에 세우기 전에 앞 시도의 `remediation-job-status.json` 을 지우고, 시도 번호를 Job env `SBMIG_JOB_ATTEMPT` 로 넘긴다(Worker 는 모든 상태 파일에 적는다).
  확인 대기면 웹 파드가 `confirm` 등을 Job 을 만들기 **전에** `state.json` 에 쓴다.
- 웹 파드는 상태·제안·승인·파일만 맡는다. 단독 업로드 `POST /api/remediations` 는 `in-process` 모드만(폴더 `<웹 out>/_remediations/<id>/`), Job 모드는 409 `not_available`.
- Job 파드의 IVAS 세 값은 Secret `sbmig-llm` 에서(81 틀 `secretKeyRef`, optional), 환경 대조 지문에 없다. `IVAS_USER_ID` 도 비밀로 지운다(`SECRET_NAMES`).
- Maven 로컬 저장소는 그 파드의 자리 저장소(`repository-slot<k>`). `<local repo>-remediation` 은 in-process 의 단독 업로드와 이어 돈 조치의 재개에.
- IVAS 로그인은 파드마다 해도 된다.
- 시간: Job 기한(4시간)은 이관과 조치가 나눠 쓴다. `remediation.chain_budget_minutes`(90, 0 = 없음)만큼 돌면 다음 경계에서 STOPPED
  **`chain_budget`**(chain.py 의 `R_CHAIN_BUDGET`, 저널 `remediation_budget`). 재개는 새 파드에서 다시 재고, 검사 중이었으면 영수증으로 이어 조회.

### 1.2 취약점 해결만 — 실행 방식 `remediation`

- 이미 이관된 프로젝트를 다시 점검할 때: `sbmig run <zip|폴더> --flow remediation`, 웹 `POST /api/runs?flow=remediation`, 화면의
  **취약점 해결**. Job 모드에서는 같은 줄·자리의 보통 실행(Job env `SBMIG_FLOW` 는 웹 파드가 넣는다).
- 엔진 노드 `intake → remediation → finalize`. 접수는 프로젝트를 고치지 않는 데까지만(사전 점검, 빌드 도구 판별과 거절), 이관 단계·LLM·KB 없음.
  `chain.run_only` 가 올린 ZIP 바이트(폴더는 그대로 묶어)를 접수에 넣는다. 올린 그대로 빌드되지 않으면 검사 전에 FAILED `baseline_build_failed`.
- `remediation.after_migration` · `remediation.trusted_projects` 는 묻지 않는다. IVAS 세 값·Maven 준비가 없으면 시작하지 않는다(exit 3,
  플래그 `remediation:not-ready`, 저널 `remediation_skipped` 에 이름만).
- 폴더·상태·승인·재개는 1.1 과 같다. 결과 줄 `SBMIG-RESULT … flow=remediation … remediation=<상태>`, 종료 코드 0 = SUCCEEDED, 2 = 그 밖의
  저장 상태, 3 = 시작 못 함. 실행의 `report.md` 는 짧다(이관 안 함 한 줄 + 조치 절 + `remediation/report.md` 경로). `--stop-after` 는 못 쓴다.

절대 규칙(회사 결정):

- 바뀌는 것은 **기존 pom.xml 안의 버전 글자뿐**이다. 소스·구조·다른 파일은 그대로, 결과 ZIP 의 파일 집합 = 원본의 것.
  변경 하나(`TextEdit`, `op="replace"`) = pom 한 줄에서 버전 글자 `old` → `new`(태그·`${…}` 없음). 인코딩·BOM·CRLF·마지막 줄바꿈도 그대로 둔다.
- **예외는 하나**: `managed_override` 제안은 대상 pom(루트 `pom.xml`, 루트를 상속하지 않는 모듈은 그 모듈 pom)의 최상위 `<dependencyManagement><dependencies>` 안에 `<dependency>` 항목 하나
  (groupId / artifactId / version, 변종이면 type·classifier 도)를 넣는다. 그 절이 없으면 그 항목 하나를 담은 `<dependencyManagement>` 절 전체를 최상위 `<dependencies>`
  → `<build>` → `</project>` 중 처음 있는 줄 앞에 넣는다. `TextEdit` 의 `op="insert"`, `old=""`, `line`(이 줄 앞에), `anchor`(그 줄의 지금 글자,
  적용할 때 다시 확인), `override`(그 좌표). 넣는 글자는 `render_override` 의 모양과 정확히 같다.
- "계획한 것만 바뀌었나"(`verify_text_only`): 나머지 파일은 바이트가 같고, 받은 pom 은 원본에 계획한 replace·insert 를(같은 줄 앞은 제안 ID 순)
  적용한 바이트와 같고, XML 구조는 계획한 override 요소만큼만 는다.
- 원본 ZIP·파일 목록(manifest)·작업 트리 `accepted/` 는 따로. 변경은 후보 사본에 먼저 하고, 빌드되고 해석이 계획과 같을 때만 `accepted/` 로
  확정한다. `target/` 은 해시·복사·전달 어디에도 없다(manifest 경로만).
- IVAS 업로드 결과가 불확실하면 **다시 올리지 않는다**. 미완료 검사·빠진 쪽·중복·ZIP 해시 불일치는 "0건" 이 아니다.
- 새 버전의 근거는 서버 정책(`remediation.policy.pins`) 또는 승인된 저장소가 실제로 가진 버전뿐이다. IVAS 의 patched 버전은 하한 힌트일 뿐이고,
  pin 이 없으면 힌트가 있어야 제안한다(없으면 `no_candidate_version`).
- 서버가 다시 떠도 자동 재개·재전송은 없다(`INTERRUPTED` + 명시적 resume). 상태를 진행시키는 것은 `remediation/claim.json` 을 가진 프로세스
  하나뿐(`RunClaim`, 30 초 갱신, 600 초 갱신 없으면 넘겨받음, 둘째는 거절).
- IVAS 아이디·비밀번호·토큰, Nexus 자격증명은 코드·설정·로그·보고서·예외 문장에 쓰지 않는다.

## 2. 흐름 — 노드별

그래프는 `sbmig/remediations/graph.py`(`run_remediation`). 노드 경계마다 `state.json` 저장, 재개는 `state.next_node` 부터.
중지 요청은 노드 사이와 빌드·IVAS 조회 중에 본다.

| 노드 | 하는 일 | 다음 / 멈춤 |
|---|---|---|
| `baseline_build` | Maven 준비 확인(없으면 FAILED `not_ready`), 실행 경계 검사(refuse 하나라도 → FAILED `execution_boundary_refused`), 사본에서 `mvn clean package -Dmaven.test.skip=true` | 실패 → FAILED `baseline_build_failed` |
| `baseline_scan` | 원본 ZIP sha256 재확인, IVAS 검사(라벨 `baseline`) | 조치 대상 0건(완전한 검사) → 원본 바이트 사본이 전달 ZIP, `evaluate`. 미완료 → INCOMPLETE `scan_incomplete`. 업로드 불확실 → AWAITING_CONFIRMATION |
| `inventory` | 빌드 대상마다 `dependency:tree`, 발견 항목 → `Inventory`(출처 판정) | 해석·인벤토리 불완전 → INCOMPLETE `manual_review` |
| `plan` | 제안·잔여, 승인 부류, 해시 세 개. 트리·정책 해시가 바뀐 옛 계획은 `rounds/<n>/plan.stale.json` 으로 두고 새로 만든다 | 승인 대기 제안(승인을 켠 때) → AWAITING_APPROVAL. 적용할 것 없음 → `evaluate` |
| `apply` | `accepted/` → 후보 사본, 자동+승인 제안 적용, `rounds/<n>/candidate.patch`, "계획한 것만 바뀌었나" 검사 | 위반 → FAILED `text_only_violation`(결과 ZIP 에 넣지 않음) |
| `build` | 후보 빌드, 다시 해석, 산출물 안 jar 목록, "해석 결과 = 계획" 검사(적용 범위), 통과하면 `accepted/` 로 확정. `candidate.ready.json` 이 없으면 `apply` 로 | 빌드 실패 → FAILED `candidate_build_failed`(include 면 실패한 수정본이 결과 ZIP, 재검사 없음), 불일치 → FAILED `resolution_mismatch` |
| `freeze_delivery` | `accepted/` 로 결정적인 전달 ZIP(`delivery/result.zip`) | — |
| `rescan` | 전달 ZIP sha256 재확인, 같은 바이트를 IVAS 검사(라벨 `final-r<n>`) | 미완료 → INCOMPLETE, sha 다름 → FAILED `zip_hash_mismatch`, 불확실 → AWAITING_CONFIRMATION |
| `evaluate` | 조치 대상 취약점이 남았고 이번 라운드가 확정됐고 라운드 < `remediation.max_rounds` → 다음 라운드 | `inventory` 로 돌아가거나 성공 판정 |
| `report` | 보고서 두 개(끝 상태에서만) | 끝 |

검사 기록: 올리기 전 `state.pending_upload[라벨]`, 영수증(프로젝트·분석 ID)을 받는 즉시 `state.pending_receipt[라벨]`. 재개 때 영수증이
있으면 올리지 않고 이어서 조회, `pending_upload` 만 있으면 확인 없이는 다시 멈춘다.
파일 컴포넌트 ID 에는 `|` 가 있을 수 있다(`11560|934982`): matchedfiles 경로 조각 하나로 인코딩(`11560%7C934982`).
ID 는 `fileComponent.id`, 없을 때만 `ossId`(메모).
요약 대조(`remediation.ivas_summary_check`): `exact`(기본) = 요약 `components.total` == 모든 쪽의 라이브러리
취약점 행 수(중복 제거 전), 다르면 `summary_mismatch`. `swComponents` · `fileComponents` 는 0 / 0 아님만. `nonzero` = components 도 그렇게.

**조치 구분 코드**(`action.actionDivCd`, 뜻은 ivas.py 의 `ACTION_MEANINGS`): NE 조치 필요, NP 조치계획 미등록, PL 조치예정,
OT 조치일 경과, PC 패치 완료, IG 조치 예외. `remediation.ignore_actions`(기본 비어 있음 = 모두 조치 대상; 예 `[PC, IG]`; 공백·대소문자 무시)의
코드는 검사 파일과 보고서 표 "조치 대상에서 제외한 항목"에만 남고 인벤토리·계획·남은 취약점에서 빠진다(`counts` 의 `*_findings` 는 조치 대상만,
`*_ignored` 따로). 빈·모르는 코드는 조치 대상, 한 번 기록(저널 `ivas_action_unknown`).

**SUCCEEDED 조건**(전부): 기준 빌드 통과, 적용한 라운드는 모두 빌드·검증·확정, 최종 검사 완전, 전달 ZIP sha256 = 최종 검사 ZIP sha256,
전달 ZIP 의 `build_ok`(실패한 수정본 아님)·`scanned`(완전한 최종 검사가 이 바이트를 봄) 참, pending·rejected 제안 없음, 잔여(수동 검토·출처 불명·
deny·후보 없음·적용 안 된 제안) 없음, 최종 검사에 조치 대상 없음. 아니면 INCOMPLETE / FAILED(이유는 `state.reasons`; `build_ok` 거짓 →
`candidate_build_failed`, `scanned` 거짓 → `zip_hash_mismatch` 또는 `scan_incomplete`).

**실패한 후보**(`remediation.failed_candidate`, 값 `include`(기본) 또는 `discard`):

- `include`: 후보 **빌드**가 실패하면 그 수정본이 `delivery/result.zip`(`build_ok=false`, `scanned=false`, 재검사 없음), FAILED
  `candidate_build_failed`. `accepted/`·원본 ZIP·`candidate.patch`·`build.log` 는 남고, `report.md` 는 "빌드가 실패한 수정본" 경고로 시작.
  앞 라운드의 검증된 전달 ZIP 은 `delivery/result-r<k>-verified.zip`(+ `.json`)으로 옮겨 두고 내려받기 이름은 `result-verified.zip`.
- `discard`: 실패한 후보는 결과 ZIP 에 넣지 않는다(변경은 `candidate.patch` 로만). 앞 라운드의 검증된 전달 ZIP 이 있으면 그것이
  `result.zip`, 없으면 전달 ZIP 없음.
- 어느 값이든 `text_only_violation`·`resolution_mismatch` 후보는 결과 ZIP 에 넣지 않는다.

## 3. 승인 규칙

기본 설정(`remediation.require_approval` false, `remediation.always_approve` `[]`)은 **모든 제안을 승인 없이 적용**하고 승인 대기로 멈추지
않는다(프로젝트 팀은 결과만 원한다). 제안은 `require_approval` 이 true 이거나 위험 부류(`risks`) 하나가 `always_approve` 에 있을 때만 기다린다(`decision` `pending`, 아니면 `auto`). 부류: `major`(메이저 변경),
`parent`(`<parent><version>`), `bom`(가져온 BOM 버전), `managed_override`(넣는 관리 항목).
사람 대신 남는 안전장치: 격리된 후보, 후보 빌드(깨는 변경 묶음은 통째로 FAILED `candidate_build_failed`),
효과 없는 override 항목 제거, 전달 ZIP 재검사. 위험 부류가 있는 적용 변경은 보고서의 「주의」 칸과 할 일에 나온다(12).

승인 켜기: 모든 제안 = ConfigMap `sbmig-env` 의 `SBMIG_REMEDIATION__REQUIRE_APPROVAL: "true"`(웹 파드 재시작). 부류만 = 이미지에 굽는
회사 yml 의 `remediation.always_approve`(예 `[major, parent, bom, managed_override]`). `always_approve` 는 env 줄로 넣지 않는다(81 틀에 없어 Job 이 거절된다).
부모·BOM 버전 변경은 그 부모·BOM 자체가 `remediation.policy.pins` 에 있을 때만 제안된다(`parent_version` / `bom_version`, override 아님).

**관리 버전 override**: `transitive` · `parent_managed` · `bom_managed` 항목은 (관리자 pin 이 없으면) 직접 의존성과 같은 규칙(1절의 근거,
같은 메이저 우선, deny·`remediation.policy.allow_major`)으로 버전을 고르고, 대상 pom 에 항목 하나를 넣는 `managed_override` 제안을
(pom, g:a·변종)마다 하나씩 낸다(`remediation.module_overrides` false 면 모듈은 잔여). 그 관리 항목이 그 pom 에 이미 있으면 넣지 않는다: 최상위
하나이고 버전 글자(또는 프로젝트 속성)가 해석된 버전과 같으면 그 글자를 바꾸고(`own_management_version` / `property_version`), 아니면 잔여.
제안의 `note` 는 `report.md` 제안 아래.

제안하지 않는 것(잔여·수동 검토): `plugin`, `vendored`, `unmapped`(꺼진 프로필에만 있는 선언, 한 `<dependencies>` 에 두 번 선언된 g:a 포함),
`remediation.policy.deny` 에 걸린 것, `remediation.policy.allow_major` 가 false 일 때의 메이저. 잘못된 버전을 막는 규칙 — 공유 버전 property 는 쓰는
아티팩트 모두에 목표 버전이 있을 때만, 다른 메이저는 IVAS 힌트가 그 메이저일 때만, 날짜 버전(첫 숫자 > 9999)은 후보 아님, 힌트를 이미 만족하면 안 올림 — 은 12 의 5절.

승인 요청은 **제안 해시 · 원본 트리 해시 · 정책 해시 · diff** 가 저장된 제안과 같고, 지금의 `accepted/` 와 정책에 맞고(낡지 않음), 아직
`pending` 일 때만 받는다. 하나라도 다르면 요청 전체가 409 `approval_mismatch`(+ `fields`), 아무것도 쓰지 않는다.
정책 해시 = `remediation.policy` 전부 + `require_approval` + `always_approve` + `failed_candidate` + `ignore_actions` + 사내 g:a(+ 프레임워크 패턴, 12) + false 일 때의 `module_overrides`. 하나만 바꿔도 열린 제안은
모두 무효, 계획을 다시 만든다(`ivas_summary_check` 는 안 들어감). 제안 해시는 edit 전부(`op` · `anchor` · `override` 포함)를 덮는다. 거절한 (g:a, 버전)은 그 작업에서 다시 제안하지 않는다(잔여 `rejected_proposals`, 다음 실행은 다시 제안). `by` 는 비워도 된다. 화면의 승인은 체크박스 목록과
`선택한 N건 적용`: 체크 = approved, 안 함 = rejected, 이어서 재개(`"resume": true`). 정책이 바뀐 뒤 누른 실행(`policy_changed`)도 같다(다시 기다리지 않음).

## 4. 상태와 운영자가 할 일

| 상태 | 뜻 | 할 일 |
|---|---|---|
| `QUEUED` / `RUNNING` | 대기 / 실행 중(단독 업로드는 한 번에 한 건) | 기다린다. 멈추려면 stop |
| `AWAITING_APPROVAL` | 승인 대기 제안이 있다(승인을 켠 때만) | 화면의 체크박스 + 적용 버튼, 또는 `GET …/proposals` → `POST …/approvals`(3절) |
| `AWAITING_CONFIRMATION` | 업로드를 보냈는데 결과를 모른다 | IVAS 화면에 그 프로젝트(`<앞머리>-<작업 ID>-<라벨>`)의 분석이 있나 본다. 없으면 resume `{"confirm": "reupload"}`, 있으면 `{"confirm": "receipt", "label", "project_id", "analysis_id"}` |
| `STOPPED` | 중지 요청을 경계에서 처리(원격 IVAS 분석은 취소 안 됨) | resume |
| `INTERRUPTED` | 서버가 실행 중에 내려갔다 | 상태를 보고 resume(자동 재개 없음) |
| `SUCCEEDED` | 성공 조건 전부 | `result.zip` 전달 |
| `INCOMPLETE` | 끝났지만 성공 아님 | 보고서의 잔여 표를 담당 팀에 |
| `FAILED` | 빌드·검증 실패, 경계 거부, 준비 안 됨, 내부 오류 | 이유 코드대로(6절). `delivery.build_ok` false 면 `result.zip` 은 실패한 수정본(배포 금지) |

이유 코드(문자열 그대로가 API 계약): `baseline_build_failed`, `candidate_build_failed`, `scan_incomplete`, `upload_uncertain`, `zip_hash_mismatch`,
`text_only_violation`, `resolution_mismatch`, `execution_boundary_refused`, `not_ready`, `pending_approval`, `rejected_proposals`,
`residual_findings`, `manual_review`, `no_candidate_version`, `rounds_exhausted`, `stop_requested`, `internal_error`, `policy_denied`,
`unknown_provenance`, `stale_location`, `inventory_incomplete`, `inhouse_artifact`(12). 정의는 `sbmig/remediations/models.py`(이관에 이어 돈 조치만 `chain_budget`, chain.py).

## 5. HTTP API (웹 파드, TokenGuard 뒤 — 다른 사이트의 요청은 403; 이어 돈 조치의 id 는 이관 run id)

| 경로 | 결과 |
|---|---|
| `POST /api/remediations?name=<file.zip>` (본문 = ZIP) | `in-process` 모드만: 202 `{id, status, url}` / 400 접수 거부(코드) / 413 크기 초과 / 503 `not_ready` + `missing`. Job 모드는 409 `not_available` |
| `GET /api/remediations?page=&size=` | 목록(최신 먼저) |
| `GET /api/remediations/readiness` | `{ready, missing}`(이름만) |
| `GET /api/remediations/<id>` | 상태 dict(status, reason, reasons, node, next_node, round, counts, pending_approvals, delivery `{sha256, size, round, build_ok, scanned}`, files, links …) |
| `GET /api/remediations/<id>/result` | 끝 상태면 report.json(`delivery`, `verified_delivery` 포함), 아니면 409 `not_finished` |
| `GET /api/remediations/<id>/proposals` | 지금 라운드의 제안(diff, 해시 세 개, 결정)과 잔여. `?all=1` 은 모든 라운드 |
| `POST /api/remediations/<id>/approvals` | 결정 목록 → `{accepted, pending}` / 409 `approval_mismatch` / 409 `state_conflict`. `"resume": true` 면 이어 재개, 답에 `resumed` 또는 `resume_error`(결정은 남음) |
| `POST /api/remediations/<id>/resume` | 재개(확인 대기면 `confirm` 필수). Job 모드의 이어 돈 조치는 새 빌드 파드 |
| `POST /api/remediations/<id>/stop` | 다음 경계에서 멈춤 |
| `GET /api/remediations/<id>/files/<name>` | 정해진 파일만: 보고서 두 개, result.zip, source.zip, manifest.json, journal.jsonl, remediation.log, delivery.json, `result-verified.zip`(앞 라운드의 검증된 ZIP, 없으면 404), `plan-r<n>.json`, `candidate-r<n>.patch`, `build-r<n>.log`, baseline-build.log, `scan-<label>.json` |
| `DELETE /api/remediations/<id>` | 원본까지 폴더 삭제(실행 중이면 409) |

## 6. 진단 순서

실행 폴더: 이관에 이어 돌면 `<이관 실행 폴더>/remediation/`(쿠버네티스 `/data/out/<run id>/remediation/`), 단독이면 `out/_remediations/<id>/`.
조치가 없으면 이관 `journal.jsonl` 의 `remediation_skipped`.

1. **`state.json`** — `status`, `reason`, `reasons`, `message`, `node`(마지막), `next_node`(재개 지점), `round`, `pending_upload` /
   `pending_receipt`(검사 중 멈춘 자리), `boundary`(경계 검사), `residuals`.
2. **`journal.jsonl`** — `node_start` / `node_end`, `scan_receipt`, `scan_resume`, `upload_uncertain`, `scan_stopped`, `scan_done`,
   `ivas_upload`, `ivas_status`, `ivas_pages`, `ivas_scan_done`, `plan_written`, `plan_reused`, `plan_stale`, `policy_hash_differs`,
   `round_committed`, `next_round`, `maven_project_config`, `ivas_action_unknown`, `remediation_budget`, `failed_candidate_delivered`, `verified_delivery_kept`, `earlier_delivery_kept`.
   결과 ZIP 의 정체는 `delivery/delivery.json` 의 `build_ok` · `scanned`.
3. **remediation.log** — 로그(비밀 없음; 이어 돈 조치·`--flow remediation`·새 파드 재개 모두). Maven 원문은 `baseline/build.log`, `rounds/<n>/build.log`(대상이 여럿이면 `build-<대상>.log`).
4. **`scans/<label>.json`** — 검사 하나: `complete`, `incomplete_reasons`(`analysis_failed`, `analysis_timeout`, `page_missing`, `total_mismatch`,
   `duplicate_result`, `summary_mismatch`, `unsupported_sw`, `zip_hash_mismatch`, `id_mismatch`, `http_error`, `stop_requested`), `findings`, `counts`, `notes`.
5. **`rounds/<n>/`** — `before.json`(해석 결과), `inventory.json`(항목별 `origin`, `location`, `reason`), `plan.json`(제안·잔여), `apply.json`
   (적용·건너뜀), `candidate.patch`, `resolved.json`(후보 해석 결과).
6. **`approvals.jsonl`** — 승인·거절 기록.

자주 보는 경우: `not_ready` → readiness 의 `missing`(`remediation.trusted_projects`, IVAS 세 값, JDK 21, Maven, settings.xml, 승인된 저장소 호스트). `execution_boundary_refused` →
`state.boundary` 의 `code`(`project_maven_config`, `maven_extensions`, `maven_wrapper`, `symlink`, `foreign_repository`,
`foreign_plugin_repository`, `unparsable_pom`). `manual_review` 만 남음 → `inventory.json` 의 `origin` 이 `plugin` / `vendored` / `unmapped` 이거나,
`transitive` / 부모·BOM 관리인데 override 를 막은 사실이 잔여의 `detail` 에(남은 classifier, 루트 pom 비상속·없음, 직접 선언 버전, 넣을 줄
없음, 이미 있는 관리 항목). `resolution_mismatch` → 제안의 적용 범위 모듈이 계획 버전이 아니다(12 의 4절). `no_candidate_version` → 잔여의 `detail` 에 저장소가 준 버전 목록과 제외 이유(힌트 없음 포함)가 있다.

## 7. 설정 키 (`remediation:` 절, `config/default.yml` 에 한국어 설명)

우선순위: `config/default.yml` → 회사 yml → 환경 변수 `SBMIG_REMEDIATION__<키>`. 비밀은 환경 변수(쿠버네티스 Secret)에서만:
`IVAS_BASE_URL`, `IVAS_USER_ID`, `IVAS_PASSWORD`. Nexus 자격증명은 서버 settings.xml 의 `<server>`(`${env.NEXUS_USER}` 등).

| 키 | 뜻 |
|---|---|
| `remediation.max_upload_mb` / `remediation.max_entries` / `remediation.max_unpacked_mb` / `remediation.max_entry_mb` / `remediation.max_compression_ratio` | ZIP 한도(압축률은 1 MiB 이상 풀리는 항목만) |
| `remediation.after_migration` | 이관이 BUILD SUCCESS 면 같은 빌드 파드에서 조치를 이어 돌릴지(코드 기본 false, 매니페스트 true) |
| `remediation.trusted_projects` | (in-process 단독 업로드만) Maven 이 올라온 프로젝트의 플러그인을 돌리므로 운영자가 true 로 바꿔야 접수(기본 false) |
| `remediation.require_approval` | false(기본) = `always_approve` 부류만 승인 대기, true = 모든 제안 |
| `remediation.always_approve` | 승인을 기다릴 부류(기본 `[]` = 없음; major, parent, bom, managed_override) |
| `remediation.max_rounds` | 계획 → 적용 → 빌드 → 재검사 반복(기본 3) |
| `remediation.failed_candidate` | 후보 빌드 실패: `include`(기본, 실패한 수정본도 결과 ZIP) / `discard`. 2절 |
| `remediation.ignore_actions` | 계획에서 뺄 IVAS 조치 코드(2절, 정책 해시에 들어감) |
| `remediation.chain_budget_minutes` | 이어 돈 조치의 파드당 시간 한도(기본 90분, 0 = 없음) |
| `remediation.project_maven_config` | `.mvn/maven.config` · `jvm.config` · `extensions.xml` 이 있을 때 ignore(기본, 기록만) / refuse. 빌드는 그 폴더를 읽지 않는다(`build.ignore_project_mvn`) |
| `remediation.maven_wrapper` | `mvnw` 가 있을 때(서버 Maven 만 쓰므로 기본 ignore) |
| `remediation.allowed_repository_hosts` | 승인된 저장소 호스트. 비우면 서버 settings.xml 의 미러·저장소 호스트 |
| `remediation.foreign_repository` | pom 이 승인 안 된 호스트 저장소를 선언할 때 |
| `remediation.build_timeout_minutes` | package 빌드 한 번 한도 |
| `remediation.ivas_timeout_seconds` / `remediation.ivas_upload_timeout_seconds` | IVAS 요청 / 업로드 한도 |
| `remediation.ivas_poll_seconds` / `remediation.ivas_analysis_timeout_minutes` | 상태 조회 간격 / 분석 대기 한도(기본 30분, 넘으면 미완료) |
| `remediation.ivas_page_size` / `remediation.ivas_project_prefix` | 결과 한 쪽 크기 / IVAS 프로젝트 이름 앞머리 |
| `remediation.ivas_summary_check` | `exact`(기본) / `nonzero`. 2절 요약 대조 |
| `remediation.policy.pins` | "g:a" → 이 버전으로만(부모·BOM 좌표면 그 버전 변경 제안) |
| `remediation.policy.deny` / `remediation.policy.deny_versions` | 자동 변경 금지 / 후보에서 뺄 버전 |
| `remediation.policy.allow_prerelease` / `remediation.policy.allow_major` | 시험판 허용 / 메이저 변경 제안 허용 |

Maven 쪽은 이관과 같은 키를 쓴다: `build.mvn`, `build.java_home`, `build.settings_file`(조치에서는 필수), `build.local_repo`, `build.extra_args`
(TLS 끄기·다른 `-s` / `-f` 가 있으면 빼지 않고 준비 안 됨, 인자 이름을 적는다), `build.ignore_project_settings`, `build.ignore_project_mvn`(이관과 같은 환경·옵션). 로컬 저장소는 1.1.

## 8. 고칠 자리

| 요청 | 파일 |
|---|---|
| IVAS 응답 필드·경로·상태 코드·완전성 규칙 | `sbmig/remediations/ivas.py` (`PAGE_PATHS`, `ANALYSIS_ID_KEYS`, `IN_PROGRESS`, `_library_finding`, `_reconcile`) |
| ZIP 접수 규칙, 실행 경계, 전달 ZIP 모양 | `sbmig/remediations/intake.py` (`check_boundary`, `freeze_delivery`) |
| 새 출처(origin) 판정, 발견 항목 ↔ pom 줄 연결 | `sbmig/remediations/inventory.py` |
| 버전 고르기, 승인 부류, override 를 낼지·잔여로 둘지, 정책 해시, 승인 검증 | `sbmig/remediations/planner.py` (`_choose_target`, `_override_change`, `plan_round`, `policy_hash`, `validate_decision`) |
| 글자 치환, override 모양·자리, "계획한 것만" · "해석 = 계획" | `sbmig/remediations/apply.py` (`render_override`, `locate_override`, `verify_text_only`, `verify_resolution`) |
| 실패한 후보(include / discard), 검증된 ZIP 보존; 노드 순서, 멈춤·재개, 성공 판정 | `sbmig/remediations/graph.py` (`_build`, `deliver_failed_candidate`, `discard_candidate`; `_evaluate`, `_Holder.scan`) |
| Maven 명령·준비 조건·해석 읽기 | `sbmig/remediations/maven.py` |
| 보고서·한국어 문장·사내 라이브러리 안내·`framework` 그룹 | [12](12-remediation-report.md) |
| 상태·이유 코드·자료형(계약) | `sbmig/remediations/models.py` — 바꾸면 `docs/INTERFACES.md` 의 remediations 절도 |
| 새 설정 키 | `sbmig/core/config.py` 의 `RemediationConfig` + `config/default.yml` (한국어 설명) |
| HTTP 경로·작업 관리 | 같은 패키지의 api.py / service.py |
| 이관 뒤 이어 돌기(건너뜀 조건, 폴더, 재개, 로그) | 같은 패키지의 chain.py, 이관 그래프의 `remediation` 노드 |
| Job 파드의 IVAS env·정제 범위 | `deploy/k8s/81-web-job.template.yaml`, `sbmig/web/contract.py` (`SECRET_NAMES`), `sbmig/web/redact.py` (`_remediation_targets`) |

시험은 `tests/remediations/`(가짜 IVAS·Maven·저장소).

## 확인하는 법

```bash
sed -n 1,25p sbmig/remediations/__init__.py  # 패키지 지도
grep -n '^R_\|^QUEUED\|^NODES\|^TERMINAL\|^RESUMABLE' sbmig/remediations/models.py
grep -n "def _evaluate" -A45 sbmig/remediations/graph.py    # 성공 조건
grep -n "remediation:" -A45 config/default.yml
.venv/bin/python -m pytest tests/remediations -q -o addopts=""
```
