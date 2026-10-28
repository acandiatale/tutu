# 한국어 주석 규약 — `sbmig/` 코드 (인수인계용)

**목적.** 현업 요청(2026-10-28): 다음 담당자가 영어 docstring 을 읽지 않고도 파일을 열어
"이 모듈이 흐름 어디에 있고, 무엇을 하고, 왜 이렇게 하는지" 를 알 수 있어야 한다.
`tests/docs/test_korean_comments.py` 가 이 규약을 지킨다(모듈 docstring 과 공개 클래스·함수 docstring 에 한글이 있어야 함).

## 규칙

1. **영어 원문은 지우지 않는다. 한국어는 더하기만 한다.** 코드(로직·이름·순서·문자열·로그 줄·프롬프트 `.md`)는 손대지 않는다.
2. **모듈 docstring**: 한국어 블록이 **먼저**, 빈 줄, 그 다음 영어 원문. 한국어 블록(3~8줄)에 넣을 것:
   한 줄 요약 → 흐름상 위치(어느 단계에서 누가 부르는지) → 핵심 진입점(클래스·함수 이름) → 지켜야 할 결정·불변조건.
3. **공개 클래스·함수 docstring**(이름이 `_` 로 시작하지 않는 최상위 정의 전부, 그리고 길거나 까다로운 비공개 함수·메서드):
   한국어 1~3줄이 먼저, 빈 줄, 영어 원문. docstring 이 없던 정의에는 한국어 한 줄 docstring 을 새로 단다.
   자명한 한 줄짜리 비공개 헬퍼는 생략해도 된다.
4. **인라인 주석**은 "무엇" 이 아니라 **"왜"**: 회사 규칙, 불변조건, 실패했던 사례, 순서가 중요한 이유, 함정.
   기존 영어 인라인 주석은 그대로 두고, 중요한 것이면 그 위 줄에 한국어를 더한다.
5. **용어**는 아래 표를 따른다. 영어 식별자(함수·설정 키·단계 이름)는 그대로 쓰고 한국어로 풀이한다.
6. **줄 길이**: 코드와 같은 110칸 안에서 줄바꿈(한글은 2칸으로 센다 → 한글만 있는 줄은 50자 안팎).
7. **금지**: 코드 변경, 영어 삭제·재작성, 자동 포매팅(`ruff --fix`, black), 로그·f-string 번역, 프롬프트 `.md` 번역,
   기존 테스트 수정.
8. **검증**: 손댄 파일마다 `python -m py_compile`, 그 디렉토리의 테스트, `ruff check <파일>` (새 F·E 오류 없음),
   `python -m pytest tests/docs/test_korean_comments.py`.

## 용어표

| 식별자 / 영어 | 한국어 | 뜻 |
|---|---|---|
| intake | 접수 | 입력 프로젝트 검사(Maven 만, pom 없는 트리 거부) |
| analyze / profile | 분석 / 프로필 | 프로젝트 종류·Java 버전·Spring 세대·라이브러리 파악 |
| plan | 계획 | 실행할 단계(`transform.*`, `restructure.*`, `verify.build`) 목록 |
| transform.rewrite | 변환(OpenRewrite 1회) | Java 21 / jakarta / Boot 4 레시피만 |
| transform.compile | 변환 후 컴파일 | 그 시점에 보이는 오류 수집(빌드 게이트 아님) |
| restructure | 재구성 | 회사 레이아웃(`{name}-boot` / `-back` / `-front`, `base-parent` 등) |
| structure review | 구조 검토 | LLM 이 pom·모듈·인코딩·리소스를 회사 규칙에 비추어 검토 |
| verify.build | 검증 빌드 | `mvn clean package -Dmaven.test.skip=true`, 성공해야 끝 |
| fix loop / round | 수정 루프 / 라운드 | 빌드 실패 → 케이스별 스크립트 시도 → 재빌드 반복 |
| case / case_id | 케이스 / 케이스 ID | 오류 사례. L2 서명의 sha1 앞 10자리 |
| L1 / L2 signature | L1 / L2 서명 | 오류 줄에서 경로·숫자·이름을 지운 정규화 키(L2 가 더 거친 것) |
| cluster | 클러스터 | 같은 라운드에 같은 케이스로 묶인 오류 묶음 |
| try / attempt | 시도 | 스크립트 한 번 실행 + 재빌드 + 점수 |
| stored script / `fix_v<N>.py` | 저장된 스크립트 | 케이스 폴더에 버전별로 쌓이는 수정 스크립트 |
| reuse / augment / new | 재사용 / 보강 / 신규 | 사다리: 저장본 그대로 → LLM 이 고쳐서 → LLM 이 새로 |
| `no_script` + edits | 스크립트 없는 직접 수정 | LLM 이 스크립트 대신 편집 목록을 돌려주는 경우 |
| improved / partial / no_change / regressed / inconclusive | 개선 / 부분 / 변화 없음 / 악화 / 판정 불가 | 재빌드 점수 |
| rollback / revert | 되돌리기 | 도움이 안 된 시도의 커밋을 되돌림 |
| resolved / resolved_by | 해결 / 해결한 시도 | 빌드에서 사라진 오류와 그 원인 시도(v2.1f) |
| BLOCKED / blocked_policy | 차단 / 차단 정책 | 더 시도하지 않는 케이스와 그 기준 |
| KB (knowledge base) | 지식베이스(파일) | `kb-v2/` 아래 `errors.jsonl`, `cases.md`, `cases/<id>/` — DB 금지 |
| reindex / recount | 재색인 | `errors.jsonl` 로부터 케이스·실행 기록을 다시 셈 |
| hunk (as-is / to-be) | 변경 조각(수정 전 / 수정 후) | v2.1e 학습 기록 |
| Handover | 인수인계 섹션 | 보고서 맨 앞, 사람이 이어서 해야 할 일 |
| operator check | 운영자 확인 필요 | 메이저 업그레이드 등 사람이 봐야 하는 항목(v2.1d) |
| advisory | 권고 | EOL 라이브러리 안내(차단 없음, 안내만) |
| mandatory / optional artifacts | 필수 / 선택 사내 아티팩트 | 루트 pom 의 2개 필수, 쓰던 것만 올리는 8개 선택 |
| base-parent / boot-jar / comp-jar | 사내 부모 pom | 루트 / boot 모듈 / 그 외 모듈 |
| replay / fake provider | 재생 / 가짜 LLM | 오프라인 테스트용 LLM 응답 재생 |
| company gateway | 회사 LLM 게이트웨이 | `LLM_BASE_URL` + `LLM_API_KEY`, messages 만 |
| fleet | 일괄 실행 | 여러 프로젝트를 순차로 돌리는 명령 |
| business team / operator | 현업 / 운영자 | 요청한 팀 / 도구를 돌리는 사람 |

## 예시

`sbmig/kb/store.py` 가 규약대로 단 첫 파일이다. 모듈 docstring, 클래스·메서드 docstring, 인라인 "왜" 주석의 형태를 거기서 본다.
