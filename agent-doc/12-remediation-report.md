# 취약점 조치 보고서와 사내 라이브러리 안내

> 다루는 것: 조치 결과 보고서(`report.md`)의 구성과 절마다 할 일, 용어표, 사내 라이브러리 안내, 제안의 적용 범위, 한국어 표시 규칙, 고칠 자리.
> 언제 읽나: 보고서의 절마다 할 일을 물을 때, "이 라이브러리는 왜 자동으로 안 고쳤나"를 볼 때, 보고서 문장·구성·사내 판정을 바꾸기 전에.
> 함께 읽을 파일: [11](11-remediations.md) (흐름·상태·설정 키), [06](06-company-rules-restructure.md) (회사 부모·필수·선택 아티팩트), [13](13-framework-projects.md)

## 1. 보고서가 어디에 있고 누가 쓰나

- 사람용 `report.md`(한국어 결과 보고서)와 도구용 `report.json`(코드와 영어 문장 그대로). 이관에 이어 돈 조치면 `<이관 실행 폴더>/remediation/` 아래.
- 사람이 받는 이름은 `remediation-report.md` / `remediation-report.json`이다(`sbmig/remediations/api.py`의 `DOWNLOAD_NAMES`, 파일 주소에 `?download=1`).
  폴더 안 이름은 그대로다. 화면은 조치 칸 안의 「조치 결과 보고서 미리보기」에 이 파일 그대로를 그린다(「보기」 / 「원문」, 「크게 보기」).
- 제목 「# 취약점 조치 결과 — <이름>」: 웹에서 올렸으면 입력한 프로젝트명(`contract.entered_project`; 조치만 하는 실행은 없으면 실행 id 앞부분), 아니면 `source.zip`.
  한국어 이관 보고서가 이 보고서를 가리킬 때도 받는 이름으로 적고(`markdown.received_name`), 영어판은 `remediation/report.md` 경로를 쓴다.
- 읽는 사람: 프로젝트의 개발자·관리자. 첫 화면에서 무슨 일이 있었고 무엇을 할지 알아야 한다.
- 조치 그래프의 마지막 노드 `report`가 쓴다(`sbmig/remediations/report.py`의 `write_report`; 그 노드 도중 끝났으면 다음 진입이, 11의 2절).
  확인 대기로 멈춘 실행은 보고서 없이 `state.json`만 남는다.
- 같은 입력이면 같은 바이트. 비밀 값과 실행 폴더의 절대 경로는 지운다.
- 이관 보고서에는 `취약점 조치` 절 하나만 있다(상태 코드·사유 코드·집계·파일). 사내 라이브러리 몫이 있으면 "사내 라이브러리 (담당자 요청): 라이브러리 N개" 한 줄을 더 적는다.
- `sbmig.log`의 `chained remediation <id>: <상태> <이유> — <문장>` 한 줄이 끝난 까닭을 말한다(600자, 비밀 없음).

## 2. 절 순서와 읽는 사람이 할 일

빈 절은 쓰지 않는다(조치한 것이 없으면 "조치 완료" 절이 없다). 상태·사유 코드는 접힌 "처리 기록" 에만 있다.

| 절 | 담는 것 | 할 일 |
|---|---|---|
| 제목 아래 결과 블록 | 굵은 한 문장(✅ 조치를 마쳤습니다 / ⚠️ 일부만 조치되었습니다 / ❌ 조치하지 못했습니다 / ⏸ 확인이 필요합니다 / ⏹ 조치를 멈췄습니다 / ⏹ 조치가 중단되었습니다) + 숫자와 까닭 한두 문장 | 여기와 "해야 할 일"만 봐도 판단이 서야 한다 |
| (있을 때) 주의 문단 | 결과 ZIP이 빌드에 실패한 수정본이라는 사실, 재검사 없음, 빌드 로그, 이전 ZIP 위치. 자동 수정이 못 고쳤으면 「빌드 오류 자동 수정을 시도했지만 고치지 못했습니다(<사유>). 시도한 소스 수정은 결과에 넣지 않았습니다.」(후보는 그대로였으니 '되돌렸다'가 아님), 시도가 없었으면 「빌드 오류 자동 수정을 하지 못했습니다(<사유>).」, 시작 안 했으면 「…시작하지 않았습니다(<사유>).」 | 그 ZIP을 배포하지 않는다 |
| 요약 | 표 1 `구분 / 취약점 / 라이브러리`: 조치 전·조치 완료·(있을 때) 사내 라이브러리 담당자 요청·직접 확인 필요·조치 대상에서 제외·조치 후 남은 취약점. 표 2 `위험도 / 기준 (IVAS 점수) / 조치 전 / 조치 후`. 그 아래 "결과 파일:" 한 줄(없으면 아래 「결과 파일 — 없음」) | — |
| 해야 할 일 | 번호 목록. 항목마다 "**제목 (N건)** — 설명 ~해 주세요."이 상태에서 할 수 있는 일만 쓴다. 적용한 위험 변경이 있으면 "자동으로 적용한 변경 확인하기"(아래 "조치 완료의 「주의」 칸"). 소스를 고친 라운드가 있으면 맨 앞에 "자동으로 고친 소스 확인하기" | 위에서부터 한다 |
| 조치 완료 — N건 | 표 `위험도 / 라이브러리 / 버전 변경 / 수정한 곳 / [주의] / 해결된 취약점`, 그 아래 제안마다 버전 확인·수정 방식·적용 범위·참고. 건수와 「해결된 취약점」은 완전한 최종 검사에서 사라진 것만이다. 적용했는데 같은 취약점이 아직 보고되면 그 칸은 「-」이고 표 위에 설명 한 문장이 붙는다 | 결과 ZIP을 받는다 |
| (있을 때) 빌드 오류 수정 | 「버전을 올린 뒤 빌드가 실패해 소스를 고쳤습니다.」, 확정한 라운드마다 표 `파일`, 반복·LLM 호출 수, 모은 `fix.patch` 하나. 11의 2절 | 바뀐 소스를 검토한다 |
| 사내 라이브러리에서 발생한 취약점 — N건 | 안내 문구, 사내 아티팩트별 묶음(회사 부모 pom이면 제목에 표시, 사내 프레임워크면 제목에 '사내 프레임워크'와 안내 한 줄). 표 `위험도 / 취약한 라이브러리 / 버전 / 의존 경로 / 취약점`, 표 아래 "들어온 경로" | 그 아티팩트의 아키텍처 담당자에게 묶음째 요청한다 |
| 직접 확인이 필요한 취약점 — N건 | 나머지 잔여를 사유별로 묶은 것 — 설명은 묶음마다 한 번. 표 `위험도 / 라이브러리 / 버전 / [구분] / [사용 모듈] / 취약점 / [내용]`, 표 아래 "들어온 경로" | 묶음의 설명대로, 들어온 경로가 가리키는 pom에서 프로젝트 담당 팀이 조치한다 |
| 조치 대상에서 제외한 항목 — N건 | `remediation.ignore_actions` 코드로 제외한 검사 결과. 표 `위험도 / 취약점 / 라이브러리 / 버전 / IVAS 조치 상태` | — |
| 프로젝트 파일에서 찾은 취약점 — N건 (`remediation.file_components: report`일 때) | 앞 문장(IVAS가 프로젝트 안 파일(정적 파일 등)에서 찾았고, pom 버전만 바꾸는 조치라 바꾸지 않았고 조치 결과에도 넣지 않았다). 표 `위험도 / 컴포넌트 / 버전 / 패치된 버전 / 취약점 / 파일 위치`(파일은 셋까지 「외 N개」, 못 읽었으면 「파일 위치를 읽지 못했습니다」, 추정은 「(추정)」). 일부만 읽었으면 그 문장이 앞에, 못 읽었으면 표 대신 「파일에서 찾은 취약점 목록을 IVAS에서 읽지 못했습니다. …」. 10행이 넘으면 접힌다 | 그 파일의 담당 팀이 본다. 조치 결과와 관계없다 |
| 변경하지 않은 것 | 소스·구조는 그대로라는 보증(바꾼 것 없는 결과 ZIP은 「결과 ZIP은 원본 ZIP과 같은 파일입니다.」, 빌드 산출물을 빼 올린 바이트와 다르면 「… 원본 ZIP에서 빌드 산출물만 뺀 것입니다.」; 소스를 고쳤으면 「… 소스는 빌드 오류를 고치느라 <n>개 파일을 바꿨습니다.」), 원본 ZIP 위치, 빌드 산출물을 뺀 안내(「…검사와 결과 ZIP에서 제외했습니다.」; 조치 전 검사가 원본이었던 이전 버전의 실행은 뺀다) | — |
| 처리 기록 (접힘) | 작업 ID, 상태 코드·사유 코드, 집계 표, 1. 조치 전 빌드 → 2. 조치 전 검사 (IVAS) → 라운드별(제안 표, 적용 범위, 수정본 빌드, 자동 수정 한 줄(고침·다시 빌드하니 통과·고치지 못함 … 시도한 소스 수정은 결과에 넣지 않음·하지 못함(<사유>) · LLM 호출 N회·시작 안 함·버린 수정·빌드가 바꾼 파일 N개), 수정본 채택, 검증 참고·문제, 재검사) → 결과 파일 → 빌드 설정 점검. 그 아래 접힌 "남은 취약점 전체 목록" | 문의할 때 이 절의 코드를 전한다 |

**결과 파일 — 없음**(FAILED이고 결과 ZIP이 없을 때, `report._no_delivery_why`): 「— <까닭> 원본은 그대로입니다.」. 까닭은 되돌리지
못한 수정본이면 「수정본을 빌드 뒤 원래 상태로 되돌리지 못해 …」, 쓰지 못한 ZIP이면 「라운드 <r>의 결과 ZIP을 쓰지 못했습니다.」, 아니면
사유별 문장(`_NO_DELIVERY_KO`: 수정본 빌드 실패·조치 전 빌드 실패·버전 표기 외 변경·계획과 다른 버전·검사 파일 대조 실패·시작 전 멈춤·내부
오류), 모르는 코드는 「결과 ZIP을 만들지 않았습니다.」.

**프로젝트 파일 취약점**(11의 2절 "파일 컴포넌트"): 요약 표 아래에 「프로젝트 파일에서 찾은 취약점 N건은 위 개수에 넣지 않고 아래 「프로젝트 파일에서
찾은 취약점」에 따로 적었습니다.」, 요약과 조치 완료·남은 취약점의 개수는 라이브러리만이다. `report.json`에는 `report`일 때만 `file_components`,
`file_findings`, `file_findings_complete`, 수정 루프를 돈 라운드는 `rounds[].fix`. 이관 보고서의 조치 절은
「- 프로젝트 파일에서 찾은 취약점 N건은 위 개수에 넣지 않고 자세한 결과에 따로 적었습니다.」 한 줄을 더한다(en: `- File findings listed apart in the
remediation report, not counted above: N`).

**빌드가 실패한 프로젝트의 조치**(11의 1.1): 버전을 바꿨으면 결과 블록이 「⚠️ 라이브러리 버전을 바꾸고 다시 검사했습니다. 바꾼 버전은
빌드로 확인하지 못했습니다.」로 시작해 조치 전에도 빌드되지 않았다는 것, 같은 오류에서 멈췄고 새 오류는 없지만 그 뒤는 알 수 없다는 것을
적고, 할 일 맨 앞이 「빌드 오류 고치기」(이관 보고서 「변환 뒤 남은 빌드 오류」), 「바꾼 버전으로 한 번 빌드하기」다. 바꾼 버전이 없으면
보통 결과 문장 뒤에 `NOTHING_TO_CONFIRM_KO` 한 문장, 「빌드 오류 고치기」는 할 일 맨 뒤. 새 오류로 FAILED면 그 오류 3개까지. 사유가
`baseline_build_failed` 하나뿐인 INCOMPLETE(`build_unconfirmed`)는 「일부만 조치됨」 대신 「조치함 · 빌드 미확인」(화면의 조치 영역, 13의 8절), 「조치했지만 빌드로 확인하지 못했습니다」(이관 보고서)다.

위험도 표시: IVAS 점수가 `remediation.severity_red`/`remediation.severity_orange`/`remediation.severity_yellow`(기본 9.0 / 7.0 / 4.0)
이상이면 🔴 심각 / 🟠 높음 / 🟡 보통, 그보다 낮거나 점수가 없으면 "낮음·점수 없음"(표시 글자 없음). 라이브러리 표는 맨 앞
`위험도` 칸(그 줄의 최고 점수)으로 시작하고 위험한 줄이 위에 온다. 취약점 칸은 "🔴 CVE-… (9.8)"처럼 점수 높은 순으로 3개까지,
나머지는 "외 N건". 조치한 취약점의 점수는 조치 전 검사에서 찾는다. 판정은 보여 주는 소수 한 자리 값으로 한다.

읽는 요령: 묶음의 모든 행이 같은 값이면 칸을 빼고 표 위에 한 번 쓴다("아래 항목은 모두 간접 의존성입니다.").
의존 경로는 artifactId만 " → "로 잇고 취약한 라이브러리로 끝난다(버전은 버전 칸). classifier가 붙은 의존성은 좌표 뒤 괄호에
classifier를 쓴다. 숫자: 취약점은 건, 라이브러리·파일은 개, 라운드는 회, 1,000 이상은 쉼표, pom 줄은 "41행".

**들어온 경로** (`Residual.sources`, 모듈마다 `ModuleSource` 하나): 사내·직접 확인 표 아래 "들어온 경로:" 목록에 행마다 라이브러리가
모듈에 들어오는 길을 쓴다. 직접 선언 "`back/pom.xml` 123행에 직접 선언", 간접 "`a:b:1` > `c:d:2` 경로로 가져옴"(모듈이 직접 선언한
의존성부터 취약한 라이브러리 바로 앞까지), 관리 버전 "`g:parent:1.2.3`에서 버전 지정"(선언한 줄을 알면 앞에 "`app/pom.xml` 40행에 선언, "),
변종 "classifier `jakarta`", "형식 `test-jar`", 일반 jar와 변종을 함께 쓰면 "사용 형태 2가지(일반 jar, classifier `jakarta`)"(저장된 `jar/`는
보이지 않음). 모든 모듈이 같으면 행에 한 줄, 다르면 같은 경로의 모듈끼리 "`app-a`, `app-b` 모듈: …" 하위 항목. 받침에 따라 바뀌는 조사를
고정된 말("경로로 가져옴", "에서 버전 지정")로 받는다. 사내 절은 의존 경로 칸이 있으므로 모듈마다 경로가 다르거나 선언한
줄·형식·classifier가 있는 행만 쓴다. 모듈별 출처가 행의 구분과 다르면(간접 행인데 한 모듈은 직접 선언) "아래 항목은 모두 …"를 쓰지 않고
구분 칸에 "모듈마다 다름 (들어온 경로 참고)". `sources`가 없는 행(이전 상태 파일)은 빠지고, 쓸 행이 없으면 "들어온 경로:"도 없다.
**IVAS 표기**: IVAS가 버전을 Maven과 대소문자만 다르게 보고했으면(`2.2.0.release`/`2.2.0.RELEASE`) 같은 버전으로 보고
(Maven이 그렇게 비교한다), 버전 칸에 Maven 표기 뒤로 "(IVAS 표기: 2.2.0.release)"를 한 번 붙인다(`Residual.reported_version`).
건수와 점수도 대소문자를 가리지 않는다.

### 용어표 (보고서·화면·이관 보고서에서 같은 말을 쓴다)

| 코드의 말 | 보고서의 말 |
|---|---|
| fixed / manual review | 조치 완료 / 직접 확인 필요 |
| in-house / residual / ignored | 사내 라이브러리 (담당자 요청) / 조치하지 못한 항목 / 제외한 항목 |
| baseline build·scan / final scan | 조치 전 빌드·검사 / 조치 후 검사 (재검사) |
| candidate build / round committed / delivery ZIP | 수정본 빌드 / 수정본 채택 / 결과 ZIP |
| modules a change governs / also-modules | 적용 범위 / 적용되지 않을 수 있는 모듈 |
| via / version text / evidence / risk | 의존 경로 / 버전 표기 / 버전 확인 / 주의 |
| `direct_literal`/`direct_property`/`own_management` | 직접 의존성 (pom에 버전 표기) / 직접 의존성 (property로 버전 지정) / 프로젝트의 dependencyManagement |
| `parent_managed`/`bom_managed`/`vendored`/`unmapped` | 부모 pom에서 버전 지정 / BOM에서 버전 지정 / 프로젝트에 포함된 jar 파일 / 자동으로 수정할 위치를 정하지 못함 |
| `managed_override` / execution boundary | dependencyManagement에 버전 지정 추가 / 빌드 설정 점검 |
| `sources`/`reported_version` | 들어온 경로 / IVAS 표기 |
| root pom / a module that does not inherit from it | 프로젝트 최상위 pom.xml / 회사 부모 pom에서 설정을 물려받는 모듈 |

쓰지 않는 말: 다스리다, 닿다, 들어온 길, 버전 글자, 남긴 항목, 뺀 항목, 사람 검토, 고친 것, "~할 것" 제목, 문장 안 괄호 영어 코드.
pom·BOM·classifier·dependencyManagement·property·scope·jar는 영어 그대로 쓴다. 회사 문구 "해당 아티팩트는 예외처리가
가능합니다. 담당자에게 연락 부탁드립니다."는 그대로 둔다.

### 조치 완료의 「주의」 칸과 "자동으로 적용한 변경 확인하기"

조치는 묻지 않고 적용하므로(11의 3절) 위험한 변경은 보고서가 따로 표시한다.

- 조치 완료 표: 적용한 제안 가운데 하나라도 위험 부류(`risks`)가 있으면 「해결된 취약점」 앞에 「주의」 칸이 생긴다. 값은
  (메이저 버전 변경 / 부모 pom 변경 / BOM 변경 / dependencyManagement 항목 추가), 해당 없는 행은 `-`,
  여러 제안을 묶은 행은 모두 적는다. 위험 부류가 하나도 없으면 칸 자체가 없다. 처리 기록의 라운드별 제안 표는 위험 부류가 있는 계획이면 맨 끝에 「주의」 칸을 둔다.
- 해야 할 일: 적용한(`decision: auto`) 위험 부류 제안이 N건이면 "**자동으로 적용한 변경 확인하기 (N건)**"가 붙는다.
  부류마다 건수를 적고(두 부류인 제안은 양쪽에 세고 N에는 한 번), 「주의」 칸을 보고 애플리케이션 동작을 점검하라고 한다. 자리: 결과 ZIP
  단계와 실패·중단·확인 대기 단계 뒤, 사내 라이브러리·직접 확인 단계 앞.
- 볼 것: 메이저는 API 변화, 부모 pom·BOM은 다른 관리 버전도 바뀜, dependencyManagement 항목은 이 버전을 고정함(제안 아래 "참고").

### 모듈 pom 제안 읽는 법 (조치 완료)

회사 구조의 모듈은 최상위 pom.xml이 아니라 회사 부모 pom에서 설정을 물려받으므로, 최상위 pom.xml의 버전 지정이 적용되지 않는
모듈에는 **그 모듈의 pom.xml에** dependencyManagement 항목을 넣는 제안을 낸다(`remediation.module_overrides`, 기본 켬).
제안은 (pom, g:a·형식·classifier)마다 하나다.

- 표는 라이브러리마다 한 행이고 "수정한 곳" 칸에 고친 pom을 모두 적는다(4개가 넘으면 "외 N곳").
- 표 아래 라이브러리마다 "수정한 pom:" 목록이 pom마다 한 줄이다: "`app-back/pom.xml`에 버전 지정 추가 (제안 `p-1-2`) · 항목
  `g:a:2.1` classifier `jakarta` · `app-back` 모듈: … 경로로 가져옴". 최상위 pom이면 "최상위 pom.xml에 버전 지정 추가".
  같은 pom에 형식·classifier가 다른 항목이 있으면 줄이 따로다. 제안이 하나뿐이면 목록 없이 한 줄이다.
- 넣은 버전 지정 항목이 실제 버전을 바꾸지 못하면(직접 적힌 버전이 이 pom이든 부모 pom이든
  dependencyManagement보다 우선) 그 항목만 빼고 수정본을 한 번 더 빌드한다(다른 변경은 그대로). 그 제안은 "직접 확인"의 "버전 지정을 넣어도 바뀌지 않는 버전"
  묶음에 들어온 경로와 함께 나온다(그 선언의 버전을 직접 올려야 한다). `decision`은 `ineffective`, 처리 기록에는 "효과가 없어 다시 뺌".
  버전 표기를 바꾸는 제안이 실제 버전을 바꾸지 못한 것은 `resolution_mismatch` 다(4절: 그 라운드의 수정본을 버린다).

## 3. 사내 라이브러리 안내 (이유 코드 `inhouse_artifact`)

회사 규칙: 취약한 라이브러리가 **사내 아티팩트에서 왔으면 프로젝트에서 고치지 않는다**(그 pom은 담당자만 고치고, 프로젝트의 덮어쓰기는
담당자 쪽 수정과 어긋난다). 잔여로 두고 보고서가 따로 묶어 문구를 붙인다.

사내 아티팩트 = 회사 artifacts yml의 목록 가운데 `remediation.inhouse_groups`가 고른 것.

| 그룹 | artifacts yml의 자리 | "거기서 왔다"의 뜻 |
|---|---|---|
| `mandatory` | `artifacts.mandatory` | 의존성 트리에서 그 아티팩트(별칭 포함)를 거쳐 가져왔거나, 취약한 것이 그 아티팩트 자체 |
| `optional` | `artifacts.optional` | 위와 같다 |
| `parents` | `artifacts.parent`, `artifacts.boot_parent`, `artifacts.comp_parent` | 그 부모를 상속하는 모듈에서 (1) 부모 쪽이 버전을 관리하는 의존성(출처가 분명할 때만), (2) 프로젝트 pom이 선언하지 않은 의존성 = 부모가 물려준 것과 그것이 끌고 온 것 |
| `framework` | `framework.artifacts`의 패턴(artifactId, groupId:artifactId, `*`) | 취약한 것이 프레임워크 아티팩트 자체이거나 의존 경로에 프레임워크가 있음 |

판정과 계획:

- 인벤토리가 모듈마다 판정한다: `inhouse`(가져온 사내 아티팩트의 g:a), `inhouse_modules`(그런 모듈).
  경로 판정은 `sbmig/remediations/inhouse.py`의 `inhouse_on_path`, 부모 파생은 `sbmig/remediations/inventory.py`의 `_parent_inhouse`.
- 계획(`sbmig/remediations/planner.py`의 `_inhouse_residual`): 항목이 사내 아티팩트 자체이거나, 간접·부모·BOM 관리 항목이
  모든 모듈에서 사내 아티팩트를 거쳐 왔으면 잔여 `inhouse_artifact`. **사내 라이브러리를 거쳐 들어온 모듈만 사내 몫이다**: 일부 모듈만
  그러면 그 모듈들이 `inhouse_artifact` 잔여가 되고, 나머지 모듈은 사내 모듈이 없는 것처럼 계획한다(제안, 버전 지정 추가, 또는 그 모듈들만의
  잔여). 그래서 `Residual.inhouse`는 `inhouse_artifact` 잔여에만 있고, 같은 취약점이 두 절에 나올 수 있다. 보고서는 두 절에 행을 쓰고
  결과 블록에 "그중 N건은 모듈에 따라 … 두 절에 모두 나옵니다."를 더하며, 남은 건수에서는 한 번만 센다.
- 예외 하나: 회사 부모 몫인 관리 버전인데 서버 정책이 그 부모를 pin 했으면(`remediation.policy.pins`) 부모 버전을 올리는 제안(아키텍처
  담당이 고친 부모를 내놓았을 때, 위험 부류 `parent`).
- 직접 선언한 외부 라이브러리와 그것이 가져온 것은 사내 몫이 아니다(조치하거나 직접 확인).
- `framework` 그룹: 그 자체와 그것이 가져오는 라이브러리는 사내 몫이고, 버전은 어떤
  경우에도 제안하지 않는다(pin 예외도 없다). 패턴이므로 목록에 g:a가 들어가지 않는다(`InhouseKeys`).
  보고서는 그 묶음 제목에 '사내 프레임워크'를 붙이고 "사내 프레임워크의 버전은 바꾸지 않습니다."로 시작하는 안내 한 줄을 더한다.
  `remediation/report.json`의 사내 묶음 가운데 프레임워크 것만 `"group": "framework"`를 가진다. 이관 쪽 이야기는 [13](13-framework-projects.md).
- 사내 아티팩트 목록(과 `framework` 패턴, `inhouse_framework`)이 바뀌면 정책 해시가 바뀌어 다시 계획한다.
- 남은 것이 사내 몫뿐이어도 `INCOMPLETE`, 이유 `inhouse_artifact`, 건수 `counts.residual_inhouse`.
- `counts`(`graph.py`의 `fill_counts`): `baseline_findings`/`final_findings`는 검사 결과 행(나뉜 라이브러리도 한 번), `fixed`는 완료된
  조치 후 검사에서 사라진 조치 전 취약점(종류·ID·식별자, 대소문자 무시; 요약·「해결된 취약점」도 같은 기준), `residual`은 잔여 행(나뉜
  라이브러리는 두 행), `residual_inhouse`는 그중 `inhouse_artifact` 행, `proposals*`는 제안 단위.

| 키 | 뜻 |
|---|---|
| `remediation.inhouse_groups` | 사내 아티팩트로 볼 그룹(기본 mandatory, optional, parents, framework). 비우면 이 안내를 쓰지 않는다 |
| `remediation.inhouse_message` | 보고서의 그 절 첫머리에 붙는 문구(기본은 2절 용어표 아래의 회사 문구) |

## 4. 제안의 적용 범위

같은 라이브러리의 같은 버전을 모듈마다 다른 경로로 가져올 수 있다(자기 pom의 버전 표기, 간접 의존성, 부모 pom의 버전 지정). 그래서 제안은 **자기 버전 표기가 적용되는 모듈(적용 범위)**만 책임진다(코드: `modules`).

- 인벤토리 항목의 `modules`는 그 버전을 사용하는 모든 모듈, `also_modules`는 그 버전 표기가 적용된다고 볼 근거가 없는 모듈.
- 제안의 `modules` = 적용 범위, `also_modules` = 적용되지 않을 수 있는 모듈. 둘 다 제안 해시에 들어가지 않는다.
- 검증(`sbmig/remediations/apply.py`의 `verify_resolution`)은 적용 범위의 모듈과 그 모듈이 만든 산출물에서만 전 → 후를 요구한다.
  나머지 모듈이 옛 버전에 남으면 실패가 아니라 "검증 참고" 줄이다. 수정본은 채택되고, 재검사에 남은 것을 다음 라운드가 그
  모듈만으로 다시 계획한다(dependencyManagement 버전 지정 제안 또는 잔여).
- 적용 범위의 모듈이 계획한 버전이 되지 않았을 때만 `resolution_mismatch`(FAILED, 수정본을 버림)다.
- 보고서: 제안 아래 "적용 범위" 줄, 처리 기록에 "적용 범위 (p-…)"·"적용되지 않을 수 있는 모듈 (p-…)" 줄.

## 5. "직접 확인이 필요한 취약점" 의 묶음

잔여를 (이유 코드, 영어 설명 문장의 패턴)으로 묶고, 묶음마다 제목과 한 문장 설명이 있다. 자주 보일 묶음:

| 묶음 | 뜻 | 누가 무엇을 |
|---|---|---|
| 모듈 pom에 직접 지정해야 하는 버전 (`not_inheriting_root`) | 최상위 pom.xml의 버전 지정이 적용되지 않는 모듈인데 `remediation.module_overrides`가 꺼져 모듈 pom 제안도 내지 않았다(기본은 켬). 설명에 Maven 상속 용어를 쓰지 않는다 | 프로젝트 담당: 들어온 경로를 보고 그 모듈 pom.xml의 dependencyManagement에 버전을 추가하거나 가져오는 의존성의 버전을 올린다 |
| classifier가 붙은 의존성 (`variant`) | 설명 첫 문장이 classifier를 풀어 쓴다("같은 버전의 jar를 용도별로 구분하는 이름입니다(예: jakarta)"). 변종도 같은 classifier·형식의 항목으로 조치하므로 못 한 것만 남는다 | 프로젝트 담당 |
| 패치 버전 정보 없음 / 저장소에 올릴 수 있는 버전 없음 / 메이저 버전 변경만 가능 | 올릴 버전을 근거 있게 정하지 못했다 | 보안·프로젝트 담당이 버전을 정하거나 서버 정책에 버전 지정을 추가한다 |
| 다른 메이저 버전에만 있는 패치 (`major_not_hinted`) / 날짜로 번호를 매긴 옛 버전 (`dated_current`) | 메이저는 IVAS 패치 힌트가 그 메이저일 때만 올린다. 첫 숫자가 9999보다 큰 버전(`20030203.000550`)은 날짜라 후보도 아니다(목록에 "(날짜로 번호를 매긴 옛 빌드)") | 담당 팀이 버전을 정한다 |
| 이미 패치 버전 이상인데 보고됨 (`meets_hint`) | 지금 버전이 모든 힌트를 만족해 올리지 않았다(pin은 적용) | IVAS 담당에 확인을 요청한다 |
| 여러 라이브러리가 함께 쓰는 버전 property (`shared_property`) | 그 property를 쓰는 라이브러리 가운데 하나라도 저장소에 그 버전이 없어 바꾸지 않았다 | 라이브러리마다 버전을 따로 두거나 모두에 있는 버전을 정한다 |
| 꺼진 프로필에만 있는 선언 (`profile_only`) / 같은 의존성을 두 번 선언 (`declared_twice`) | 기본으로 꺼진 프로필에만 선언이 있거나, 한 `<dependencies>`에 같은 g:a가 두 번 있다(출처 `unmapped`) | 프로필을 확인하거나 중복을 지운 뒤 버전을 올린다 |
| 모듈 pom에서 버전을 직접 지정 | 직접 지정한 버전이 dependencyManagement보다 우선 | 프로젝트 담당이 그 버전을 고친다 |
| 빌드 플러그인 / 프로젝트에 포함된 jar 파일 | 자동 수정 대상이 아니다 | 프로젝트 담당 |

전체 묶음 표는 `sbmig/remediations/i18n.py`의 `GROUPS` 다.

## 6. 한국어로 보여 주는 규칙

- **저장은 그대로**: `state.json`, `plan.json`, `report.json`, HTTP 응답의 문장은 영어이고 코드(출처·이유·종류)는 문자열 그대로가 계약이다.
- **보여 줄 때만 한국어**: `sbmig/remediations/i18n.py`가 코드 → 한국어 라벨 표와 영어 문장 → 한국어 문장 규칙(`RULES`, `ko_text`)을
  가진다. g:a, 버전, 모듈 이름, 파일 경로, 제안 ID는 글자 그대로 남긴다.
- 모르는 문장은 지어내지 않는다: `ko_text`(화면의 `*_ko`)는 그대로, 보고서용 `ko_display`는 "원문: `…`"으로 감싼다. Maven 빌드 요약과
  내부 오류의 세부는 옮기지 않는다.
- 화면: 상태 응답은 `message_ko`·`status_ko`를 더 싣고 화면은 `message_ko`를 먼저 보여 준다.
- **새 영어 문장을 코드에 더하면** `RULES`에 규칙을 더하고 `tests/remediations/test_i18n.py`에 표본 문장을 더한다. 그 시험은 규칙마다 표본이
  있는지, 옮긴 글에 한글이 있고 식별자가 남았는지 본다. 잔여 설명이면 `GROUPS`에 묶음도 더한다. 한국어는 2절의 용어표대로,
  합니다체로 한 문장에 사실 하나만 쓴다. 조사는 앞말에 붙인다(pom을, ZIP은, jar가). 값(좌표·버전·모듈·경로) 뒤에는 받침에 따라
  바뀌는 조사를 붙이지 않고 명사로 받거나("2.25.4 버전이", "`app-boot` 모듈에") 의·에·에서·입니다처럼 바뀌지 않는 말을 붙인다. `tests/remediations/test_report.py`의 `BANNED_WORDS`가 쓰지 않는 말을 막는다.

## 7. 고칠 자리

| 요청 | 파일 |
|---|---|
| 사내 아티팩트로 볼 목록·별칭 | 회사 artifacts yml + `remediation.inhouse_groups` (`sbmig/remediations/inhouse.py`의 `inhouse_keys`) |
| 프레임워크 패턴 판정 | `sbmig/remediations/inhouse.py` (`InhouseKeys`, `framework_patterns`), 정책 해시는 `sbmig/remediations/planner.py` (`policy_hash`) |
| "사내에서 왔다"의 판정(경로, 부모 파생) | `sbmig/remediations/inhouse.py`, `sbmig/remediations/inventory.py` (`_parent_inhouse`, `_fill_inhouse`) |
| 사내 몫을 잔여로 둘지 제안할지, pin 예외 | `sbmig/remediations/planner.py` (`_inhouse_residual`, `_change_for`) |
| 안내 문구 | `remediation.inhouse_message` |
| 위험도 표시의 기준 점수·표시 글자 | `remediation.severity_red` 등 세 키, `sbmig/remediations/i18n.py` (`severity_of`, `SEVERITY_LEVELS`) |
| 보고서의 절·표·줄임 규칙 | `sbmig/remediations/report.py` (`build_report_md`) |
| 한국어 라벨·문장·묶음 | `sbmig/remediations/i18n.py` |
| 이관 보고서의 조치 절 | `sbmig/report/markdown.py` (`remediation_lines`, 한국어 안내서 `sbmig/report/guide_ko.py`의 특수 절), 문구는 `sbmig/report/i18n.py` |
| 화면의 문구 | `sbmig/web/static/index.html` |

## 확인하는 법

```bash
grep -n "inhouse" config/default.yml sbmig/core/config.py
grep -n "^def " sbmig/remediations/inhouse.py
grep -n "def _inhouse_residual" -A16 sbmig/remediations/planner.py
grep -n "^GROUPS" -A6 sbmig/remediations/i18n.py
.venv/bin/python -m pytest tests/remediations/test_inhouse.py tests/remediations/test_i18n.py tests/remediations/test_report.py -q
```
