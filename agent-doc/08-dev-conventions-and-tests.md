# 개발 규약과 테스트

> 다루는 것: 코드를 안전하게 바꾸는 법 — 7개 불변조건, 사용자의 상시 규칙, 한국어 주석 규약, 테스트 구조와 명령, 픽스처 추가, 문서 갱신, git 습관, 변경 전·후 체크리스트, 저장소에 기록된 함정.
> 언제 읽나: sbmig 코드·설정·노트를 고치기 전에(LLM 과 함께 고칠 때는 이 파일을 먼저 LLM 에 준다).
> 함께 읽을 파일: [07-config-and-knowledge.md](07-config-and-knowledge.md), [04-cases-scripts-kb.md](04-cases-scripts-kb.md), [02-flow-and-nodes.md](02-flow-and-nodes.md), [10-open-items-and-history.md](10-open-items-and-history.md)

## 1. 7개 불변조건 (깨면 안 되는 것)

| # | 불변조건 | 실제로 금지하는 것 |
|---|---|---|
| 1 | **빌드 게이트**: 실행은 마지막 `mvn clean package` 시도와 미해결 오류를 적은 보고서 없이 끝나지 않는다. 수정 루프는 모든 시도를 재빌드로 점수 매기고 도움이 안 된 것은 되돌린다 | LLM 이 "다 고쳤다" 고 말했다고 성공 처리하기, 최종 빌드를 건너뛰는 분기, 점수 없이 시도를 남겨 두기 |
| 2 | **회사 특수사항은 config / knowledge YAML 에**, Python 코드에 두지 않는다 | `if group_id == "com.company..."`, 사내 부모 이름·모듈 이름·Nexus 주소를 코드에 쓰기 |
| 3 | **KB 는 파일 기반**(DB 금지) | SQLite / 외부 DB 도입. 나중에 DB 를 쓰더라도 파일 KB 에서 파생된 복제본일 뿐 |
| 4 | **플러그인 경계 고정**: `LLMRequest` / `LLMResponse`(1단), `FixerLike.propose_script`(2단), `sbmig.api.migrate(...)` | 이 시그니처·필드의 뜻을 바꾸기(추가는 기본값과 함께만) |
| 5 | **레시피 id 의 단일 출처** = `knowledge/recipes/catalog.yml`. `sbmig recipes check` 와 스냅샷 테스트가 지킨다. 레시피 BOM 은 3.37.0 고정 | 코드나 yml 에만 있는 레시피 id, BOM 버전 올리기(검증 없이) |
| 6 | **테스트·데모 실행은 `out/` 아래만** 쓴다. 레포의 `kb-v2/` 에 쓰지 않는다(로컬 e2e KB 는 `out/e2e/kb-*`) | 테스트가 기본 `kb.dir` 을 그대로 쓰기, 픽스처 `sbmig.e2e.yml` 에 `kb.dir` 을 빠뜨리기 |
| 7 | **수정 스크립트는 `sbmig.script` 로만** 실행: 정적 검사, `python -I` 자식 프로세스, 프로젝트 폴더로 제한된 헬퍼 API `p`, 타임아웃 + 프로세스 트리 종료 | 스크립트를 같은 프로세스에서 `exec`, 헬퍼 밖의 파일 접근 허용. (악의적 작성자를 막는 샌드박스가 아니라 실수를 막는 장치다) |

### "회사 특수사항은 YAML 에" — 맞는 예와 틀린 예

| 틀린 예 | 맞는 예 |
|---|---|
| 코드에 `PARENT = "com.company.platform:base-parent"` | `artifacts.parent` 를 회사 yml 에, 코드는 `cfg.artifacts.parent` 만 읽음 |
| 코드에 `if name.endswith("-boot")` 로 boot 모듈 판정 | `layout.boot_module_pattern` + pom 내용(Boot 앱인지)으로 확인 |
| 코드 안의 바이너리 확장자 목록 `["dmdb", …]` | 파일 내용으로 바이너리 판정 + 보충은 `build.non_filtered_extensions` |
| 프롬프트에 `spring-security 7.0.x 를 써라` | 버전은 부모 체인·Boot BOM 에서 읽은 사실로 프롬프트에 들어감 |
| 코드에 jar 이름 → 좌표 사전 | `knowledge/libraries/jar_names.yml` + 회사는 `kb-v2/jar_names.yml` |

## 2. 사용자의 상시 규칙

- **버전·좌표를 프롬프트나 코드에 박지 않는다.** 버전은 사실(부모 체인, Boot BOM, as-is 유효 pom, 저장소 메타데이터)에서 읽는다.
- **버전을 추측하지 않는다.** 모르면 `null`(관리되는 버전) 또는 사람에게 넘긴다. 프롬프트 규칙도 "모르는 버전은 비워 두라" 고 말한다.
- **좌표를 지어내지 않는다.** 동봉 jar 처럼 좌표를 모르면 자리표시자 좌표 + `system` 스코프 + 인수인계(Nexus 게시 요청)로 처리한다.
- **폴더 이름 규칙을 Python 에 두지 않는다.** 판정은 내용 기반이다(pom 의 g:a 가 겹치는 복사본, jar 안의 pom.properties·패키지,
  BC4J XML 같은 증거). `bin/`, `WEB-INF/lib` 같은 이름으로 가르지 않는다.
- **차단(block) 정책은 없다.** EOL 라이브러리도 빌드는 시도한다. 권고는 안내 + 인수인계뿐이다.
- KB 는 파일, 테스트는 `out/` 만. 운영자에게 보이는 변경(CLI, 설정 키, 보고서 섹션, 로그 줄)은 `docs/PLAN.md` / `docs/INTERFACES.md` 에 적는다.
- 테스트(프로젝트의 단위 테스트)는 범위 밖: 모든 빌드가 `-Dmaven.test.skip=true`.

## 3. 한국어 주석 규약 (`sbmig/` 만)

규약 문서: `docs/v2/CODE-COMMENTS-KO.md` (용어표 포함, 본보기는 `sbmig/kb/store.py`).

- **모듈 docstring**: 한국어 블록이 먼저(한 줄 요약 → 흐름상 위치 → 핵심 진입점 → 지켜야 할 결정), 빈 줄, 그 다음 영어 원문.
- **공개 최상위 클래스·함수**(이름이 `_` 로 시작하지 않는 것): docstring 에 한국어 1~3줄이 먼저, 영어는 지우지 않고 뒤에.
- **인라인 주석**은 "무엇" 이 아니라 "왜"(회사 규칙, 실패했던 사례, 순서가 중요한 이유).
- 로그 문자열·f-string·프롬프트 `.md` 는 번역하지 않는다. **프롬프트 `.md`, YAML, 테스트, `coding/` 은 영어로 둔다.**
- `tests/docs/test_korean_comments.py` 가 강제한다: 새 모듈이나 새 공개 함수에 한국어가 없으면 실패한다. 이것이 새 파일을 만들 때 가장 흔한 실패다.

## 4. 테스트 구조

| 위치 | 내용 |
|---|---|
| `tests/<패키지>/` | `sbmig/<패키지>/` 를 그대로 따라간다(`tests/loop`, `tests/fix`, `tests/restructure`, `tests/analyze`, `tests/kb`, `tests/web` …) |
| `tests/conftest.py` | 공용 픽스처: `cfg`(= `config/artifacts.local-e2e.yml` + 가짜 LLM), `make_ws`, `fixture_project`, 리액터 모듈 레지스트리 초기화, LLM 쿨다운 잠재우기 |
| `tests/docs/` | 문서-코드 동기화: `test_korean_comments.py`, `test_agent_doc.py`(이 팩) |
| `tests/fixtures/test_fixture_projects.py` | 픽스처 목록(`HAPPY`, `SCENARIOS`)과 메타데이터 스키마, 그리고 `BUILD_MATRIX`(픽스처, 빌드할 하위 폴더, as-is JDK, 필요 조건) — 통합 테스트가 각 픽스처를 as-is 로 빌드해 본다. `BUILD_MATRIX` 는 모든 픽스처 이름을 덮어야 한다(테스트가 검사) |
| `tests/fixtures/test_e2e_script.py` | e2e 스크립트(`scripts/e2e.py`)의 판정 로직 단위 테스트 |
| `fixtures/projects/<이름>/` | 작지만 진짜인 Maven(또는 Ant) 프로젝트 + `expected.json` + `sbmig.e2e.yml` + `replay/llm-NNN.json` + README |

마커: `integration`(mvn / java / 네트워크 필요), `llm`(실제 LLM 필요). 빠른 스위트는 `-m "not integration"` 이다.
지금 빠른 스위트는 약 2,900개 테스트(통합 약 50개 제외), 시간은 약 6~9분이다.

## 5. 명령

```bash
# 설정 (한 번)
uv venv && source .venv/bin/activate && uv pip install -e ".[dev]"      # 또는 pip install -e ".[dev]"

# 빠른 스위트 (종료 코드가 필요하면 파일로 받는다 — tail 로 파이프하면 종료 코드를 잃는다)
.venv/bin/python -m pytest -m "not integration" -p no:cacheprovider > out/pytest.txt 2>&1; echo "exit=$?"
# 폴더 하나 / 파일 하나
.venv/bin/python -m pytest tests/loop -p no:cacheprovider
.venv/bin/python -m pytest tests/docs/test_korean_comments.py -p no:cacheprovider
# 통합 테스트 (JDK 21 + Maven + Maven Central 네트워크)
.venv/bin/python -m pytest -m integration -p no:cacheprovider
# 실제 Maven e2e: 모든 픽스처를 v2 흐름으로 (재생 LLM)
JAVA21_HOME=/path/to/jdk-21 scripts/e2e.sh                 # 결과: out/e2e/summary.md
scripts/e2e.sh --only boot2,msa                           # 일부만
scripts/e2e.sh --expect-only                              # 기대값만 출력
# 린트: 게이트가 아니다 — 손댄 파일만
ruff check sbmig/loop/fixloop.py
```

- `pyproject.toml` 의 `addopts` 가 이미 `-q` 라서 `-q` 를 더 주면 요약 줄이 숨는다.
- **`ruff check sbmig tests` 에는 원래 있던 지적(대부분 `UP045`)이 있다.** 기능 작업 중에 저장소 전체 `ruff --fix` 를 절대 돌리지 않는다.
- e2e 는 시작할 때 `recipes check --jars` 와 `advisories check` 를 돌리고, `fixtures/libs/install.sh` 로 모의 사내 라이브러리를 로컬
  저장소에 설치한다(`--skip-libs` 로 생략). 재사용 시나리오는 순서가 있다: pure-java 가 java6-legacy 보다 먼저(`expected.json` 의 `kb.after`).
- KB 데모 `scripts/demo-kb/run.sh`(두 번째 실행이 첫 실행의 스크립트를 재사용), 플릿 데모 `scripts/fleet-demo.sh`.

## 6. 새 시나리오에는 픽스처를 더한다

1. `fixtures/projects/<이름>/` 에 as-is 프로젝트를 만든다(가장 작게, 공개 라이브러리만; 사내 라이브러리가 필요하면 `fixtures/libs/` 의 모의 것).
2. README(as-is 빌드 명령과 v2 결과), `expected.json`(`sbmig analyze` 가 내야 할 프로필 + `expect` 블록: `success`, `exit_code`,
   `max_llm_calls`, `blocked`, `unresolved_max`, `journal_events` …).
3. `sbmig.e2e.yml`: `config/artifacts.local-e2e.yml` 뒤에 겹쳐지는 픽스처 설정. 반드시 `llm.provider: fake`,
   `llm.base_url: "replay:fixtures/projects/<이름>/replay"`, 그리고 **자기 KB** `kb.dir: out/e2e/kb-<이름>`.
4. `replay/llm-NNN.json`: 재생할 LLM 답. 필드 `replay_role`(`fix` 또는 `structure_review`), `match`(`"case: <case_id>"` —
   case_id 는 L2 서명 sha1 의 앞 10자리; 맞는 것 중 안 쓴 첫 항목, 마지막 것은 반복, 없으면 "수정 없음"), `response`, `note`.
   구조 검토는 거의 모든 실행에서 LLM 호출 1회다.
5. `tests/fixtures/test_fixture_projects.py` 의 `SCENARIOS` 와 `BUILD_MATRIX` 에 이름을 더하고, `fixtures/projects/README.md` 의 표에 한 줄.
6. `scripts/e2e.sh --only <이름>` 으로 실제 Maven 에서 확인.

## 7. 바꿀 때 같이 고치는 문서

| 무엇을 바꿨나 | 고칠 문서 |
|---|---|
| 무엇이든 한 라운드 | `docs/PLAN.md` — 맨 아래에 그 라운드의 Status 표(또는 행) |
| 계약(모델 필드, 설정 키, CLI, 보고서 섹션, 로그 줄, 저널 이벤트) | `docs/INTERFACES.md` — 새 절 |
| 결정·현황·후속 과제 | `CLAUDE.md` |
| 운영자 텍스트 | `docs/RUNBOOK.md`, 배포는 `deploy/README.md` |
| 진행 내역 체크리스트 | `docs/PROGRESS.md` (큰 작업을 마칠 때) |

## 8. git 습관

- 작업 브랜치는 `flow-v2`. 커밋은 작게, 첫 줄은 무엇을 왜 바꿨는지 설명하는 문장(이 저장소는 첫 줄에 라운드 이름을 붙인다).
- 비밀값·실제 사내 좌표·회사 소스·`.env`·`deploy/k8s/30-secret.yaml` 은 커밋하지 않는다. `out/` 과 `kb-v2/` 는 git-ignore 다.
- 병렬 작업(사람 또는 서브에이전트)은 서로 겹치지 않는 디렉토리를 맡고, 통합·테스트·커밋은 한 곳에서 한다.

## 9. 저장소에 기록된 함정

- **`TrySource` 리터럴** (`sbmig/core/models.py`): 수정 시도의 출처는 pydantic `Literal`(`kb-script`, `llm-new`, `llm-augment`,
  `llm-edit`, `review`, `sbmig-version`, `sbmig-resources`, `sbmig-jakarta`, `sbmig-bundled`)이다. 새 LLM 없는 사전 시도를 만들면
  여기에 값을 더해야 한다. 빠뜨리면 시도 기록이 `ValidationError` 로 실패해 그 시도가 오류 목록·KB 에 남지 않고, 픽스처의 케이스별
  `sources` 검사가 깨진다(e2e 로 발견된 일).
- **한국어 주석 테스트**: 새 모듈은 docstring 첫 줄이 한국어여야 한다. 영어만 쓰면 `tests/docs/test_korean_comments.py` 가 실패한다.
- **노트 줄 수와 색인**: `knowledge/notes/*.md` 는 160줄 이하, `knowledge/notes/index.yml` 의 항목과 노트 앞머리(`title`, `keywords` …)가
  같아야 한다(`tests/fix/test_fix_notes.py`). 노트만 고치고 색인을 안 고치면 실패한다.
- **`script.max_declines_per_case`**(기본 1): LLM 이 편집도 스크립트도 없이 "프로젝트 밖의 문제" 라고 거절한 답의 허용 횟수다. 넘으면 케이스는
  BLOCKED `needs_owner`, 같은 파일의 형제 케이스는 추가 호출 없이 같이 차단된다. 이 값은 LLM 총 요청 상한(`script.max_llm_tries_per_case`,
  20)이나 보강 상한(`script.max_augments_per_case`, 15)과 별개다. 직접 편집으로 고치는 케이스는 `script.max_edit_tries_per_case`(4)가 따로 센다.
  sbmig 가 아는 사실과 모순되는 거절(저장소에 jakarta 빌드가 있음, 동봉 jar 가 그 패키지를 제공함, 대체 노트 `replacement: true` 가 맞음)은
  세지 않고 다시 묻는다.
  단, 동봉 jar 가 그 패키지를 제공하는데 좌표 부족을 이유로 거절하면 그 거절은 받아들이지 않는다.
- **케이스 id = L2 서명의 해시**: `sbmig/build/signatures.py` / `sbmig/triage/cluster.py` 의 규칙을 바꾸면 저장된 케이스가 고아가 된다
  (`sbmig kb reindex` 는 L2 를 다시 만들지 않는다). 서명 규칙을 바꿀 때는 픽스처 `replay` 의 `match` 와 `expected.json` 의 케이스 id 도 바뀐다.
- **`script.python`** 이 가리키는 파이썬에는 pydantic 과 lxml 이 있어야 한다(없으면 `ModuleNotFoundError`).
- **설정 목록은 합쳐지지 않고 교체된다**(deep_merge): 회사 yml 의 목록 하나가 기본 목록 전체를 바꾼다.

## 10. 체크리스트

**변경 전**
1. `CLAUDE.md` 의 불변조건과 상시 규칙에 닿는지 본다(회사 값? 버전? 폴더 이름 규칙? KB 형식?).
2. 바꿀 곳의 계약을 `docs/INTERFACES.md` 에서 찾는다(최신 절이 맞다).
3. 바꿀 패키지의 테스트 폴더를 먼저 돌려 지금 초록인지 확인한다.
4. 새 키가 필요하면 `sbmig/core/config.py` 의 모델과 `config/default.yml` 둘 다에 넣을 계획을 세운다.
5. 같은 일을 하는 기존 함수가 있는지 grep 한다(이 저장소는 규칙이 이미 있는 경우가 많다).

**변경 후**
1. 새 모듈·공개 함수에 한국어 docstring.
2. 새 시도 출처면 `TrySource`, 새 차단 이유면 `loop.blocked_policy` 와 그 리터럴.
3. 손댄 패키지의 테스트 + `tests/docs` + 빠른 스위트 전체(결과를 파일로 받아 종료 코드 확인).
4. 흐름이 바뀌면 관련 픽스처로 `scripts/e2e.sh --only <이름>`.
5. 손댄 파일만 `ruff check`.
6. `docs/PLAN.md` 행, 계약 변경이면 `docs/INTERFACES.md`, 결정이면 `CLAUDE.md`.
7. 커밋 전에 diff 에 비밀값·실제 좌표·버전 하드코딩이 없는지 본다.

## 확인하는 법

```bash
grep -n "^## Invariants" -A20 CLAUDE.md
grep -n "^TrySource" -A2 sbmig/core/models.py
grep -n "160\|index.yml" tests/fix/test_fix_notes.py
grep -n "max_declines_per_case\|max_edit_tries_per_case\|max_llm_tries_per_case" sbmig/core/config.py
grep -n "^HAPPY\|^SCENARIOS\|^BUILD_MATRIX" tests/fixtures/test_fixture_projects.py
sed -n '/\[tool.pytest/,/^\[tool.ruff/p' pyproject.toml
.venv/bin/python -m pytest --collect-only -m "not integration" -p no:cacheprovider | tail -3
cat fixtures/projects/webapp-bundled-jar/sbmig.e2e.yml
.venv/bin/python -m pytest tests/docs -p no:cacheprovider
```
