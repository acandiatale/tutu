# 취약점 조치 (remediations)

> 다루는 것: 취약점 조치(별도 그래프, 1절). 노드·상태·운영자 할 일·API·진단·설정 키·고칠 자리.
> 언제 읽나: 조치가 왜 그렇게 끝났는지 볼 때, 조치 규칙·IVAS 연동을 바꾸기 전에.
> 함께 읽을 파일: [00](00-what-sbmig-is.md), [07](07-config-and-knowledge.md), [09](09-deploy-and-ops.md), [13](13-framework-projects.md)

## 1. 무엇을 하나

입력은 Maven 프로젝트 ZIP, 출력은 결과 ZIP(`result.zip`)과 보고서, 순서는 2절의 노드 표(재검사는 전달 ZIP과 같은 바이트).
코드는 `sbmig/remediations/`, 이관 KB와 섞이지 않고 LLM을 부르지 않는다. 이관 그래프(`sbmig/graph/`)와는 노드 하나로 이어진다(1.1, 따로 돌 때는 1.2).

### 1.1 어디서 도나 — 빌드 파드, 이관에 이어서

- `remediation.after_migration`이 true이고 이관이 BUILD SUCCESS이거나, 수정 루프 없는 사내 프레임워크 실행([13](13-framework-projects.md))의
  최종 빌드가 실패했고 `remediation.after_failed_build: framework`(기본)면, `finalize` 뒤의 `remediation` 노드가 같은 `sbmig run` 프로세스에서 조치 그래프를 부른다(chain.py의 `run_after_migration`,
  둘째 경우 `baseline_may_fail=True`). 다른 실행의 빌드가 실패하면 잇지 않는다. 코드 기본 false,
  매니페스트는 ConfigMap `sbmig-env`의 `SBMIG_REMEDIATION__AFTER_MIGRATION: "true"`(두 파드가 같아야 환경 대조 통과, 09).
- 파일은 `<이관 실행 폴더>/remediation/`, 조치 id = 이관 run id. 체인이 이관된 프로젝트 트리로 ZIP을 만들어 접수(`intake.accept_zip`)에 넣는다.
- 이관과 같은 빌드: 최종 빌드 대상(`verify.build`의 target; 루트 한 번이면 빈 목록) = `state.build_targets`
  (루트 aggregator 없는 MSA는 서비스마다, 루트 pom 없이 접수). 명령 앞부분·settings·로컬 저장소·환경(IVAS_/LLM_ 만 뺌)이 같고, 캐시된 해결 실패는 `-U`로 한 번 더
  (`remediation_negative_cache_retry`), 줄마다 `remediation mvn … (cwd=…)`. 이관은 통과했는데 기준 빌드가 실패하면 메시지가 그 사실로 시작
  (`remediation_baseline_differs`; 빌드 사본에 `.git/` 없음).
- 기준 빌드 실패를 견디는 방식(둘째 경우, `RemediationState.baseline_tolerated`, 재개도 같음): 끝까지 돌고 실패한 기준 빌드만(시간 초과·실행
  못 함은 FAILED `baseline_build_failed`) 기록하고(`baseline_build.ok` false, 저널 `remediation_baseline_failed` `tolerated: true`) 검사한다.
  실패한 후보 빌드는 `maven.compare_with_baseline`으로 비교(`RoundState.build_compare`/`build_tolerated`, 저널 `remediation_candidate_compared`):
  같은 빌드 대상(로그 이름)·같은 실패 모듈이고 후보의 오류 키(종류·모듈·goal·파일·줄·L1)가 모두 기준 빌드에 있으면 확정·전달 ZIP
  (`DeliveryInfo.build_tolerated`)·재검사, 새 오류(풀리지 않는 버전 포함)면 FAILED `candidate_build_failed`, `baseline_build_failed`.
  한계(보고서도 적음): 실패한 모듈 뒤는 컴파일된 적이 없어 "새 오류 없음"은 "컴파일됨"이 아니다. 버전을 바꿨으면(확정 라운드의
  적용 제안) INCOMPLETE, 첫 이유 `baseline_build_failed`(요약 `baseline_tolerated: true`, 결과 줄 `exit=2 … remediation=INCOMPLETE`), SUCCEEDED
  없음: 빌드 오류를 고친 뒤 바뀐 버전으로 한 번 빌드한다. 바꾼 것이 없거나 후보가 빌드되면 보통처럼 끝난다(`baseline_build_failed` 없음). 말은 12.
- 조치의 결과는 이관의 성공 판정·종료 코드·보고서 판정을 바꾸지 않는다. `SBMIG-RESULT … remediation=<상태|skipped>`.
  IVAS 세 값이나 Maven 준비가 없으면 건너뛰고 저널에 `remediation_skipped`(이름만).
- 조치는 누구에게도 묻지 않고 변경을 적용해 이관과 같은 파드에서 끝까지 간다. 빌드 파드는 사람을 기다리지 못해
  `AWAITING_CONFIRMATION`이면 상태를 저장하고 끝난다. Job 모드의 resume =
  새 빌드 파드(같은 Job 틀·실행 폴더, Worker 동작 `remediation-resume`→`chain.resume`), 업로드와 같은 FIFO 줄·자리.
  줄에 서기 전에 앞 시도의 `remediation-job-status.json`을 지우고 시도 번호를 Job env `SBMIG_JOB_ATTEMPT`로(Worker가 모든 상태 파일에 적음).
  확인 대기면 웹 파드가 `confirm` 등을 Job 생성 전에 `state.json`에 쓴다.
- 웹 파드는 상태·파일·재개만 맡는다. 단독 업로드는 `in-process` 모드만(5절, 폴더 `<웹 out>/_remediations/<id>/`).
- Job 파드의 IVAS 세 값은 Secret `sbmig-llm`에서(81 틀 `secretKeyRef`, optional), 환경 대조 지문에 없다. `IVAS_USER_ID`도 비밀로 지운다(`SECRET_NAMES`).
- Maven 로컬 저장소는 그 파드의 자리 저장소(`repository-slot<k>`). `<local repo>-remediation`은 in-process의 단독 업로드와 이어 돈 조치의 재개에.
- IVAS 로그인은 파드마다 해도 된다.
- 시간: Job 기한(4시간)은 이관과 조치가 나눠 쓴다. `remediation.chain_budget_minutes`(90, 0 = 없음)만큼 돌면 다음 경계에서 STOPPED
  `chain_budget`(chain.py의 `R_CHAIN_BUDGET`, 저널 `remediation_budget`). 재개는 새 파드에서 다시 재고, 검사 중이었으면 영수증으로 이어 조회.

### 1.2 취약점 해결만 — 실행 방식 `remediation`

- 이관된 프로젝트를 다시 점검할 때: `sbmig run <zip|폴더> --flow remediation`, 웹 `POST /api/runs?…&flow=remediation`, 화면의
  취약점 해결. Job 모드는 보통 실행과 같은 줄·자리(Job env `SBMIG_FLOW`는 웹 파드가 넣음).
- 엔진 노드 `intake→remediation→finalize`. 접수는 고치지 않는 데까지만(사전 점검, 빌드 도구 판별·거절), 이관 단계·LLM·KB 없음.
  `chain.run_only`가 올린 ZIP 바이트(폴더는 그대로 묶어)를 접수에 넣는다. 올린 그대로 빌드되지 않으면 검사 전에 FAILED `baseline_build_failed`.
- `remediation.after_migration`·`remediation.trusted_projects`는 묻지 않는다. IVAS 세 값·Maven 준비가 없으면 시작하지 않는다(exit 3,
  플래그 `remediation:not-ready`, 저널 `remediation_skipped`에 이름만).
- 폴더·상태·재개는 1.1과 같다. 결과 줄 `SBMIG-RESULT … flow=remediation … remediation=<상태>`, 종료 코드 0 = SUCCEEDED, 2 = 그 밖의
  저장 상태, 3 = 시작 못 함. `report.md`는 짧다(이관 안 함 한 줄 + 조치 절 + `remediation/report.md` 경로, ko는 받는 이름 `remediation-report.md`). `--stop-after`는 못 쓴다.

절대 규칙(회사 결정):

- 바뀌는 것은 기존 pom.xml 안의 버전 글자뿐. 소스·구조·다른 파일은 그대로, 결과 ZIP의 파일 집합 = 원본.
  변경 하나(`TextEdit`, `op="replace"`) = pom 한 줄에서 버전 글자 `old`→`new`(태그·`${…}` 없음). 인코딩·BOM·CRLF·마지막 줄바꿈도 그대로.
- 예외는 하나: `managed_override` 제안은 대상 pom(루트 `pom.xml`, 루트를 상속하지 않는 모듈은 그 모듈 pom)의 최상위 `<dependencyManagement><dependencies>` 안에 `<dependency>` 항목 하나
  (groupId / artifactId / version, 변종이면 type·classifier도)를 넣는다. 절이 없으면 그 항목을 담은 `<dependencyManagement>` 절을 최상위 `<dependencies>`
 →`<build>`→`</project>` 중 처음 있는 줄 앞에 넣는다. `TextEdit`의 `op="insert"`, `old=""`, `line`(이 줄 앞에), `anchor`(그 줄의 지금 글자,
  적용할 때 다시 확인), `override`(그 좌표). 넣는 글자는 `render_override`의 모양과 정확히 같다.
- "계획한 것만 바뀌었나"(`verify_text_only`): 나머지 파일은 바이트가 같고, 받은 pom은 원본에 계획한 replace·insert를(같은 줄 앞은 제안 ID 순)
  적용한 바이트와 같고, XML 구조는 계획한 override 요소만큼만 는다.
- 원본 ZIP·파일 목록(manifest)·작업 트리 `accepted/`는 따로. 변경은 후보 사본에 먼저 하고, 빌드되고 해석이 계획과 같을 때만 `accepted/`로
  확정한다. 올린 ZIP의 `target/`(pom 옆)은 manifest에서 뺀다(v2.1z-bh).
- pom은 Maven이 읽는 것은 모두 읽는다: 경계 검사·인벤토리·계획의 위치·적용·구조 검사가 `apply.read_pom_xml`/`apply.pom_xml_bytes`로 이관과 같이
  디코드한다(`analyze.pom_parser.pom_encoding`: BOM→엄격한 UTF-8→선언한 인코딩, 한국어 계열은 cp949→파일 전체를 읽는 첫 후보). 변경은 읽은
  인코딩으로 다시 써서 바뀐 글자의 바이트만 다르다. 정말 못 읽는 pom만 FAILED `execution_boundary_refused`, `unparsable_pom: <경로>: <이유>`:
  `not well-formed XML (line N): …`(XML 문법 오류(줄 N)), `cannot be opened (permission denied)`(파일을 열 수 없습니다(권한)), `(missing)`(파일이
  없습니다), `(a folder, not a file)`(파일이 아니라 폴더입니다), `character encoding unknown (…)`(글자 인코딩을 알 수 없습니다). 화면·보고서:
  「pom 파일을 읽을 수 없습니다. app-core/pom.xml: XML 문법 오류(줄 12).」, 할 일 「pom 파일 고치기」(`i18n.pom_why_ko`).
- IVAS 업로드 결과가 불확실하면 다시 올리지 않는다. 미완료 검사·빠진 쪽·중복·ZIP 해시 불일치는 "0건"이 아니다.
- 새 버전의 근거는 서버 정책(`remediation.policy.pins`)이나 승인된 저장소에 실제로 있는 버전뿐. IVAS의 patched 버전은 하한 힌트이고,
  pin이 없으면 힌트가 있어야 제안한다(없으면 `no_candidate_version`).
- 서버가 다시 떠도 자동 재개·재전송은 없다(`INTERRUPTED` + 명시적 resume). 상태를 진행시키는 것은 `remediation/claim.json`을 가진 프로세스
  하나뿐(`RunClaim`, 30초 갱신, 600초 갱신 없으면 넘겨받음, 둘째는 거절).
- IVAS 아이디·비밀번호·토큰, Nexus 자격증명은 코드·설정·로그·보고서·예외 문장에 쓰지 않는다.

## 2. 흐름 — 노드별

그래프는 `sbmig/remediations/graph.py`(`run_remediation`). 노드 경계마다 `state.json` 저장, 재개는 `state.next_node`부터.
중지 요청은 노드 사이와 빌드·IVAS 조회 중에 본다.

| 노드 | 하는 일 | 다음 / 멈춤 |
|---|---|---|
| `baseline_build` | Maven 준비 확인(없으면 FAILED `not_ready`), 실행 경계 검사(refuse 하나라도→FAILED `execution_boundary_refused`), 사본에서 `mvn clean package -Dmaven.test.skip=true` | 실패→FAILED `baseline_build_failed`(견디는 방식이면 기록하고 계속, 1.1) |
| `baseline_scan` | 원본 ZIP sha256 재확인, IVAS 검사(라벨 `baseline`) | 조치 대상 0건(완전한 검사)→원본 바이트 사본이 전달 ZIP, `evaluate`. 미완료→INCOMPLETE `scan_incomplete`. 업로드 불확실→AWAITING_CONFIRMATION |
| `inventory` | 빌드 대상마다 `dependency:tree`, 발견 항목→`Inventory`(출처 판정) | 해석·인벤토리 불완전→INCOMPLETE `manual_review` |
| `plan` | 제안·잔여, 위험 부류, 해시 세 개. 트리·정책 해시가 바뀐 옛 계획은 `rounds/<n>/plan.stale.json`으로 두고 새로 만든다 | 적용할 것 없음→`evaluate` |
| `apply` | `accepted/`→후보 사본, 계획의 제안 전부 적용, `rounds/<n>/candidate.patch`, "계획한 것만 바뀌었나" 검사 | 위반→FAILED `text_only_violation` |
| `build` | 후보 빌드, 다시 해석, 산출물 안 jar 목록, "해석 결과 = 계획" 검사(적용 범위), 통과하면 `accepted/`로 확정. `candidate.ready.json`이 없으면 `apply`로 | 빌드 실패→(견디는 방식은 먼저 1.1의 비교) FAILED `candidate_build_failed`(2절 "실패한 후보"), 불일치→FAILED `resolution_mismatch` |
| `freeze_delivery` | `accepted/`로 결정적인 전달 ZIP(`delivery/result.zip`) | — |
| `rescan` | 전달 ZIP sha256 재확인, 같은 바이트를 IVAS 검사(라벨 `final-r<n>`) | 미완료→INCOMPLETE, sha 다름→FAILED `zip_hash_mismatch`, 불확실→AWAITING_CONFIRMATION |
| `evaluate` | 조치 대상 취약점이 남았고 이번 라운드가 확정됐고 라운드 < `remediation.max_rounds`→다음 라운드 | `inventory`로 돌아가거나 성공 판정 |
| `report` | 보고서 두 개(끝 상태에서만) | 끝 |

검사 기록: 올리기 전 `state.pending_upload[라벨]`, 영수증(프로젝트·분석 ID)을 받으면 곧 `state.pending_receipt[라벨]`. 재개 때 영수증이
있으면 올리지 않고 이어 조회, `pending_upload`만 있으면 확인 없이는 다시 멈춘다.
파일 컴포넌트의 파일 목록 주소는 `…/result/filecomponents/{id}/matchedfiles`. ID는 받은 그대로 경로 조각 하나로 인코딩한다(`11560|934982`→
`11560%7C934982`; 실제 IVAS에서 이 ID는 ossId이고 `|`도 받는다고 사용자가 확인). sbmig는 `fileComponent.id`를 먼저, 없을 때만
`ossId`를 쓴다(메모).
요약 대조(`remediation.ivas_summary_check`): `exact`(기본) = 요약 `components.total` == 모든 쪽의 라이브러리
취약점 행 수(중복 제거 전), 다르면 `summary_mismatch`. `swComponents`·`fileComponents`는 0 / 0 아님만. `nonzero` = components도 그렇게.

**파일 컴포넌트**(`remediation.file_components`, 기본 `report` | `count`): IVAS가 프로젝트 안 파일에서 찾은 것(`file_main`, `file_potential`:
정적 파일, pom 밖의 jar)은 pom 버전만 바꾸는 이 조치가 고치지 않는다.
- `report`: 정보다. `ivas.split_files`/`_Holder.split()`가 먼저 떼어 내고(무시 코드여도 파일 쪽), 인벤토리·제안·잔여·남은 취약점·판정·다음
  라운드 어디에도 넣지 않고 보고서의 절 하나에 적는다(12). 파일 목록이나 한 컴포넌트의 matchedfiles를 못 읽어도 미완료가 아니라
  검사의 메모, `ScanResult.file_complete` false(목록·그 안의 잘못되거나 겹친 항목·`fileComponents` 요약 대조 실패) 또는 `Finding.files_unread`.
  완전성(`scan_incomplete`)은 요약과 라이브러리·`sw` 목록이 정한다. 개수(`state.counts`, 요약, `report`일 때만): `baseline_file_findings`,
  `final_file_findings`, `file_lists_unread`(파일 목록을 못 읽은 검사 수; 그 검사의 `*_file_findings`는 없음). 저널
  `file_findings_reported {label, findings, unread, file_complete}`(검사마다), `file_residuals_dropped`(옛 상태의 파일 잔여를 버림).
  pom 밖의 jar도 파일 컴포넌트라 빠진다(경로로 구별).
- `count`: 파일 발견은 `vendored` 잔여로 결과를 `INCOMPLETE`로 두고, 못 읽은 쪽은 검사를 미완료로 만든다. `report`로 저장된 검사가 파일 쪽을
  못 읽었으면 미완료(`ivas.count_view`, 이유 `file_components_unread`; 파일은 그대로). 정책 해시에 들어가지 않는다.

**조치 구분 코드**(`action.actionDivCd`, 뜻은 ivas.py의 `ACTION_MEANINGS`): NE 조치 필요, NP 조치계획 미등록, PL 조치예정,
OT 조치일 경과, PC 패치 완료, IG 조치 예외. `remediation.ignore_actions`(기본 비어 있음 = 모두 조치 대상; 예 `[PC, IG]`; 공백·대소문자 무시)의
코드는 검사 파일과 보고서 표 "조치 대상에서 제외한 항목"에만 남고 인벤토리·계획·남은 취약점에서 빠진다(`counts`의 `*_findings`는 조치 대상만,
`*_ignored` 따로). 빈·모르는 코드는 조치 대상, 한 번 기록(저널 `ivas_action_unknown`).

SUCCEEDED 조건(전부): 기준 빌드 통과(견디는 방식의 예외는 1.1), 적용한 라운드는 모두 빌드·검증·확정, 최종 검사 완전, 전달 ZIP sha256 = 최종 검사 ZIP sha256,
전달 ZIP의 `build_ok`(실패한 수정본 아님)·`scanned`(완전한 최종 검사가 이 바이트를 봄) 참, 잔여(수동 검토·출처 불명·
deny·후보 없음·적용 안 된 제안) 없음, 최종 검사에 조치 대상 없음. 아니면 INCOMPLETE / FAILED(이유는 `state.reasons`; `build_ok`·`build_tolerated` 거짓 →
`candidate_build_failed`, `scanned` 거짓→`zip_hash_mismatch` 또는 `scan_incomplete`).

**실패한 후보**(`remediation.failed_candidate`, 값 `include`(기본) 또는 `discard`):

- `include`: 후보 빌드가 실패하면 그 수정본이 `delivery/result.zip`(`build_ok=false`, `scanned=false`, 재검사 없음), FAILED
  `candidate_build_failed`. `accepted/`·원본 ZIP·`candidate.patch`·`build.log`는 남고, `report.md`는 "빌드가 실패한 수정본" 경고로 시작.
  앞 라운드의 검증된 전달 ZIP은 `delivery/result-r<k>-verified.zip`(+ `.json`)으로 옮겨 두고 내려받기 이름은 `result-verified.zip`.
- `discard`: 실패한 후보는 결과 ZIP에 넣지 않는다(변경은 `candidate.patch` 로만). 앞 라운드의 검증된 전달 ZIP이 있으면 그것이
  `result.zip`, 없으면 전달 ZIP 없음.
- 어느 값이든 `text_only_violation`·`resolution_mismatch` 후보는 결과 ZIP에 넣지 않는다.

## 3. 제안 규칙

계획의 제안은 모두 누구에게도 묻지 않고 적용한다(`decision` `auto`; 넣어도 효과가 없어 다시 뺀 override는 `ineffective`). 위험 부류
(`risks`): `major`(메이저 변경), `parent`(`<parent><version>`), `bom`(가져온 BOM 버전), `managed_override`(넣는 관리 항목): 보고서의
「주의」 칸과 할 일에 사실로 나온다(12). 사람 대신 남는 안전장치: 격리된 후보, 후보 빌드(깨는 변경 묶음은 통째로 FAILED
`candidate_build_failed`), 효과 없는 override 항목 제거, 전달 ZIP 재검사.
부모·BOM 버전 변경은 그 부모·BOM 자체가 `remediation.policy.pins`에 있을 때만 제안된다(`parent_version`/`bom_version`, override 아님).

관리 버전 override: `transitive`·`parent_managed`·`bom_managed` 항목은 (관리자 pin이 없으면) 직접 의존성과 같은 규칙(1절의 근거,
같은 메이저 우선, deny·`remediation.policy.allow_major`)으로 버전을 고르고, 대상 pom에 항목 하나를 넣는 `managed_override` 제안을
(pom, g:a·변종)마다 하나씩 낸다(`remediation.module_overrides` false면 모듈은 잔여). 그 관리 항목이 그 pom에 이미 있으면 넣지 않는다: 최상위
하나이고 버전 글자(또는 프로젝트 속성)가 해석된 버전과 같으면 그 글자를 바꾸고(`own_management_version`/`property_version`), 아니면 잔여.
제안의 `note`는 `report.md` 제안 아래.

제안하지 않는 것(잔여·수동 검토): `plugin`, `vendored`, `unmapped`(꺼진 프로필에만 있는 선언, 한 `<dependencies>`에 두 번 선언된 g:a 포함),
`remediation.policy.deny`에 걸린 것, `remediation.policy.allow_major`가 false 일 때의 메이저. 잘못된 버전을 막는 규칙(공유 property는
모든 아티팩트에 목표 버전이 있을 때만, 다른 메이저는 힌트가 그 메이저일 때만, 날짜 버전 아님, 이미 만족한 힌트면 안 올림)은 12의 5절.

정책 해시 = `remediation.policy` 전부 + `failed_candidate` + `ignore_actions` + 사내 g:a(+ 프레임워크 패턴, 12) + false 일 때의
`module_overrides`(`ivas_summary_check`는 안 들어감). 바뀌면 열린 계획을 마지막 검사로 다시 만든다. 제안 해시는 edit 전부(`op`·`anchor`·`override`
포함)를 덮는다.

승인 단계는 없다. yml의 `remediation:` 아래 `require_approval`·`always_approve`, 환경 변수 `SBMIG_REMEDIATION__REQUIRE_APPROVAL`은 오류가
아니라 각각 WARNING 한 줄로 무시된다(`config: remediation.require_approval=… is ignored: the approval step of the vulnerability remediation
was removed (every proposal is applied without waiting) — remove it`, `config._warn_removed_keys`; 그 변수는 환경 대조에서도 빠진다). 옛 버전이
승인 대기로 저장한 조치는 끝난 것으로 보인다(목록에는 표시 없음, 열면 「조치 미완료」와 「이 실행의 취약점 조치는 끝나지 않았습니다. 프로젝트를 다시
올리면 끝까지 진행합니다.」; 화면에 승인이라는 말은 없다; 파일은 그대로 받음, resume은 409): 프로젝트를 다시 올린다. 클러스터 정리는 `docs/RUNBOOK.md` §6.

## 4. 상태와 운영자가 할 일

| 상태 | 뜻 | 할 일 |
|---|---|---|
| `QUEUED`/`RUNNING` | 대기 / 실행 중(단독 업로드는 한 번에 한 건) | 기다린다. 멈추려면 stop |
| `AWAITING_CONFIRMATION` | 업로드를 보냈는데 결과를 모른다 | IVAS 화면에 그 프로젝트(`<앞머리>-<작업 ID>-<라벨>`)의 분석이 있나 본다. 없으면 resume `{"confirm": "reupload"}`, 있으면 `{"confirm": "receipt", "label", "project_id", "analysis_id"}` |
| `STOPPED` | 중지 요청을 경계에서 처리(원격 IVAS 분석은 취소 안 됨) | resume |
| `INTERRUPTED` | 서버가 실행 중에 내려갔다 | 상태를 보고 resume |
| `SUCCEEDED` | 성공 조건 전부 | `result.zip` 전달 |
| `INCOMPLETE` | 끝났지만 성공 아님 | 보고서의 잔여 표를 담당 팀에 |
| `FAILED` | 빌드·검증 실패, 경계 거부, 준비 안 됨, 내부 오류 | 이유 코드대로(6절). `delivery.build_ok` false면 `result.zip`은 실패한 수정본(배포 금지) |

이유 코드(문자열 그대로가 API 계약): `baseline_build_failed`, `candidate_build_failed`, `scan_incomplete`, `upload_uncertain`, `zip_hash_mismatch`,
`text_only_violation`, `resolution_mismatch`, `execution_boundary_refused`, `not_ready`,
`residual_findings`, `manual_review`, `no_candidate_version`, `rounds_exhausted`, `stop_requested`, `internal_error`, `policy_denied`,
`unknown_provenance`, `stale_location`, `inventory_incomplete`, `inhouse_artifact`(12). 정의는 `sbmig/remediations/models.py`(이관에 이어 돈 조치만 `chain_budget`, chain.py).

## 5. HTTP API (웹 파드, TokenGuard 뒤 — 다른 사이트의 요청은 403)

| 경로 | 결과 |
|---|---|
| `POST /api/remediations?name=<file.zip>` (본문 = ZIP) | `in-process` 모드만: 202 `{id, status, url}` / 400 접수 거부(코드) / 413 크기 초과 / 503 `not_ready` + `missing`. Job 모드는 409 `not_available` |
| `GET /api/remediations?page=&size=` | 목록(최신 먼저) |
| `GET /api/remediations/readiness` | `{ready, missing}`(이름만) |
| `GET /api/remediations/<id>` | 상태 dict(status, reason, reasons, node, next_node, round, counts, delivery `{sha256, size, round, build_ok, scanned}`, files, links …) |
| `GET /api/remediations/<id>/result` | 끝 상태면 report.json(`delivery`, `verified_delivery` 포함), 아니면 409 `not_finished` |
| `POST /api/remediations/<id>/resume` | 재개(확인 대기면 `confirm` 필수). Job 모드의 이어 돈 조치는 새 빌드 파드 |
| `POST /api/remediations/<id>/stop` | 다음 경계에서 멈춤 |
| `GET /api/remediations/<id>/files/<name>` | 정해진 파일만: 보고서 두 개, result.zip, source.zip, manifest.json, journal.jsonl, remediation.log, delivery.json, `result-verified.zip`(앞 라운드의 검증된 ZIP, 없으면 404), `plan-r<n>.json`, `candidate-r<n>.patch`, `build-r<n>.log`, baseline-build.log, `scan-<label>.json`. `?download=1`이면 첨부(이름: [12](12-remediation-report.md) 1절) |
| `DELETE /api/remediations/<id>` | 원본까지 폴더 삭제(실행 중이면 409) |

## 6. 진단 순서

실행 폴더는 1.1(쿠버네티스 `/data/out/<run id>/remediation/`).
조치가 없으면 이관 `journal.jsonl`의 `remediation_skipped`.

1. `state.json`: `status`, `reason`, `reasons`, `message`, `node`(마지막), `next_node`(재개 지점), `round`, `pending_upload` /
   `pending_receipt`(검사 중 멈춘 자리), `boundary`(경계 검사), `residuals`.
2. `journal.jsonl`: `node_start`/`node_end`, `scan_receipt`, `scan_resume`, `upload_uncertain`, `scan_stopped`, `scan_done`,
   `ivas_upload`, `ivas_status`, `ivas_pages`, `ivas_scan_done`, `plan_written`, `plan_reused`, `plan_stale`, `policy_hash_differs`,
   `round_committed`, `next_round`, `maven_project_config`, `ivas_action_unknown`, `remediation_budget`, `failed_candidate_delivered`, `verified_delivery_kept`, `earlier_delivery_kept`.
   결과 ZIP의 정체는 `delivery/delivery.json`의 `build_ok`·`build_tolerated`·`scanned`.
3. remediation.log: 로그(비밀 없음, 모든 조치). Maven 원문은 `baseline/build.log`, `rounds/<n>/build.log`(대상이 여럿이면 `build-<대상>.log`).
4. `scans/<label>.json`: 검사 하나: `complete`, `incomplete_reasons`(`analysis_failed`, `analysis_timeout`, `page_missing`, `total_mismatch`,
   `duplicate_result`, `summary_mismatch`, `unsupported_sw`, `zip_hash_mismatch`, `id_mismatch`, `http_error`, `stop_requested`, `count`일 때 `file_components_unread`), `file_complete`, `findings`, `counts`, `notes`.
5. `rounds/<n>/`: `before.json`(해석 결과), `inventory.json`(항목별 `origin`, `location`, `reason`), `plan.json`(제안·잔여), `apply.json`
   (적용·건너뜀), `candidate.patch`, `resolved.json`(후보 해석 결과).

자주 보는 경우: matchedfiles 호출의 `scan_incomplete … http_error, page_missing`→예전 단수 주소(`filecomponent`)의 404, 이어 갈 수 없고 새로 돌린다. 보고서에 파일 발견이 있는데 결과는 조치 완료→`report`에서 정상(`count`면 판정에 들어감). 이관 빌드가 실패했는데 조치가 돌았다→사내 프레임워크 실행이면 정상(1.1, 끄려면 `never`). `not_ready`→readiness의 `missing`(`remediation.trusted_projects`, IVAS 세 값, JDK 21, Maven, settings.xml, 승인된 저장소 호스트). `execution_boundary_refused` →
`state.boundary`의 `code`(`project_maven_config`, `maven_extensions`, `maven_wrapper`, `symlink`, `foreign_repository`,
`foreign_plugin_repository`, `unparsable_pom`). `manual_review`만 남음→`inventory.json`의 `origin`이 `plugin`/`vendored`/`unmapped` 이거나,
`transitive` / 부모·BOM 관리인데 override를 막은 사실이 잔여의 `detail`에(남은 classifier, 루트 pom 비상속·없음, 직접 선언 버전, 넣을 줄
없음, 이미 있는 관리 항목). `resolution_mismatch`→제안의 적용 범위 모듈이 계획 버전이 아니다(12의 4절). `no_candidate_version`→잔여의 `detail`에 저장소가 준 버전 목록과 제외 이유(힌트 없음 포함)가 있다.

## 7. 설정 키 (`remediation:` 절, `config/default.yml`에 한국어 설명)

우선순위: `config/default.yml`→회사 yml→환경 변수 `SBMIG_REMEDIATION__<키>`. 비밀은 환경 변수(쿠버네티스 Secret)에서만:
`IVAS_BASE_URL`, `IVAS_USER_ID`, `IVAS_PASSWORD`. Nexus 자격증명은 서버 settings.xml의 `<server>`(`${env.NEXUS_USER}` 등).

| 키(`remediation.` 아래) | 뜻 |
|---|---|
| `max_upload_mb`/`max_entries`/`max_unpacked_mb`/`max_entry_mb`/`max_compression_ratio` | ZIP 한도(압축률은 1 MiB 이상 풀리는 항목만) |
| `after_migration` | 이관에 이어 같은 빌드 파드에서 조치를 돌릴지(기본값과 매니페스트 값은 1.1) |
| `after_failed_build` | `framework`(기본, 1.1) / `never` = 빌드가 실패한 실행은 잇지 않음 |
| `trusted_projects` | (in-process 단독 업로드만) Maven이 올라온 프로젝트의 플러그인을 돌리므로 운영자가 true로 바꿔야 접수(기본 false) |
| `max_rounds` | 계획→적용→빌드→재검사 반복(기본 3) |
| `failed_candidate` | 후보 빌드 실패: `include`(기본, 실패한 수정본도 결과 ZIP) / `discard`. 2절 |
| `ignore_actions` | 계획에서 뺄 IVAS 조치 코드(2절, 정책 해시에 들어감) |
| `chain_budget_minutes` | 이어 돈 조치의 파드당 시간 한도(1.1) |
| `project_maven_config` | `.mvn/maven.config`·`jvm.config`·`extensions.xml`이 있을 때 ignore(기본, 기록만) / refuse. 빌드는 그 폴더를 읽지 않는다(`build.ignore_project_mvn`) |
| `maven_wrapper` | `mvnw`가 있을 때(서버 Maven만 쓰므로 기본 ignore) |
| `allowed_repository_hosts` | 승인된 저장소 호스트. 비우면 서버 settings.xml의 미러·저장소 호스트 |
| `foreign_repository` | pom이 승인 안 된 호스트 저장소를 선언할 때 |
| `build_timeout_minutes` | package 빌드 한 번 한도 |
| `ivas_timeout_seconds`/`ivas_upload_timeout_seconds` | IVAS 요청 / 업로드 한도 |
| `ivas_poll_seconds`/`ivas_analysis_timeout_minutes` | 상태 조회 간격 / 분석 대기 한도(기본 30분, 넘으면 미완료) |
| `ivas_page_size`/`ivas_project_prefix` | 결과 한 쪽 크기 / IVAS 프로젝트 이름 앞머리 |
| `ivas_summary_check` | `exact`(기본) / `nonzero`. 2절 요약 대조 |
| `file_components` | `report`(기본: 파일 발견은 보고서에만) / `count`(잔여로 판정에 넣음). 2절 |
| `policy.pins` | "g:a"→이 버전으로만(부모·BOM은 3절) |
| `policy.deny`/`policy.deny_versions` | 자동 변경 금지 / 후보에서 뺄 버전 |
| `policy.allow_prerelease`/`policy.allow_major` | 시험판 허용 / 메이저 변경 제안 허용 |

Maven 쪽은 이관과 같은 키를 쓴다: `build.mvn`, `build.java_home`, `build.settings_file`(조치에서는 필수), `build.local_repo`, `build.extra_args`
(TLS 끄기·다른 `-s`/`-f`가 있으면 빼지 않고 준비 안 됨, 인자 이름을 적는다), `build.ignore_project_settings`, `build.ignore_project_mvn`. 로컬 저장소는 1.1.

## 8. 고칠 자리

파일별 자리는 [01](01-repo-map.md) 3절의 "취약점 조치:" 행. 시험은 `tests/remediations/`(가짜 IVAS·Maven·저장소).

## 확인하는 법

```bash
sed -n 1,25p sbmig/remediations/__init__.py  # 패키지 지도
grep -n '^R_\|^QUEUED\|^NODES\|^TERMINAL\|^RESUMABLE' sbmig/remediations/models.py
grep -n "def _evaluate" -A45 sbmig/remediations/graph.py  # 성공 조건
grep -n "remediation:" -A45 config/default.yml
.venv/bin/python -m pytest tests/remediations -q
```
