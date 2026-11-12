# 취약점 조치 보고서와 사내 라이브러리 안내

> 다루는 것: 조치 결과 보고서(`report.md`)의 구성과 절마다 할 일, 용어표, 사내 라이브러리 안내 규칙(어떤 항목이 담당자 몫으로 빠지는지), 제안의 적용 범위, 영어로 저장된 문장을 한국어로 보여 주는 규칙, 바꿀 때 고칠 자리.
> 언제 읽나: 보고서의 어느 절을 보고 무엇을 해야 하는지 물을 때, "이 라이브러리는 왜 자동으로 안 고쳤나" 를 볼 때, 보고서 문장·구성이나 사내 라이브러리 판정을 바꾸기 전에.
> 함께 읽을 파일: [11-remediations.md](11-remediations.md) (흐름·상태·승인·설정 키), [06-company-rules-restructure.md](06-company-rules-restructure.md) (회사 부모·필수·선택 아티팩트)

## 1. 보고서가 어디에 있고 누가 쓰나

- 사람용 `report.md`(한국어 결과 보고서)와 도구용 `report.json`(코드와 영어 문장 그대로). 이관에 이어 돈 조치면 `<이관 실행 폴더>/remediation/` 아래.
- 읽는 사람: 이관된 프로젝트를 맡은 개발자와 그 관리자. 첫 화면만 보고 "무슨 일이 있었고 내가 무엇을 해야 하는지" 알 수 있어야 한다.
- 조치 그래프의 마지막 노드 `report` 가 쓴다(`sbmig/remediations/report.py`의 `write_report`). 승인·확인 대기로 멈춘 실행은
  보고서 없이 `state.json`만 남는다(11의 6절).
- 같은 입력이면 같은 바이트다. 모든 줄이 비밀 값과 실행 폴더의 절대 경로를 지우는 정제를 거친다.
- 이관 보고서에는 `취약점 조치` 절 하나만 있다(상태 코드·사유 코드·집계·파일). 사내 라이브러리 몫이 있으면 "사내 라이브러리 (담당자 요청): 라이브러리 N개" 한 줄을 더 적는다.
- `sbmig.log`의 `chained remediation <id>: <상태> <이유> — <문장>` 한 줄이 끝난 까닭을 말한다(600자, 비밀 없음).

## 2. 절 순서와 읽는 사람이 할 일

빈 절은 쓰지 않는다(조치한 것이 없으면 "조치 완료" 절이 없다). 상태·사유 코드는 접힌 "처리 기록" 에만 있다.

| 절 | 담는 것 | 할 일 |
|---|---|---|
| 제목 아래 결과 블록 | 굵은 한 문장(✅ 조치를 마쳤습니다 / ⚠️ 일부만 조치되었습니다 / ❌ 조치하지 못했습니다 / ⏸ 승인을 기다리고 있습니다 / ⏸ 확인이 필요합니다 / ⏹ 조치를 멈췄습니다 / ⏹ 조치가 중단되었습니다) + 숫자와 까닭 한두 문장 | 여기와 "해야 할 일" 만 봐도 판단이 서야 한다 |
| (있을 때) 주의 문단 | 결과 ZIP이 빌드에 실패한 수정본이라는 사실, 재검사 없음, 빌드 로그, 원본·이전 라운드 결과 ZIP의 위치 | 그 ZIP을 배포하지 않는다 |
| 요약 | 표 1 `구분 / 취약점 / 라이브러리`: 조치 전 · 조치 완료 · 승인 대기 · (있을 때) 사내 라이브러리 담당자 요청 · 직접 확인 필요 · 조치 대상에서 제외 · 조치 후 남은 취약점. 표 2 `위험도 / 기준 (IVAS 점수) / 조치 전 / 조치 후`. 그 아래 "결과 파일:" 한 줄 | — |
| 해야 할 일 | 번호 목록. 항목마다 "**제목 (N건)** — 설명 ~해 주세요." 이 상태에서 할 수 있는 일만 쓴다 | 위에서부터 한다 |
| 조치 완료 — N건 | 표 `위험도 / 라이브러리 / 버전 변경 / 수정한 곳 / 해결된 취약점`, 그 아래 제안마다 버전 확인 · 수정 방식 · 적용 범위 · 참고 | 결과 ZIP을 받는다 |
| 승인 대기 — 제안 N건 | 표 `위험도 / 라이브러리 / 버전 변경 / 수정할 곳 / 주의 / 해결할 취약점` + 제안마다 같은 설명 | 화면의 체크박스 목록에서 고르고 `선택한 항목 적용` |
| 사내 라이브러리에서 발생한 취약점 — N건 | 안내 문구, 사내 아티팩트별 묶음(회사 부모 pom 이면 제목에 표시). 표 `위험도 / 취약한 라이브러리 / 버전 / 의존 경로 / 취약점` | 그 아티팩트의 아키텍처 담당자에게 묶음째 요청한다 |
| 직접 확인이 필요한 취약점 — N건 | 나머지 잔여를 사유별로 묶은 것 — 설명은 묶음마다 한 번. 표 `위험도 / 라이브러리 / 버전 / [구분] / [사용 모듈] / 취약점 / [내용]` | 묶음의 설명대로 프로젝트 담당 팀이 조치한다 |
| 조치 대상에서 제외한 항목 — N건 | `remediation.ignore_actions` 코드로 제외한 검사 결과. 표 `위험도 / 취약점 / 라이브러리 / 버전 / IVAS 조치 상태` | — |
| 변경하지 않은 것 | 소스·구조는 그대로라는 보증, 원본 ZIP 위치 | — |
| 처리 기록 (접힘) | 작업 ID, 상태 코드 · 사유 코드, 집계 표, 1. 조치 전 빌드 → 2. 조치 전 검사 (IVAS) → 라운드별(제안 표, 적용 범위, 수정본 빌드, 수정본 채택, 검증 참고·문제, 재검사) → 결과 파일 → 빌드 설정 점검. 그 아래 접힌 "남은 취약점 전체 목록" | 문의할 때 이 절의 코드를 전한다 |

위험도 표시: IVAS 점수가 `remediation.severity_red` / `remediation.severity_orange` / `remediation.severity_yellow`(기본 9.0 / 7.0 / 4.0)
이상이면 🔴 심각 / 🟠 높음 / 🟡 보통, 그보다 낮거나 점수가 없으면 "낮음·점수 없음"(표시 글자 없음). 라이브러리 표는 맨 앞
`위험도` 칸(그 줄의 최고 점수)으로 시작하고 위험한 줄이 위에 온다. 취약점 칸은 "🔴 CVE-… (9.8)" 처럼 점수 높은 순으로 3개까지,
나머지는 "외 N건". 조치한 취약점은 조치 후 검사에 없으므로 점수를 조치 전 검사에서 찾는다. 판정은 보여 주는 소수 한 자리 값으로 한다.

읽는 요령: 묶음의 모든 행이 같은 값이면 칸을 빼고 표 위에 한 번 쓴다("아래 항목은 모두 간접 의존성입니다.", "사용 모듈: …").
의존 경로는 artifactId만 " → " 로 잇고 취약한 라이브러리로 끝난다(버전은 버전 칸). classifier가 붙은 의존성은 좌표 뒤 괄호에
classifier를 쓴다. 숫자: 취약점은 건, 라이브러리·파일은 개, 라운드는 회, 1,000 이상은 쉼표, 크기는 MB, pom 줄은 "41행".
규칙이 모르는 영어 문장은 "원문: `…`" 으로 그대로 보인다 — 6절의 번역 규칙에 그 문장이 없다는 뜻이다.

### 용어표 (보고서·승인 화면·이관 보고서에서 같은 말을 쓴다)

| 코드의 말 | 보고서의 말 |
|---|---|
| fixed / pending approval / manual review | 조치 완료 / 승인 대기 / 직접 확인 필요 |
| in-house / residual / ignored | 사내 라이브러리 (담당자 요청) / 조치하지 못한 항목 / 제외한 항목 |
| baseline build · scan / final scan | 조치 전 빌드 · 검사 / 조치 후 검사 (재검사) |
| candidate build / round committed / delivery ZIP | 수정본 빌드 / 수정본 채택 / 결과 ZIP |
| modules a change governs / also-modules | 적용 범위 / 적용되지 않을 수 있는 모듈 |
| via / version text / evidence / risk | 의존 경로 / 버전 표기 / 버전 확인 / 주의 |
| `direct_literal` / `direct_property` / `own_management` | 직접 의존성 (pom에 버전 표기) / 직접 의존성 (property로 버전 지정) / 프로젝트의 dependencyManagement |
| `parent_managed` / `bom_managed` / `vendored` / `unmapped` | 부모 pom에서 버전 지정 / BOM에서 버전 지정 / 프로젝트에 포함된 jar 파일 / 의존성 트리에서 찾지 못함 |
| `managed_override` / execution boundary | dependencyManagement에 버전 지정 추가 / 빌드 설정 점검 |

쓰지 않는 말: 다스리다, 닿다, 들어온 길, 버전 글자, 남긴 항목, 뺀 항목, 사람 검토, 고친 것, "~할 것" 제목, 문장 안 괄호 영어 코드.
pom · BOM · classifier · dependencyManagement · property · scope · jar는 영어 그대로 쓴다. 회사 문구 "해당 아티팩트는 예외처리가
가능합니다. 담당자에게 연락 부탁드립니다." 는 그대로 둔다.

## 3. 사내 라이브러리 안내 (이유 코드 `inhouse_artifact`)

회사 규칙: 취약한 라이브러리가 **사내 아티팩트에서 왔으면 프로젝트에서 고치지 않는다.** 그 아티팩트의 pom은 담당자만 고칠 수 있고,
프로젝트에 덮어쓰기를 넣으면 담당자 쪽 수정과 어긋난다. 그런 항목은 제안하지 않고 잔여로 두며, 보고서가 따로 묶어 문구를 붙인다.

사내 아티팩트 = 회사 artifacts yml의 목록 가운데 `remediation.inhouse_groups` 가 고른 것.

| 그룹 | artifacts yml의 자리 | "거기서 왔다" 의 뜻 |
|---|---|---|
| `mandatory` | `artifacts.mandatory` | 의존성 트리에서 그 아티팩트(별칭 포함)를 거쳐 가져왔거나, 취약한 것이 그 아티팩트 자체 |
| `optional` | `artifacts.optional` | 위와 같다 |
| `parents` | `artifacts.parent`, `artifacts.boot_parent`, `artifacts.comp_parent` | 그 부모를 상속하는 모듈에서 (1) 부모 쪽이 버전을 관리하는 의존성(출처가 분명할 때만), (2) 프로젝트 pom이 선언하지 않은 의존성 = 부모가 물려준 것과 그것이 끌고 온 것 |

판정과 계획:

- 인벤토리가 모듈마다 판정해 항목에 적는다: `inhouse`(그 의존성을 가져온 사내 아티팩트의 g:a), `inhouse_modules`(그렇게 가져온 모듈).
  경로 판정은 `sbmig/remediations/inhouse.py`의 `inhouse_on_path`, 부모 파생은 `sbmig/remediations/inventory.py`의 `_parent_inhouse`.
- 계획(`sbmig/remediations/planner.py`의 `_inhouse_residual`): 항목이 사내 아티팩트 자체이거나, 간접·부모·BOM 관리 항목이
  **모든** 모듈에서 사내 아티팩트를 거쳐 왔으면 잔여 `inhouse_artifact`. 일부 모듈만 그러면 예전처럼 계획하고, "직접 확인" 표의 라이브러리 칸에 "일부 모듈은 사내 라이브러리에서 가져옴" 을 붙인다.
- 예외 하나: 회사 부모 몫인 관리 버전인데 서버 정책이 그 부모를 pin 했으면(`remediation.policy.pins`) 부모 버전을 올리는 제안을
  낸다. 아키텍처 담당이 고친 부모를 내놓았을 때 쓰는 길이고, 부모 버전 제안은 늘 승인 대상이다.
- 프로젝트가 자기 pom에 버전을 적은 외부 라이브러리는 사내 아티팩트와 무관하게 예전처럼 조치한다.
- 프로젝트가 직접 선언한 외부 라이브러리가 가져온 것은 사내 몫이 아니다("직접 확인이 필요한 취약점" 쪽에 남는다).
- 사내 아티팩트의 목록이 바뀌면 정책 해시가 바뀐다 — 전에 만든 승인 후보는 낡은 계획이 되어 다시 계획한다.
- 상태는 다른 잔여와 같다: 남은 것이 사내 몫뿐이어도 `INCOMPLETE`, 이유에 `inhouse_artifact`. 건수는 `state.json`의 `counts.residual_inhouse`.

| 키 | 뜻 |
|---|---|
| `remediation.inhouse_groups` | 사내 아티팩트로 볼 그룹(기본 mandatory, optional, parents). 비우면 이 안내를 쓰지 않는다 |
| `remediation.inhouse_message` | 보고서의 그 절 첫머리에 붙는 문구(기본 "해당 아티팩트는 예외처리가 가능합니다. 담당자에게 연락 부탁드립니다.") |

## 4. 제안의 적용 범위

같은 라이브러리의 같은 버전을 모듈마다 다른 경로로 가져올 수 있다. 한 모듈은 자기 pom의 버전 표기로, 다른 모듈은 간접 의존성이나
부모 pom의 버전 지정으로 받는 식이다. 그래서 제안은 **자기 버전 표기가 적용되는 모듈(적용 범위)**만 책임진다(코드: `modules`).

- 인벤토리 항목의 `modules` 는 그 버전을 사용하는 모든 모듈, `also_modules` 는 그 버전 표기가 적용된다고 볼 근거가 없는 모듈.
- 제안의 `modules` = 적용 범위, `also_modules` = 적용되지 않을 수 있는 모듈. 둘 다 승인 해시에 들어가지 않는다.
- 검증(`sbmig/remediations/apply.py`의 `verify_resolution`)은 적용 범위의 모듈과 그 모듈이 만든 산출물에서만 전 → 후를 요구한다.
  나머지 모듈이 옛 버전에 남으면 실패가 아니라 "검증 참고" 줄이다. 수정본은 채택되고, 재검사에 남은 것을 다음 라운드가 그
  모듈만으로 다시 계획한다(dependencyManagement 버전 지정 제안 또는 잔여).
- 적용 범위의 모듈이 계획한 버전이 되지 않았을 때만 `resolution_mismatch`(FAILED, 수정본을 버림)다.
- 보고서: "조치 완료" · "승인 대기" 의 제안 아래 "적용 범위" 줄, 처리 기록의 라운드에 "적용 범위 (p-…)" 와 "적용되지 않을 수 있는 모듈 (p-…)" 줄.

## 5. "직접 확인이 필요한 취약점" 의 묶음

잔여를 (이유 코드, 설명의 종류)로 묶는다. 설명의 종류는 영어 설명 문장의 패턴이 정하고, 묶음마다 제목과 한 문장 설명이 있다.
회사 실행에서 자주 보일 묶음:

| 묶음 | 뜻 | 누가 무엇을 |
|---|---|---|
| 루트 pom을 상속하지 않는 모듈 | 모듈이 회사 부모 pom을 상속해 루트 pom의 버전 지정이 적용되지 않는다 | 프로젝트 담당: 그 모듈 pom에서 버전을 지정하거나 그 의존성을 가져오는 라이브러리를 올린다 |
| classifier가 붙은 의존성 | jar가 아닌 형식이거나 classifier가 붙어 버전 지정으로 조치할 수 없다 | 프로젝트 담당 |
| 패치 버전 정보 없음 / 저장소에 올릴 수 있는 버전 없음 / 메이저 버전 변경만 가능 | 올릴 버전을 근거 있게 정하지 못했다 | 보안·프로젝트 담당이 버전을 정하거나 서버 정책에 버전 지정을 추가한다 |
| 모듈 pom에서 버전을 직접 지정 | 직접 지정한 버전이 dependencyManagement 보다 우선한다 | 프로젝트 담당: 그 버전을 수정한다 |
| 빌드 플러그인 / 프로젝트에 포함된 jar 파일 | 자동 수정 대상이 아니다 | 프로젝트 담당 |

전체 묶음 표는 `sbmig/remediations/i18n.py`의 `GROUPS` 다.

## 6. 한국어로 보여 주는 규칙

- **저장은 그대로**: `state.json`, `plan.json`, `report.json`, HTTP 응답의 문장은 영어이고 코드(출처·이유·종류)는 문자열 그대로가 계약이다.
- **보여 줄 때만 한국어**: `sbmig/remediations/i18n.py` 가 코드 → 한국어 라벨 표와 영어 문장 → 한국어 문장 규칙(`RULES`, `ko_text`)을
  가진다. g:a, 버전, 모듈 이름, 파일 경로, 제안 ID 는 글자 그대로 남긴다.
- 모르는 문장은 지어내지 않는다: `ko_text`(화면의 `*_ko`)는 그대로 돌려주고, 보고서용 `ko_display` 는 "원문: `…`" 으로 감싼다.
  Maven의 빌드 요약(처리 기록의 코드 블록), 도구 내부 오류의 세부("원문:")는 일부러 옮기지 않는다.
- 승인 화면: `GET /api/remediations/<id>/proposals` 가 제안마다 `note_ko`, 잔여마다 `detail_ko` · `origin_ko` · `reason_ko` 를 더 싣고,
  화면은 `note_ko` 를 보여 준다. 상태 응답(`GET /api/remediations/<id>`)은 `message_ko` · `status_ko` 를 더 싣고 화면은
  `message_ko` 를 먼저 보여 준다. 원래 키는 그대로다.
- **새 영어 문장을 코드에 더하면** `RULES`에 규칙을 더하고 `tests/remediations/test_i18n.py`에 표본 문장을 더한다. 그 시험은 규칙마다 표본이
  있는지, 옮긴 글에 한글이 있고 식별자가 남았는지 본다. 잔여 설명이면 `GROUPS`에 묶음도 더한다. 한국어는 2절의 용어표대로,
  합니다체로 한 문장에 사실 하나만 쓴다. 조사는 앞말에 붙인다(pom을, ZIP은, jar가). 값(좌표·버전·모듈·경로) 뒤에는 받침에 따라
  바뀌는 조사를 붙이지 않고 명사로 받거나("2.25.4 버전이", "`app-boot` 모듈에") 의·에·에서·입니다처럼 바뀌지 않는 말을 붙인다. `tests/remediations/test_report.py`의 `BANNED_WORDS` 가 쓰지 않는 말을 막는다.

## 7. 고칠 자리

| 요청 | 파일 |
|---|---|
| 사내 아티팩트로 볼 목록·별칭 | 회사 artifacts yml + `remediation.inhouse_groups` (`sbmig/remediations/inhouse.py`의 `inhouse_keys`) |
| "사내에서 왔다" 의 판정(경로, 부모 파생) | `sbmig/remediations/inhouse.py`, `sbmig/remediations/inventory.py` (`_parent_inhouse`, `_fill_inhouse`) |
| 사내 몫을 잔여로 둘지 제안할지, pin 예외 | `sbmig/remediations/planner.py` (`_inhouse_residual`, `_change_for`) |
| 안내 문구 | `remediation.inhouse_message` |
| 위험도 표시의 기준 점수·표시 글자 | `remediation.severity_red` 등 세 키, `sbmig/remediations/i18n.py` (`severity_of`, `SEVERITY_LEVELS`) |
| 보고서의 절·표·줄임 규칙 | `sbmig/remediations/report.py` (`build_report_md`) |
| 한국어 라벨·문장·묶음 | `sbmig/remediations/i18n.py` |
| 이관 보고서의 조치 절 | `sbmig/report/markdown.py`, 한국어는 `sbmig/report/i18n.py` |
| 승인 화면의 문구 | `sbmig/web/static/index.html`, `sbmig/remediations/service.py` (`proposals`) |

## 확인하는 법

```bash
grep -n "inhouse" config/default.yml sbmig/core/config.py
grep -n "^def " sbmig/remediations/inhouse.py
grep -n "def _inhouse_residual" -A16 sbmig/remediations/planner.py
grep -n "^GROUPS" -A6 sbmig/remediations/i18n.py
.venv/bin/python -m pytest tests/remediations/test_inhouse.py tests/remediations/test_i18n.py tests/remediations/test_report.py -q -o addopts=""
```
