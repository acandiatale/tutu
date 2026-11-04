# sbmig 는 무엇인가

> 다루는 것: sbmig 의 목적, 회사의 네 가지 프로젝트 유형과 결과물, v2 흐름 한 장 그림, 불변조건 7개, v2 가 생긴 이유, 운영 모델, 용어표.
> 언제 읽나: 이 문서 묶음을 처음 열었을 때 가장 먼저. 다른 파일의 용어가 낯설 때 용어표를 다시 본다.
> 함께 읽을 파일: [01-repo-map.md](01-repo-map.md), [02-flow-and-nodes.md](02-flow-and-nodes.md), [06-company-rules-restructure.md](06-company-rules-restructure.md), [../CLAUDE.md](../CLAUDE.md)

## 1. 한 문단 요약

sbmig 는 회사의 Maven 프로젝트를 **JDK 21 + Spring Boot 4.1.0** 으로 옮기는 사내 이관 에이전트다(Python 3.10+,
명령 `sbmig`). 입력은 as-is 프로젝트 폴더나 zip 이고, 출력은 회사 레이아웃으로 바뀐 프로젝트(git 저장소, 단계마다 커밋)와
보고서다. **끝의 기준은 하나다: `mvn clean package -Dmaven.test.skip=true` 가 BUILD SUCCESS.** 테스트는 컴파일도 실행도
하지 않는다(현업 결정 "테스트는 다 스킵", `build.skip_test_compile: true`, 플래그 `tests-not-compiled`). 기동(runtime) 오류는
as-is 담당팀의 몫이다 — sbmig 는 빌드와 패키징까지만 책임지고, 나머지는 보고서 맨 앞 **인수인계(Handover)** 절로 넘긴다.

목표 버전은 `target.java_version` ("21"), `target.spring_boot_version` ("4.1.0"), `target.spring_framework_version` 에 있다.

## 2. 회사의 네 가지 프로젝트 유형과 결과물

분석이 프로젝트 종류(`CaseType`, `sbmig/core/models.py`)를 정한다: `pure_java`, `spring_legacy`, `spring_boot`,
`servlet_tomcat`, `msa` (+ `unknown`). 판정 코드는 `sbmig/analyze/classifier.py`.

| 유형 | 코드 값 | 재구성 모드 | 결과물 모양 |
|---|---|---|---|
| 순수 Java | `pure_java` | `layout` | 루트 aggregator(부모 `base-parent`) + `{name}-boot`(Boot 설정만, 부모 `boot-jar`) + `{name}-back`(모든 소스, 부모 `comp-jar`). 실행 가능한 jar |
| Spring Framework 4/5 (Boot 없음) | `spring_legacy` | `layout` | 위와 같음 + 웹 리소스가 있으면 `{name}-front`. JSP 가 있으면 boot 모듈은 실행 가능한 WAR |
| Spring Boot 2/3 | `spring_boot` | `boot_pom` | 디렉토리는 그대로, pom 만 바꿈. 루트 → `base-parent`, `*-boot` 앱 모듈 → `boot-jar`, 나머지 → `comp-jar`. 단일 모듈 Boot 앱은 `{name}-boot` 로 감싼다 |
| Tomcat 서블릿 전용 | `servlet_tomcat` | `layout` | `spring_legacy` 와 같은 분할. web.xml 등록은 생성 클래스 `WebXmlRegistrations` 로 옮긴다 |
| MSA (배포 서비스 여럿) | `msa` | `msa_pom` | 서비스마다 pom 만(부모·필수·선택 아티팩트). 디렉토리 구조는 손대지 않음 |

`{name}` 은 루트 artifactId 다(`layout.project_name` 으로 덮어쓸 수 있음). 좌표는 전부 설정에서 읽는다 — 이 문서의
`com.company.platform:base-parent` 같은 값은 `config/artifacts.example.yml` 의 자리표시자다. 자세한 규칙은
[06-company-rules-restructure.md](06-company-rules-restructure.md).

## 3. v2 흐름 한 장

```
 입력(폴더 / zip)
      |
      v
 [intake 접수] ---- Gradle / pom·build.xml 없음 / EAR 만 ----> 중단 (exit 3)
  - Maven 만. Ant(build.xml)는 pom.xml 합성 후 계속
  - 루트 pom 없는 형제 pom -> 루트 aggregator 생성
  - pom 복사본(stray) 무시, reactor 중복 정리, 사전 점검(Maven, JDK 21, 부모 설정)
  - as-is effective dependencyManagement 캡처
      |
      v
 [analyze 분석] ---- ADF 근거 2계열 이상 ----> 중단 (exit 3, adf-project)
  - 프로필(유형, Java, Spring/Boot 버전, 라이브러리), 권고(안내만)
  - 동봉 jar 스캔, 속 빈 리액터 판정(hollow-reactor -> 성공 아님)
      |
      v
 [plan 계획]  규칙 기반, LLM 없음 -> plan.json
      |
      v
 [transform 변환]  OpenRewrite 1회(레시피: Java 21 / jakarta / Boot 4 ...), 빌드 없음
      |
      v
 [restructure 재구성]  회사 레이아웃, 부모 pom, 필수·선택 아티팩트, 버전 정리,
                       동봉 jar 연결, 필터링 바이너리, JSP javax->jakarta   (LLM 없음)
      |
      v
 [structure review 구조 검토]  LLM 1회: critical -> 편집 적용 + 커밋, warning -> 보고서
      |
      v
 [verify 검증 빌드]  mvn clean package -Dmaven.test.skip=true
      |  실패
      v
 [script fix loop 수정 루프]  케이스(L2 서명)별 사다리:
      pre-try(sbmig-*, LLM 0회) -> 재사용(KB 저장 스크립트) -> 보강(LLM) -> 신규(LLM) -> 차단
      시도마다 커밋 -> 재빌드로 채점 -> 도움 안 되면 되돌림       (최대 50 라운드)
      |
      v
 [최종 빌드]  mvn clean package 를 반드시 한 번 더
      |
      v
 [finalize 마무리]  report.md(인수인계 먼저) / report.json / SBMIG-RESULT 한 줄
                    + KB 기록(kb-v2/: 오류 목록, 케이스, 저장 스크립트)
```

단계마다 코드 위치와 입출력은 [02-flow-and-nodes.md](02-flow-and-nodes.md) 에 있다.

## 4. 불변조건 7개 (깨면 안 된다)

| # | 불변조건 | 뜻 |
|---|---|---|
| 1 | **빌드 게이트** | 실행은 최종 `mvn clean package` 시도와 미해결 오류 목록이 담긴 보고서 없이 끝나지 않는다. 수정 루프는 모든 시도를 재빌드로 채점하고 도움이 안 된 것은 되돌린다 |
| 2 | 회사 특이사항은 설정에 | 회사 좌표·규칙은 config / knowledge YAML 에 두고 Python 코드에 넣지 않는다 |
| 3 | KB 는 파일 | 지식베이스는 파일 기반이다. 데이터베이스 금지 |
| 4 | 플러그인 이음새 고정 | `LLMRequest` / `LLMResponse` (1단), `FixerLike.propose_script` (2단), `sbmig.api.migrate(...)` 의 모양을 바꾸지 않는다 |
| 5 | 레시피 id 의 출처는 하나 | `knowledge/recipes/catalog.yml` 만이 레시피 id 목록이다. `sbmig recipes check` 와 스냅숏 테스트가 어긋남을 잡는다. 레시피 BOM 은 3.37.0 고정 |
| 6 | 테스트는 `out/` 에만 | 테스트·데모 실행은 `out/` 아래에만 쓴다. 저장소의 KB 폴더에 쓰지 않는다 |
| 7 | 수정 스크립트 격리 | 스크립트는 `sbmig.script` 로만 돈다: 정적 검사, `python -I` 자식 프로세스, 프로젝트 폴더로 제한된 도우미 API `p`, 시간 제한 + 프로세스 트리 종료. 실수를 막는 장치이지 악의적 작성자를 막는 샌드박스는 아니다 |

## 5. 왜 v2 인가

이전 사내 에이전트는 두 가지로 실패했다.

1. 수정 LLM 이 **빌드를 돌리지 않고 "끝났다"** 고 했다 → sbmig 는 루프의 주인이 오케스트레이터다. LLM 은 고칠 거리(스크립트·편집)만
   내고, 끝났는지는 Maven 이 정한다(불변조건 1).
2. **작은 LLM 의 레시피 선택**이 약했다 → 계획은 결정적(규칙 기반, `sbmig/plan/planner.py`)이다.

v2 는 현업의 결정 "레시피가 무엇을 바꿀지 모르니 넣지 않는다" 에서 나왔다. 그래서 OpenRewrite 는 꼭 필요한 세트만 **한 번** 돈다:
`UpgradeToJava21`, 필요할 때 `JavaxMigrationToJakarta`, Boot 프로젝트는 `UpgradeSpringBoot_4_0` + `SpringBootProperties_4_1`,
`spring_legacy` 는 `UpgradeSpringFramework_7_0` 과 쓰는 경우만 Security / Batch / Hibernate 레시피, `servlet_tomcat` 은 Servlet 6
제거 API 세트. 그 밖의 모든 문제는 **빌드 오류**로 두고, LLM 이 쓴 **파이썬 수정 스크립트**가 고친다. 스크립트는 오류 케이스별로
KB 에 저장되어 다음 프로젝트가 LLM 호출 없이 재사용한다. 레시피 세트는 `openrewrite.recipes` 에 있다.

## 6. 운영 모델

- 에이전트 **하나**가 프로젝트 **약 500 개**를 **엄격히 하나씩 차례로** 이관한다(`sbmig fleet`, `sbmig/fleet/`).
- 모든 실행이 같은 파일 KB(`kb.dir`, 기본 `kb-v2/`)를 쓴다. 앞 프로젝트가 저장한 스크립트를 뒤 프로젝트가 먼저 재사용한다
  (사다리의 "재사용" 칸, LLM 0회). 사람은 `sbmig kb cases list` / `sbmig kb cases show <case_id>` 로 본다.
- EOL 라이브러리(springfox, hystrix, sleuth …)에 `block` 정책은 **없다**("500 개가 모두 완벽할 수 없다"). 권고는 안내만:
  수정 프롬프트의 힌트 + 인수인계 절.
- LLM 키는 환경 변수(`LLM_API_KEY` 등, `.env`)에서만 온다. 키를 저장소·설정·로그에 쓰지 않는다.
- 회사 LLM 게이트웨이와 사내 에이전트 프레임워크는 어댑터 이음새(`llm.plugin`, `fix.plugin`)로 끼운다
  ([05-llm-gateway-and-prompts.md](05-llm-gateway-and-prompts.md)).

## 7. 종료 코드와 결과 한 줄

| 종료 코드 | 뜻 |
|---|---|
| 0 | 최종 빌드 성공, 차단 없음, 성공 판정 |
| 2 | 끝까지 돌았지만 성공이 아님: 미해결 오류·차단 케이스 남음, 재구성 실패, `hollow-reactor` 등 |
| 3 | 중단·거부: Maven 아님, 루트 pom 없음, ADF, Maven/JDK 문제, 사내 부모 pom 해석 불가 |

마지막 줄 `SBMIG-RESULT run=<id> exit=<n> build=<ok|FAIL> unresolved=<n> blocked=<n> … attn=<codes> review=<v>` 는 운영자가 회사 PC
화면에서 손으로 옮겨 적는 줄이다(`sbmig/report/result_line.py`). 읽는 법은 [03-run-debug-and-logs.md](03-run-debug-and-logs.md).

## 8. 용어표

| 한국어 | 영어 | 코드 이름 / 위치 | 뜻 |
|---|---|---|---|
| 접수 | intake | `Orchestrator.step_intake` | 입력 검사, Ant pom 합성, aggregator 생성, 사전 점검 |
| 분석 / 프로필 | analyze / profile | `analyze_project`, `ProjectProfile` | 유형·Java·Spring/Boot 버전·라이브러리 파악 |
| 계획 | plan | `build_plan`, `MigrationPlan` | 실행할 단계 목록(`transform.rewrite`, `restructure.*`, `verify.build`) |
| 변환(OpenRewrite 1회) | transform.rewrite | `sbmig/rewrite/runner.py` | Java 21 / jakarta / Boot 4 레시피를 한 번에 |
| 재구성 | restructure | `Restructurer.apply` | 회사 레이아웃, 부모 pom, 버전 정리 (LLM 없음) |
| 구조 검토 | structure review | `StructureReviewer` | LLM 이 pom·모듈·인코딩·리소스를 회사 규칙으로 검토 |
| 검증 빌드 | verify.build | `_step_verify` | `mvn clean package -Dmaven.test.skip=true` |
| 수정 루프 / 라운드 | fix loop / round | `BuildFixLoop` (`sbmig/loop/fixloop.py`) | 빌드 실패 → 케이스별 시도 → 재빌드 반복 |
| 케이스 / 케이스 ID | case / case_id | `sha1(L2)[:10]` | 오류 사례 하나. 폴더 `kb-v2/cases/<case_id>/` |
| L1 / L2 서명 | L1 / L2 signature | `sbmig/build/signatures.py` | 경로·숫자·이름을 지운 정규화 키. L2 가 더 거칠고 케이스를 정한다 |
| 클러스터 | cluster | `sbmig/triage/cluster.py` | 한 라운드에 같은 케이스로 묶인 오류들 |
| 사다리 | ladder | `sbmig/triage/scripts.py` | 케이스가 다음에 받을 단계: 재사용 → 보강 → 신규 → 차단 |
| 재사용 / 보강 / 신규 | reuse / augment / new | 출처 `kb-script` / `llm-augment` / `llm-new` | 저장본 그대로(LLM 0회) / LLM 이 이번 실행의 마지막 스크립트를 고침 / LLM 이 새로 씀 |
| 사전 시도 | pre-try | `sbmig-version`, `sbmig-resources`, `sbmig-jakarta`, `sbmig-bundled` | LLM 전에 sbmig 가 결정적으로 한 번 고침(버전 채우기, 필터링 제외 확장자, jakarta 빌드 교체, 동봉 jar 연결) |
| 시도 | try / attempt | `FixAttempt` | 스크립트(또는 편집) 한 번 + 재빌드 + 점수 |
| 저장된 스크립트 | stored script | `kb-v2/cases/<case_id>/fix_v<N>.py` | 케이스 폴더에 버전별로 쌓이는 수정 스크립트 |
| 스크립트 없는 직접 수정 | `no_script` + edits | `sbmig/script/edits.py` | LLM 이 스크립트 대신 편집 목록을 돌려줌 |
| 채점 | scoring | `_score` (fixloop) | 재빌드 결과로 개선 / 부분 / 변화 없음 / 악화 / 판정 불가 |
| 되돌리기 | rollback / revert | `script_rollback` 저널 | 도움이 안 된 시도의 커밋을 되돌림 |
| 차단 / 차단 정책 | BLOCKED / blocked_policy | `loop.blocked_policy`, `sbmig/triage/blocked.py` | 더 시도하지 않는 케이스와 그 기준. `repeated_failure`, `needs_owner` 등 |
| 소유팀 결정 | needs_owner | `BlockReason.NEEDS_OWNER` | LLM 이 프로젝트 밖 장애물을 이유로 거절 → 담당팀이 정함 |
| 지식베이스(파일) | KB | `kb.dir`, `sbmig/errors/cases.py` | `errors.jsonl`, `cases.md`, `cases/<case_id>/` — DB 금지 |
| 재색인 | reindex | `sbmig kb reindex` | 원본 오류 기록으로 케이스·실적을 다시 셈 |
| 인수인계 섹션 | Handover | `sbmig/report/handover.py` | 보고서 맨 앞, 사람이 이어서 할 일 |
| 운영자 확인 필요 | operator check | `major=` 필드 | 메이저 업그레이드·라이브러리 교체 등 사람이 봐야 하는 것 |
| 권고 | advisory | `knowledge/libraries/advisories.yml` | EOL 라이브러리 안내(차단 없음) |
| 필수 / 선택 사내 아티팩트 | mandatory / optional | `artifacts.mandatory`, `artifacts.optional` | 루트 pom 의 필수 2개, 쓰던 것만 올리는 선택 8개 |
| 사내 부모 pom | base-parent / boot-jar / comp-jar | `artifacts.parent`, `artifacts.boot_parent`, `artifacts.comp_parent` | 루트 / boot 모듈 / 그 밖 모듈의 부모 |
| 부모 체인 | parent chain | `sbmig/build/parent_chain.py` | 사내 부모 pom 의 `<parent>` 와 BOM import 를 따라 읽은 관리 버전·속성 |
| 동봉 jar | bundled jar | `sbmig/analyze/bundled_jars.py`, `sbmig/restructure/bundled_jars.py` | 트리 안에 있지만 어떤 pom 도 선언하지 않은 jar(`WEB-INF/lib` …) |
| 속 빈 리액터 | hollow reactor | `sbmig/analyze/hollow.py`, 플래그 `hollow-reactor` | 모듈이 컴파일하는 소스가 없고 진짜 소스는 트리의 다른 곳 → 성공 아님 |
| 떠도는 pom 복사본 | stray pom copy | `find_stray_pom_copies` (`sbmig/analyze/pom_parser.py`) | IDE 출력·백업 폴더 안의 모듈 통째 복사본 → 무시 |
| ADF 프로젝트 | Oracle ADF (JDeveloper) | `sbmig/analyze/adf.py`, 플래그 `adf-project` | 근거 2계열 이상이면 변환하지 않고 중단 |
| 합성 pom | Ant → Maven | `sbmig/analyze/ant.py` | build.xml 만 있는 프로젝트에 pom.xml 을 만들어 줌 |
| 생성 aggregator | generated aggregator | `sbmig/analyze/aggregator.py` | 루트 pom 없는 형제 pom 들을 묶는 루트 pom |
| 케이스 스레드 | case thread | `llm.case_thread` | 한 케이스의 보강 요청이 게이트웨이 세션을 이어 받는 방식(기본 꺼짐) |
| 재생 / 가짜 LLM | replay / fake provider | `llm.provider: fake` | 오프라인 테스트용 LLM 응답 재생(`replay/llm-NNN.json`) |
| 회사 LLM 게이트웨이 | company gateway | `sbmig/fix/company.py`, `llm.gateway` | `LLM_BASE_URL` 로 가는 `requests.post` 한 번 |
| 일괄 실행 | fleet | `sbmig fleet` | 여러 프로젝트를 순차로 |
| 현업 / 운영자 | business team / operator | — | 요청한 팀 / 도구를 돌리는 사람 |

## 확인하는 법

```
.venv/bin/python -m sbmig.cli --help                       # 명령 목록
grep -n "class CaseType" -A8 sbmig/core/models.py          # 유형 값
grep -n "^target:" -A4 config/default.yml                  # 목표 버전
grep -n "def report_exit_code" -A12 sbmig/report/result_line.py   # 종료 코드 0 / 2 / 3
grep -n "TrySource" -A2 sbmig/core/models.py               # 시도 출처(pre-try 이름 포함)
grep -n "recipes:" -A40 config/default.yml                 # 레시피 세트
grep -n "max_iterations" config/default.yml                # 수정 루프 라운드 수
sed -n 1,40p sbmig/errors/cases.py                         # KB 폴더 모양
.venv/bin/python -m pytest tests/docs/test_agent_doc.py -p no:cacheprovider -q
```
