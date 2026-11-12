# 취약점 조치 보고서와 사내 라이브러리 안내

> 다루는 것: 조치 보고서(`report.md`)의 구성과 절마다 운영자가 할 일, 사내 라이브러리 안내 규칙(어떤 항목이 담당자 몫으로 빠지는지), 제안의 책임 범위(다스리는 모듈), 영어로 저장된 문장을 한국어로 보여 주는 규칙, 바꿀 때 고칠 자리.
> 언제 읽나: 운영자가 보고서의 어느 절을 보고 무엇을 해야 하는지 물을 때, "이 라이브러리는 왜 자동으로 안 고쳤나" 를 볼 때, 보고서 문장·구성이나 사내 라이브러리 판정을 바꾸기 전에.
> 함께 읽을 파일: [11-remediations.md](11-remediations.md) (흐름·상태·승인·설정 키), [06-company-rules-restructure.md](06-company-rules-restructure.md) (회사 부모·필수·선택 아티팩트)

## 1. 보고서가 어디에 있고 누가 쓰나

- 사람용 `report.md`(한국어)와 도구용 `report.json`(코드와 영어 문장 그대로). 이관에 이어 돈 조치면 `<이관 실행 폴더>/remediation/` 아래.
- 조치 그래프의 마지막 노드 `report` 가 쓴다(`sbmig/remediations/report.py` 의 `write_report`). 승인·확인 대기로 멈춘 실행은
  보고서 없이 `state.json` 만 남는다(11 의 6절).
- 같은 입력이면 같은 바이트다. 모든 줄이 비밀 값과 실행 폴더의 절대 경로를 지우는 정제를 거친다.
- 이관 보고서에는 `취약점 조치` 절 하나만 있다(상태·이유·건수·파일). 사내 아티팩트 몫이 있으면 그 건수를 한 줄 더 적는다.
- `sbmig.log` 의 `chained remediation <id>: <상태> <이유> — <문장>` 한 줄이 끝난 까닭을 말한다(600자, 비밀 없음).

## 2. 절 순서와 운영자가 할 일

| 절 | 담는 것 | 운영자가 할 일 |
|---|---|---|
| 제목 아래 결과 한 줄 | 한국어 상태 + 코드 + 무엇이 어떻게 됐는지 한 문장 | 여기와 다음 절만 봐도 판단이 서야 한다 |
| (있을 때) 실패한 수정본 경고 | 결과 ZIP 이 빌드가 실패한 수정본이라는 사실, 재검사 없음, 원본·앞 라운드 ZIP 의 자리 | 그 ZIP 을 그대로 배포하지 않는다 |
| 한눈에 보기 | 찾은 것 / 위험도별 건수 / 고친 것 / 승인 대기 / 사내 라이브러리 경유 / 사람 검토 / 남은 것 / 결과 ZIP, 그 아래 위험도 표시의 기준 한 줄 | — |
| 다음에 할 일 | 순서 있는 할 일, 이 상태가 된 이유(코드와 뜻), 상태 메시지 | 위에서부터 한다 |
| 고친 것 | 확정된 제안: 라이브러리, 버전 전 → 후, 바꾼 곳, 근거, 해소 대상 취약점, 다스리는 모듈 | 결과 ZIP 을 받는다 |
| 승인을 기다리는 것 | 대기 제안과 위험 부류 | 화면의 체크박스 목록에서 고르고 `선택한 항목 적용` |
| 사내 라이브러리에서 온 취약점 | 안내 문구, 사내 아티팩트별 묶음(취약 라이브러리, 버전, 들어온 길, 모듈, 취약점) | 그 아티팩트의 담당자(아키텍처 담당)에게 묶음째 넘긴다 |
| 사람이 확인할 것 | 나머지 잔여를 이유별로 묶은 것 — 설명은 묶음마다 한 번 | 묶음의 설명대로 프로젝트 담당 팀이 본다 |
| 조치 대상에서 뺀 항목 | `remediation.ignore_actions` 코드로 뺀 발견 항목 | — |
| 진행 기록 | 기준 빌드, 기준 검사, 라운드별 제안·빌드·검증 메모, 전달 ZIP, 실행 경계 점검 | 까닭을 따질 때만 |
| 바꾸지 않은 것 | 소스·구조는 그대로라는 보증 | — |

위험도 표시: IVAS 점수가 `remediation.severity_red` / `remediation.severity_orange` / `remediation.severity_yellow`(기본 9.0 / 7.0 / 4.0)
이상이면 🔴 심각 / 🟠 높음 / 🟡 보통. 그보다 낮거나 점수가 없으면 표시가 없다(전부 색이면 눈이 가지 않는다). 라이브러리 표는 맨 앞
`위험도` 칸(그 줄의 최고 점수)으로 시작하고 위험한 줄이 위에 온다. 취약점 번호마다 표시가 붙고 점수 높은 순으로 3개까지 보인다.
고친 취약점은 최종 검사에 없으므로 점수를 기준 검사에서 찾는다. 판정은 보여 주는 소수 한 자리 값으로 한다.

읽는 요령: 같은 값이 줄마다 되풀이되는 칸은 묶음 머리의 "모든 항목 공통" 줄로 올라간다. 긴 취약점·모듈 목록은 앞의 3개와 "외 N" 으로
줄이고, 전체는 진행 기록과 `report.json` 에 있다. 보고서에 영어 문장이 보이면 6절의 번역 규칙이 그 문장을 모르는 것이다.

## 3. 사내 라이브러리 안내 (이유 코드 `inhouse_artifact`)

회사 규칙: 취약한 라이브러리가 **사내 아티팩트에서 왔으면 프로젝트에서 고치지 않는다.** 그 아티팩트의 pom 은 담당자만 고칠 수 있고,
프로젝트에 덮어쓰기를 넣으면 담당자 쪽 수정과 어긋난다. 그런 항목은 제안하지 않고 잔여로 남기며, 보고서가 따로 묶어 문구를 붙인다.

사내 아티팩트 = 회사 artifacts yml 의 목록 가운데 `remediation.inhouse_groups` 가 고른 것.

| 그룹 | artifacts yml 의 자리 | "거기서 왔다" 의 뜻 |
|---|---|---|
| `mandatory` | `artifacts.mandatory` | 의존성 트리에서 그 아티팩트(별칭 포함)를 거쳐 들어왔거나, 취약한 것이 그 아티팩트 자체 |
| `optional` | `artifacts.optional` | 위와 같다 |
| `parents` | `artifacts.parent`, `artifacts.boot_parent`, `artifacts.comp_parent` | 그 부모를 상속하는 모듈에서 (1) 부모 쪽이 버전을 관리하는 의존성(출처가 분명할 때만), (2) 프로젝트 pom 이 선언하지 않은 의존성 = 부모가 물려준 것과 그것이 끌고 온 것 |

판정과 계획:

- 인벤토리가 모듈마다 판정해 항목에 적는다: `inhouse`(끌고 온 사내 아티팩트의 g:a), `inhouse_modules`(그렇게 들어온 모듈).
  경로 판정은 `sbmig/remediations/inhouse.py` 의 `inhouse_on_path`, 부모 파생은 `sbmig/remediations/inventory.py` 의 `_parent_inhouse`.
- 계획(`sbmig/remediations/planner.py` 의 `_inhouse_residual`): 항목이 사내 아티팩트 자체이거나, 간접·부모·BOM 관리 항목이
  **모든** 모듈에서 사내 아티팩트를 거쳐 들어왔으면 잔여 `inhouse_artifact`. 일부 모듈만 그러면 예전처럼 계획하고 줄에 표시만 한다.
- 예외 하나: 회사 부모 몫인 관리 버전인데 서버 정책이 그 부모를 pin 했으면(`remediation.policy.pins`) 부모 버전을 올리는 제안을
  낸다. 아키텍처 담당이 고친 부모를 내놓았을 때 쓰는 길이고, 부모 버전 제안은 늘 승인 대상이다.
- 프로젝트가 자기 pom 에 버전 글자로 적은 외부 라이브러리는 사내 아티팩트와 무관하게 예전처럼 고친다.
- 프로젝트가 직접 선언한 외부 라이브러리가 끌고 온 것은 사내 몫이 아니다(사람 검토 쪽에 남는다).
- 사내 아티팩트의 목록이 바뀌면 정책 해시가 바뀐다 — 전에 만든 승인 후보는 낡은 계획이 되어 다시 계획한다.
- 상태는 다른 잔여와 같다: 남은 것이 사내 몫뿐이어도 `INCOMPLETE`, 이유에 `inhouse_artifact`. 건수는 `state.json` 의 `counts.residual_inhouse`.

| 키 | 뜻 |
|---|---|
| `remediation.inhouse_groups` | 사내 아티팩트로 볼 그룹(기본 mandatory, optional, parents). 비우면 이 안내를 쓰지 않는다 |
| `remediation.inhouse_message` | 보고서의 그 절 첫머리에 붙는 문구(기본 "해당 아티팩트는 예외처리가 가능합니다. 담당자에게 연락 부탁드립니다.") |

## 4. 제안의 책임 범위 (다스리는 모듈)

같은 라이브러리의 같은 버전이 모듈마다 다른 길로 들어올 수 있다. 한 모듈은 자기 pom 의 버전 글자로, 다른 모듈은 간접 의존성이나
부모 관리로 받는 식이다. 그래서 제안은 **자기 버전 글자가 다스리는 모듈**만 책임진다.

- 인벤토리 항목의 `modules` 는 그 버전이 해석된 모든 모듈, `also_modules` 는 그 글자가 다스린다고 볼 근거가 없는 모듈.
- 제안의 `modules` = 다스리는 모듈, `also_modules` = 나머지. 둘 다 승인 해시에 들어가지 않는다.
- 검증(`sbmig/remediations/apply.py` 의 `verify_resolution`)은 다스리는 모듈과 그 모듈이 만든 산출물에서만 전 → 후를 요구한다.
  나머지 모듈이 옛 버전에 남으면 실패가 아니라 검증 메모의 참고 줄이다. 라운드는 확정되고, 재검사에 남은 것을 다음 라운드가 그
  모듈만으로 다시 계획한다(override 제안 또는 잔여).
- 다스리는 모듈이 계획 버전이 되지 않았을 때만 `resolution_mismatch`(FAILED, 후보를 버림)다.
- 보고서: "고친 것" 의 제안 아래에 `다스리는 모듈` 과 `이 수정이 닿지 않을 수 있는 모듈` 줄.

## 5. "사람이 확인할 것" 의 묶음

잔여를 (이유 코드, 설명의 종류)로 묶는다. 설명의 종류는 영어 설명 문장의 패턴이 정하고, 묶음마다 제목과 한 문장 설명이 있다.
회사 실행에서 자주 보일 묶음:

| 묶음 | 뜻 | 누가 무엇을 |
|---|---|---|
| 루트 pom 을 상속하지 않는 모듈 | 모듈이 회사 부모를 상속해 루트 pom 의 덮어쓰기가 닿지 않는다 | 프로젝트 담당: 그 모듈 pom 에서 버전을 정하거나 끌고 온 의존성을 올린다 |
| 분류자·다른 종류로 들어옴 | jar 가 아닌 종류·분류자 변종이라 일반 덮어쓰기로 잡히지 않는다 | 프로젝트 담당 |
| 패치 버전 정보 없음 / 저장소에 맞는 버전 없음 / 메이저 변경만 가능 | 올릴 버전을 근거 있게 정하지 못했다 | 보안·프로젝트 담당이 버전을 정하거나 정책 pin 을 건다 |
| 모듈이 버전을 직접 선언함 | 선언한 버전이 덮어쓰기보다 앞선다 | 프로젝트 담당: 그 선언을 고친다 |
| 빌드 플러그인 / 프로젝트에 든 파일 | 자동 변경 대상이 아니다 | 프로젝트 담당 |

전체 묶음 표는 `sbmig/remediations/i18n.py` 의 `GROUPS` 다.

## 6. 한국어로 보여 주는 규칙

- **저장은 그대로**: `state.json`, `plan.json`, `report.json`, HTTP 응답의 문장은 영어이고 코드(출처·이유·종류)는 문자열 그대로가 계약이다.
- **보여 줄 때만 한국어**: `sbmig/remediations/i18n.py` 가 코드 → 한국어 라벨 표와 영어 문장 → 한국어 문장 규칙(`RULES`, `ko_text`)을
  가진다. g:a, 버전, 모듈 이름, 파일 경로, 제안 ID 는 글자 그대로 남긴다.
- 모르는 문장은 **바꾸지 않고 그대로** 낸다(지어내지 않는다). Maven 의 빌드 요약, 준비 조건 문제, 도구 내부 오류의 세부는 일부러 옮기지 않는다.
- 승인 화면: `GET /api/remediations/<id>/proposals` 가 제안마다 `note_ko`, 잔여마다 `detail_ko` · `origin_ko` · `reason_ko` 를 더 싣고,
  화면은 `note_ko` 를 보여 준다. 원래 키는 그대로다.
- **새 영어 문장을 코드에 더하면** `RULES` 에 규칙을 더하고 `tests/remediations/test_i18n.py` 에 표본 문장을 더한다. 그 시험은 규칙마다 표본이
  있는지, 옮긴 글에 한글이 있고 식별자가 남았는지 본다. 잔여 설명이면 `GROUPS` 에 묶음도 더한다.

## 7. 고칠 자리

| 요청 | 파일 |
|---|---|
| 사내 아티팩트로 볼 목록·별칭 | 회사 artifacts yml + `remediation.inhouse_groups` (`sbmig/remediations/inhouse.py` 의 `inhouse_keys`) |
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
