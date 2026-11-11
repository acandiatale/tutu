# 취약점 조치 (remediations)

> 다루는 것: 이관과 별개인 두 번째 기능 — 올린 Maven 프로젝트를 IVAS 로 검사하고 취약한 라이브러리의 **버전 글자만** 고쳐 다시 빌드·재검사하는 흐름. 노드별 동작, 상태와 운영자가 할 일, HTTP API, 진단 순서, 설정 키, 고칠 자리.
> 언제 읽나: `/api/remediations` 작업이 왜 그렇게 끝났는지 볼 때, 조치 규칙·IVAS 연동을 바꾸기 전에.
> 함께 읽을 파일: [00-what-sbmig-is.md](00-what-sbmig-is.md), [07-config-and-knowledge.md](07-config-and-knowledge.md), [09-deploy-and-ops.md](09-deploy-and-ops.md)

## 1. 무엇을 하나

입력은 Maven 프로젝트 ZIP 하나, 출력은 결과 ZIP(`result.zip`)과 보고서. 순서: 그대로 빌드(기준 빌드) → IVAS 검사(기준 검사) → 발견 항목을
Maven 이 실제로 고른 의존성과 pom 의 버전 줄에 연결 → 새 버전 제안 → 격리된 후보 사본에 적용 → 후보 빌드·검증 → 전달 ZIP 고정 → **같은 바이트**
재검사 → 판정 → 보고서. 코드는 `sbmig/remediations/` 한 패키지(지도는 `sbmig/remediations/__init__.py`), 이관 그래프(`sbmig/graph/`)·이관 KB·
`/api/runs` 와 섞이지 않는다. LLM 을 부르지 않는다.

절대 규칙(회사 결정):

- 바뀌는 것은 **기존 pom.xml 안의 버전 글자뿐**이다. 소스·구조·다른 파일은 그대로, 결과 ZIP 의 파일 집합 = 원본의 파일 집합.
  변경 하나 = pom 한 줄에서 버전 글자 `old` → `new`(태그·`${…}` 없음). 인코딩·BOM·CRLF·마지막 줄바꿈도 그대로 둔다.
- 원본 ZIP·원본 파일 목록(manifest)·작업 트리 `accepted/` 를 따로 둔다. 변경은 후보 사본에 먼저 하고, 빌드되고 해석 결과가 계획과 같을 때만
  `accepted/` 로 확정한다. 빌드 산출물(`target/`)은 해시·복사·전달 어디에도 들어가지 않는다(manifest 경로만).
- 기준 빌드가 실패하면 검사·계획으로 가지 않는다.
- IVAS 업로드 결과가 불확실하면 **자동으로 다시 올리지 않는다**. 미완료 검사·빠진 쪽·중복·ZIP 해시 불일치는 "0건" 이 아니다.
- 새 버전의 근거는 서버 정책(`remediation.policy.pins`) 또는 승인된 저장소가 실제로 가진 버전뿐이다. IVAS 의 patched 버전은 하한 힌트일 뿐이다.
- 서버가 다시 떠도 아무것도 자동으로 재개·재전송하지 않는다(`INTERRUPTED` + 명시적 resume).
- IVAS 아이디·비밀번호·토큰, Nexus 자격증명은 코드·설정·로그·보고서·예외 문장 어디에도 쓰지 않는다.

## 2. 흐름 — 노드별

그래프는 `sbmig/remediations/graph.py`(진입점 `run_remediation`). 노드 경계마다 `state.json` 을 저장하고, 재개는 `state.next_node` 부터다.
중지 요청은 노드 사이와 빌드·IVAS 조회 중에 확인한다.

| 노드 | 하는 일 | 다음 / 멈춤 |
|---|---|---|
| `baseline_build` | Maven 준비 확인(없으면 FAILED `not_ready`), 실행 경계 검사(refuse 하나라도 → FAILED `execution_boundary_refused`), 사본에서 `mvn clean package -Dmaven.test.skip=true` | 실패 → FAILED `baseline_build_failed` |
| `baseline_scan` | 원본 ZIP sha256 재확인, IVAS 검사(라벨 `baseline`) | 발견 0건(완전한 검사) → 원본 바이트 사본이 전달 ZIP, `evaluate`. 미완료 → INCOMPLETE `scan_incomplete`. 업로드 불확실 → AWAITING_CONFIRMATION |
| `inventory` | 전 트리의 `dependency:tree`, 발견 항목 → `Inventory`(출처 판정) | 해석·인벤토리 불완전 → INCOMPLETE `manual_review` |
| `plan` | 제안·잔여, 승인 부류, 해시 세 개. 트리·정책 해시가 바뀐 옛 계획은 `rounds/<n>/plan.stale.json` 으로 두고 새로 만든다 | 승인 대기 제안 → AWAITING_APPROVAL. 적용할 것 없음 → `evaluate` |
| `apply` | `accepted/` → 후보 사본, 자동+승인 제안 적용, `rounds/<n>/candidate.patch`, "버전 글자만 바뀌었나" 검사 | 위반 → FAILED `text_only_violation` |
| `build` | 후보 빌드, 다시 해석, 산출물 안 jar 목록, "해석 결과 = 계획" 검사, 통과하면 `accepted/` 로 확정 | 빌드 실패 → FAILED `candidate_build_failed`, 불일치 → FAILED `resolution_mismatch` |
| `freeze_delivery` | `accepted/` 로 결정적인 전달 ZIP(`delivery/result.zip`) | — |
| `rescan` | 전달 ZIP sha256 재확인, 같은 바이트를 IVAS 검사(라벨 `final-r<n>`) | 미완료 → INCOMPLETE, sha 다름 → FAILED `zip_hash_mismatch`, 불확실 → AWAITING_CONFIRMATION |
| `evaluate` | 취약점이 남았고 이번 라운드가 확정됐고 라운드 < `remediation.max_rounds` → 다음 라운드 | `inventory` 로 돌아가거나 성공 판정 |
| `report` | 보고서 두 개(끝 상태에서만) | 끝 |

검사 기록: 올리기 전에 `state.pending_upload[라벨]` 을 저장하고, 영수증(프로젝트·분석 ID)을 받으면 곧바로 `state.pending_receipt[라벨]` 로 옮겨
저장한다. 재개 때 영수증이 있으면 올리지 않고 그 분석을 이어서 조회한다. `pending_upload` 만 남아 있으면 확인 없이는 다시 멈춘다.

**SUCCEEDED 조건**(전부): 기준 빌드 통과, 무엇을 적용한 라운드는 모두 빌드·검증·확정, 최종 검사 완전, 전달 ZIP sha256 = 최종 검사 ZIP sha256,
pending·rejected 제안 없음, 잔여(수동 검토·출처 불명·정책 deny·후보 없음·적용 안 된 제안) 없음, 최종 검사에 발견 항목 없음. 아니면 INCOMPLETE
(이유 전부를 `state.reasons` 에).

**실패한 후보**(`remediation.failed_candidate: discard`): 실패한 후보는 결과 ZIP 에 들어가지 않는다. `accepted/` 와 원본은 그대로, 상태 FAILED,
실패한 변경은 `rounds/<n>/candidate.patch` 로만 남는다. 앞 라운드에서 이미 빌드·검증·확정·재검사된 전달물은 그대로 남는다.

## 3. 승인 규칙

`remediation.require_approval` 기본값은 **false**. 이때 승인 없이 적용되는 것: 버전 글자가 프로젝트 자신의 pom 에 있는 제안
(`direct_version` · `property_version` · `own_management_version`) 중 **같은 메이저 안**의 변경. 설정과 무관하게 늘 승인을 기다리는 것:
`remediation.always_approve` 의 부류 — 기본 `major`(메이저 변경), `parent`(부모 pom 버전), `bom`(가져온 BOM 버전), `managed_override`.
부모·BOM 변경은 서버가 그 부모·BOM 자체를 `remediation.policy.pins` 로 정해 둔 경우에만 제안된다. `managed_override` 는 정의만 있고 지금
planner 가 만들지 않는다. 자동으로 하지 않는 것(잔여·수동 검토): `transitive`(새 `<dependencyManagement>` 를 더해야 함 = 구조 변경), pin 없는
부모·BOM 관리 버전, `plugin`, `vendored`, `unmapped`, `remediation.policy.deny` 에 걸린 것, `remediation.policy.allow_major` 가 false 일 때의 메이저.
`true` 로 두면 모든 제안이 승인을 기다린다. 운영에서 모든 변경에 사람 승인이 필요하면 환경 변수 `SBMIG_REMEDIATION__REQUIRE_APPROVAL=true` 로 명시한다.

승인 요청은 **제안 해시 · 원본 트리 해시 · 정책 해시 · diff** 가 저장된 제안과 모두 같고, 제안이 지금의 `accepted/` 와 정책에도 맞고(낡지 않음),
아직 `pending` 일 때만 받아들인다. 하나라도 다르면 요청 전체가 409 `approval_mismatch`(+ `fields`), 아무것도 쓰지 않는다(전부 아니면 전무).
정책 해시에는 `remediation.policy` 전부 + `require_approval` + `always_approve` + `failed_candidate` 가 들어간다 — 하나만 바꿔도 열린 제안은 모두
무효가 되어 계획을 다시 만든다. 거절한 (g:a, 버전)은 다음 라운드에서 다시 제안하지 않는다(잔여 `rejected_proposals`). 승인은 재개가 아니다.

## 4. 상태와 운영자가 할 일

| 상태 | 뜻 | 할 일 |
|---|---|---|
| `QUEUED` / `RUNNING` | 대기 / 실행 중(한 번에 한 건) | 기다린다. 멈추려면 stop(다음 경계에서) |
| `AWAITING_APPROVAL` | 승인 대기 제안이 있다 | `GET …/proposals` 로 diff 와 해시 세 개를 보고 → `POST …/approvals` 에 그대로 돌려보내며 approved / rejected → `POST …/resume` |
| `AWAITING_CONFIRMATION` | 업로드를 보냈는데 결과를 모른다 | IVAS 화면에서 그 프로젝트 이름(`<앞머리>-<작업 ID>-<라벨>`)의 분석이 있는지 본다. 없으면 resume `{"confirm": "reupload"}`, 있으면 `{"confirm": "receipt", "label", "project_id", "analysis_id"}` |
| `STOPPED` | 중지 요청을 경계에서 처리했다. 원격 IVAS 분석은 취소되지 않았다 | resume(영수증이 있으면 다시 올리지 않고 이어서 조회) |
| `INTERRUPTED` | 서버가 실행 중에 내려갔다 | 상태를 보고 resume — 자동 재개 없음 |
| `SUCCEEDED` | 성공 조건 전부 | `result.zip` 전달 |
| `INCOMPLETE` | 끝났지만 성공 아님 | 보고서의 잔여 표를 담당 팀에 넘긴다 |
| `FAILED` | 빌드·검증 실패, 경계 거부, 준비 안 됨, 내부 오류 | 이유 코드대로(아래 5절) |

이유 코드(문자열 그대로가 API 계약): `baseline_build_failed`, `candidate_build_failed`, `scan_incomplete`, `upload_uncertain`, `zip_hash_mismatch`,
`text_only_violation`, `resolution_mismatch`, `execution_boundary_refused`, `not_ready`, `pending_approval`, `rejected_proposals`,
`residual_findings`, `manual_review`, `no_candidate_version`, `rounds_exhausted`, `stop_requested`, `internal_error`, `policy_denied`,
`unknown_provenance`, `stale_location`, `inventory_incomplete`. 정의는 `sbmig/remediations/models.py`.

## 5. HTTP API (웹 파드 안, TokenGuard 뒤)

| 경로 | 결과 |
|---|---|
| `POST /api/remediations?name=<file.zip>` (본문 = ZIP) | 202 `{id, status, url}` / 400 접수 거부(코드) / 413 크기 초과 / 503 `not_ready` + `missing` |
| `GET /api/remediations?page=&size=` | 목록, 최신 먼저 |
| `GET /api/remediations/readiness` | `{ready, missing}` — 이름만, 값은 절대 없음 |
| `GET /api/remediations/<id>` | 상태 dict(status, reason, reasons, node, next_node, round, counts, pending_approvals, delivery, files, links …) |
| `GET /api/remediations/<id>/result` | 끝 상태면 report.json 내용, 아니면 409 `not_finished` |
| `GET /api/remediations/<id>/proposals` | 지금 라운드의 제안 전부(diff, 해시 세 개, 결정)와 잔여. `?all=1` 이면 모든 라운드 |
| `POST /api/remediations/<id>/approvals` | 결정 목록 → `{accepted, pending}` / 409 `approval_mismatch` / 409 `state_conflict` |
| `POST /api/remediations/<id>/resume` | 재개(확인 대기면 `confirm` 필수) |
| `POST /api/remediations/<id>/stop` | 다음 경계에서 멈춤 |
| `GET /api/remediations/<id>/files/<name>` | 정해진 파일만: 보고서 두 개, result.zip, source.zip, manifest.json, journal.jsonl, remediation.log, delivery.json, `plan-r<n>.json`, `candidate-r<n>.patch`, `build-r<n>.log`, baseline-build.log, `scan-<label>.json` |
| `DELETE /api/remediations/<id>` | 원본까지 폴더 전체 삭제(실행 중이면 409) |

작업 관리자와 라우터는 같은 패키지의 service.py 와 api.py 다.

## 6. 진단 순서

실행 폴더는 `out/_remediations/<id>/` (웹 out 폴더 아래, 쿠버네티스에서는 `/data/out/_remediations/<id>/`).

1. **`state.json`** — `status`, `reason`, `reasons`, `message`, `node`(마지막 노드), `next_node`(재개 지점), `round`, `pending_upload` /
   `pending_receipt`(검사 중 멈춘 자리), `boundary`(경계 검사 결과), `residuals`.
2. **`journal.jsonl`** — 이벤트 한 줄씩: `node_start` / `node_end`, `scan_receipt`, `scan_resume`, `upload_uncertain`, `scan_stopped`, `scan_done`,
   `ivas_upload`, `ivas_status`, `ivas_pages`, `ivas_scan_done`, `plan_written`, `plan_reused`, `plan_stale`, `policy_hash_differs`,
   `round_committed`, `next_round`, `maven_project_config`.
3. **remediation.log** — 로그(비밀 값 없음). Maven 원문은 `baseline/build.log`, `rounds/<n>/build.log`.
4. **`scans/<label>.json`** — 검사 하나: `complete`, `incomplete_reasons`(`analysis_failed`, `analysis_timeout`, `page_missing`, `total_mismatch`,
   `duplicate_result`, `summary_mismatch`, `unsupported_sw`, `zip_hash_mismatch`, `id_mismatch`, `http_error`, `stop_requested`), `findings`, `counts`, `notes`.
5. **`rounds/<n>/`** — `before.json`(해석 결과), `inventory.json`(항목별 `origin`, `location`, `reason`), `plan.json`(제안·잔여), `apply.json`
   (적용·건너뜀), `candidate.patch`, `resolved.json`(후보 해석 결과).
6. **`approvals.jsonl`** — 누가 언제 무엇을 승인·거절했나(덧붙이기만).

자주 보는 경우: `not_ready` → readiness 의 `missing`(IVAS 세 값, JDK 21, Maven, settings.xml, 승인된 저장소 호스트). `execution_boundary_refused` →
`state.boundary` 의 `code`(`project_maven_config`, `maven_extensions`, `maven_wrapper`, `symlink`, `foreign_repository`,
`foreign_plugin_repository`, `unparsable_pom`). `manual_review` 만 남음 → `inventory.json` 의 `origin` 이 `transitive` / `plugin` / `vendored` /
`unmapped` 이거나 부모·BOM 관리. `no_candidate_version` → 잔여의 `detail` 에 저장소가 준 버전 목록과 제외 이유가 있다.

## 7. 설정 키 (`remediation:` 절, `config/default.yml` 에 한국어 설명)

우선순위: `config/default.yml` → 회사 yml → 환경 변수 `SBMIG_REMEDIATION__<키>`. 비밀은 환경 변수(쿠버네티스 Secret)에서만:
`IVAS_BASE_URL`, `IVAS_USER_ID`, `IVAS_PASSWORD`. Nexus 자격증명은 서버 settings.xml 의 `<server>`(`${env.NEXUS_USER}` 등).

| 키 | 뜻 |
|---|---|
| `remediation.max_upload_mb` / `remediation.max_entries` / `remediation.max_unpacked_mb` / `remediation.max_entry_mb` / `remediation.max_compression_ratio` | ZIP 한도(압축률은 풀린 크기 1 MiB 이상 항목만) |
| `remediation.trusted_projects` | 신뢰 프로젝트 확인. Maven 이 올라온 프로젝트의 플러그인 코드를 서버에서 실행하므로, 운영자가 확인하고 true 로 바꿔야 접수한다(기본 false = 접수 안 함) |
| `remediation.require_approval` | false = 위험 부류만 승인 대기, true = 모든 제안 |
| `remediation.always_approve` | 늘 승인이 필요한 부류 |
| `remediation.max_rounds` | 계획 → 적용 → 빌드 → 재검사 반복 한도 |
| `remediation.failed_candidate` | 후보 빌드 실패 정책(지금은 `discard` 하나) |
| `remediation.project_maven_config` | `.mvn/maven.config` · `jvm.config` · `extensions.xml` 발견 시 refuse / ignore(Maven 3.9 는 끌 수 없어 기본 refuse) |
| `remediation.maven_wrapper` | `mvnw` 발견 시(서버 Maven 만 쓰므로 기본 ignore) |
| `remediation.allowed_repository_hosts` | 승인된 저장소 호스트. 비우면 서버 settings.xml 의 미러·저장소 호스트 |
| `remediation.foreign_repository` | pom 이 승인되지 않은 호스트 저장소를 선언했을 때 |
| `remediation.build_timeout_minutes` | package 빌드 한 번의 한도 |
| `remediation.ivas_timeout_seconds` / `remediation.ivas_upload_timeout_seconds` | IVAS 요청 / 업로드 한도 |
| `remediation.ivas_poll_seconds` / `remediation.ivas_analysis_timeout_minutes` | 상태 조회 간격 / 분석 대기 한도(넘으면 미완료) |
| `remediation.ivas_page_size` | 결과 한 쪽 크기 |
| `remediation.ivas_project_prefix` | IVAS 프로젝트 이름 앞머리 |
| `remediation.policy.pins` | "g:a" → 이 버전으로만(부모·BOM 좌표면 그 버전 변경 제안) |
| `remediation.policy.deny` / `remediation.policy.deny_versions` | 자동 변경 금지 / 후보에서 뺄 버전 |
| `remediation.policy.allow_prerelease` / `remediation.policy.allow_major` | 시험판 허용 / 메이저 변경 제안 허용 |

Maven 쪽은 이관과 같은 키를 쓴다: `build.mvn`, `build.java_home`, `build.settings_file`(조치에서는 필수), `build.local_repo`, `build.extra_args`
(TLS 끄기·다른 `-s` / `-f` 는 빠진다). 웹 파드는 조치용 로컬 저장소를 따로 쓴다: `<local repo>-remediation`.

## 8. 고칠 자리

| 요청 | 파일 |
|---|---|
| IVAS 응답 필드·경로·상태 코드·완전성 규칙 | `sbmig/remediations/ivas.py` (`PAGE_PATHS`, `ANALYSIS_ID_KEYS`, `IN_PROGRESS`, `_library_finding`, `_reconcile`) |
| ZIP 접수 규칙, 실행 경계, 전달 ZIP 모양 | `sbmig/remediations/intake.py` (`check_boundary`, `freeze_delivery`) |
| 새 출처(origin) 판정, 발견 항목 ↔ pom 줄 연결 | `sbmig/remediations/inventory.py` |
| 버전 고르기, 승인 부류, 정책 해시, 승인 검증 | `sbmig/remediations/planner.py` (`_choose_target`, `plan_round`, `policy_hash`, `validate_decision`) |
| 글자 치환, "버전 글자만" · "해석 = 계획" 검증 | `sbmig/remediations/apply.py` |
| Maven 명령·준비 조건·해석 읽기 | `sbmig/remediations/maven.py` |
| 노드 순서, 멈춤·재개, 성공 판정 | `sbmig/remediations/graph.py` (`_evaluate`, `_Holder.scan`) |
| 보고서 문장·다음 할 일 | `sbmig/remediations/report.py` |
| 상태·이유 코드·자료형(계약) | `sbmig/remediations/models.py` — 바꾸면 `docs/INTERFACES.md` 의 remediations 절도 |
| 새 설정 키 | `sbmig/core/config.py` 의 `RemediationConfig` + `config/default.yml` (한국어 설명) |
| HTTP 경로·작업 관리 | 같은 패키지의 api.py / service.py |

시험은 `tests/remediations/` (가짜 IVAS·Maven·저장소, 합성 ZIP, 네트워크·`mvn` 없음).

## 확인하는 법

```bash
sed -n 1,25p sbmig/remediations/__init__.py                 # 패키지 지도
grep -n '^R_\|^QUEUED\|^NODES\|^TERMINAL\|^RESUMABLE' sbmig/remediations/models.py
grep -n "def _evaluate" -A45 sbmig/remediations/graph.py    # 성공 조건
grep -n "needs = \|def validate_decision\|def policy_hash" sbmig/remediations/planner.py
grep -n "PAGE_PATHS\|ANALYSIS_ID_KEYS\|IN_PROGRESS\|FAILED_STATES" sbmig/remediations/ivas.py
grep -n "remediation:" -A45 config/default.yml
.venv/bin/python -m pytest tests/remediations -q -o addopts=""
```
