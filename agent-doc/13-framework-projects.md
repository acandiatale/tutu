# 사내 프레임워크 프로젝트 (범위 `jdk-jakarta`)

> 다루는 것: 사내 프레임워크 프로젝트를 찾는 법, 그 실행이 하는 일과 하지 않는 일, 켜는 법, 이름, 결과 줄·보고서·화면, 진단, 수정 루프, 취약점 조치, 고칠 자리.
> 언제 읽나: 결과 줄에 `scope=jdk-jakarta`가 있을 때, 프레임워크 프로젝트의 빌드·변환이 실패했을 때, 이름이 틀릴 때, 범위 판정이 틀렸을 때, 이 규칙을 바꾸기 전에.
> 함께 읽을 파일: [00](00-what-sbmig-is.md), [02](02-flow-and-nodes.md), [03](03-run-debug-and-logs.md), [04](04-cases-scripts-kb.md), [07](07-config-and-knowledge.md), [12](12-remediation-report.md)

## 1. 규칙

사내 프레임워크 아티팩트(`framework.artifacts`)를 의존성으로 가진 프로젝트는 회사 레이아웃과 Spring Boot 4로 옮기지 않는다.
JDK 21과 javax에서 jakarta로의 변환만 하고 빌드한 뒤 변환한 트리를 내려준다. 빌드 오류는 수정 루프가 고친다(`framework.fix_loop: true`,
기본, 5절). 부모 pom과 프레임워크 선언·버전은 건드리지 않는다. 취약점 조치는 빌드가
통과해도 실패해도 이어서 돈다(오픈소스 라이브러리 검사라서. `remediation.after_failed_build`, 8절). 이 범위를 `jdk-jakarta`라고 부른다.

| 하는 일 | 하지 않는 일 |
|---|---|
| OpenRewrite 1회: `framework.recipe_sets`의 세트(기본 `jdk21`, Java EE javax를 쓰면 `jakarta`) | 재구성(회사 부모, 모듈 분할, 필수·선택 아티팩트, 버전 정리) |
| JSP·태그 파일의 javax 이름을 jakarta로(`transform.jsp_jakarta`) | 구조 검토(LLM 검토) |
| 빌드(`verify.build`)와 수정 루프(사전 시도, 저장된 스크립트, LLM), 남은 오류를 보고서에 알림 | 회사 부모 맞추기 사전 시도(`sbmig-parent`), 프레임워크·`<parent>`를 바꾸는 시도(되돌림) |
| 레시피 전 MS949·EUC-KR 소스의 인코딩 선언, 레시피 뒤 as-is `provided` scope 되돌리기 | `framework.recipe_sets`에 없는 레시피 세트(Spring Framework 7, Security, Boot 4 등) |
| 이어지는 취약점 조치, 빌드가 실패해도([11](11-remediations.md)) | 회사 부모·필수·선택 아티팩트 추가, 부모 pom 변경, 프레임워크 버전 변경 |

`framework.fix_loop: false`면 변환만 한다: 빌드 1회, 사전 시도·저장된 스크립트·LLM 호출 없음, 변환 뒤에는 pom과 소스에 아무것도 쓰지 않는다.
빌드 게이트는 어느 쪽이든 그대로다. 이 실행이 어느 쪽이었는지는 `FrameworkUse.fix_loop`에 남는다.

프로젝트는 자기 부모 pom, 모듈 구조, Spring·Spring Boot 라인, 프레임워크 아티팩트의 버전을 그대로 가진다. 한 실행의 범위는
하나다. MSA에서 서비스 하나만 프레임워크를 써도 실행 전체가 `jdk-jakarta`로 돈다.

## 2. 켜는 법

검사는 `framework.artifacts`가 비어 있으면 꺼져 있다. 실제 아티팩트 이름은 회사 좌표라 회사 `config/artifacts.yml`에만 적는다.

```yaml
framework:
  artifacts: ["com.example.fw:example-fw-core"]   # 자리표시자. 실제 g:a 또는 artifactId만
  name: "Example"                                  # 만든 이름이 틀릴 때만. 자리표시자
```

| 키 | 기본 | 뜻 |
|---|---|---|
| `framework.artifacts` | `[]` | 프레임워크 아티팩트 패턴. `artifactId` 또는 `groupId:artifactId`, 두 부분 모두 `*` 사용 가능. 비우면 검사하지 않는다 |
| `framework.scope` | `jdk-jakarta` | `jdk-jakarta`는 위의 줄인 실행, `full`은 찾은 사실만 보고서에 적고 전체 이관을 한다 |
| `framework.transitive` | `apply` | 다른 라이브러리가 가져올 때만 있는 경우. `apply`는 직접 선언과 같게, `note`는 보고서에만 적고 전체 이관, `off`는 찾지 않는다(Maven 1회 절약) |
| `framework.name` | `""` | 화면과 보고서에 보일 프레임워크 이름. 비우면 아티팩트 이름에서 만든다(아래) |
| `framework.fix_loop` | `true` | `true`는 수정 루프로 빌드 오류를 고친다(5절). `false`는 변환 뒤 빌드 1회, 오류는 보고만 한다 |
| `framework.recipe_sets` | `[jdk21, jakarta]` | 줄인 실행이 돌릴 `openrewrite.recipes`의 세트 이름. 모르는 이름은 설정 오류다. `jakarta`는 유형 규칙을 그대로 따르고(`pure_java`·`unknown`은 Java EE javax import가 있을 때만), `servlet_jakarta_10`은 `jakarta` 세트와 함께일 때만 돈다 |
| `remediation.inhouse_groups` | `[mandatory, optional, parents, framework]` | 취약점 조치에서 사내 몫으로 볼 그룹. `framework`가 위 패턴이다(8절) |

패턴 비교는 대소문자를 가린다. `:`가 없는 패턴은 artifactId하고만 비교하므로 groupId가 무엇이든 맞는다. `groupId:artifactId`
패턴은 groupId까지 맞아야 한다. 언제 `full`이나 `note`를 쓰나: 프레임워크 프로젝트도 회사 레이아웃으로 옮기기로 정한 경우는
`framework.scope: full`, 다른 사내 라이브러리가 프레임워크를 끌고 오기만 하는 프로젝트를 전체 이관하려면 `framework.transitive: note`.

프레임워크 이름(`FrameworkConfig.display_name`): `framework.name`이 있으면 그 값이다. 비어 있으면 프로젝트에서 찾은 아티팩트,
그다음 설정한 패턴의 artifactId에서 만든다(`*`가 든 패턴은 건너뜀). `-`, `_`, `.`로 나눈 끝 낱말 가운데 `core`, `framework`, `fw`,
`starter`, `parent`, `bom`, `boot`, `common`, `base`, `api`를 떼고 첫 글자를 대문자로 바꾼다. 예: `example-fw-core` → `Example`.
만들 수 없으면 이름 없이 "사내 프레임워크"로 적는다. 회사 할 일: 만든 이름이 맞으면 할 것이 없다. 틀리면 회사 `config/artifacts.yml`의
`framework:` 아래에 `name` 한 줄을 넣고 이미지를 다시 빌드한다(파일이 이미지에 들어 있다). 실제 이름은 회사 이름이라 이 저장소에 적지 않는다.
`framework.fix_loop: false`는 변환만 하고 오류는 담당자에게 넘기기로 정했을 때 쓴다.

서블릿 API의 높이는 회사가 정할 스위치다. 기본 `jakarta` 세트는 서블릿 API를 Jakarta EE 9 기준(서블릿 5.0)으로 올린다. Tomcat 10.1 이상에서
돌릴 프로젝트면 `framework.recipe_sets`에 `servlet_jakarta_10`을 더한다(Servlet 6에서 없어진 API 정리).

## 3. 어디서 찾나 (순서대로)

코드: `sbmig/analyze/framework.py`. `analyze_project`가 `detect_framework`와 `decide_scope`를 호출하고, 오케스트레이터의
`_check_framework`가 간접 의존성 검사를 더한다.

| 순서 | 출처(`source`) | 찾는 곳 | Maven 호출 |
|---|---|---|---|
| 1 | `direct` | 프로젝트 pom의 `<dependencies>`. `<dependencyManagement>`에만 있는 항목은 근거가 아니다 | 없음 |
| 2 | `parent` | 접수 때 캡처한 as-is effective pom(`logs/effective-depmgmt.json`)의 모듈별 `<dependencies>`에 있는데 어떤 프로젝트 pom도 선언하지 않은 것. 프로젝트 밖 부모 pom에서 상속한 것이다 | 없음(접수의 캡처를 읽음) |
| 3 | `bundled` | pom이 선언하지 않은 트리 안의 jar(동봉 jar). 좌표가 있으면 좌표로, 없으면 파일 이름(`<artifactId>[-<버전>].jar`)으로 | 없음 |
| 4 | `transitive` | 위 셋에서 못 찾았고 `framework.transitive`가 `off`가 아니고 실제 Maven이고 dry run이 아닐 때만. as-is 트리에서 `mvn dependency:tree -Dincludes=<패턴>` | 대상마다 1회 |

간접 검사는 `sbmig.build.deptree.dependency_tree_status`로 돌린다. 대상은 루트 reactor 하나, 루트 aggregator가 모든 서비스를 묶지
않는 MSA면 서비스 루트마다 하나다. 결과 트리는 `logs/` 아래 `deptree-*.txt`에 남는다. 깊이 1에서 맞는 노드(정적 파싱이 놓친
profile·속성 선언)는 `direct`로 적고, 더 깊은 노드는 `transitive`로 `via`(모듈의 의존성부터 프레임워크까지의 g:a:v 경로)와 함께 적는다.

읽지 못한 출처는 실행을 멈추지 않고 `unchecked`에 이유를 남긴다:

| `unchecked` 값 | 뜻 |
|---|---|
| `parent: no as-is effective pom` | 접수의 effective pom 캡처가 없다. 부모 pom에서 상속한 프레임워크는 찾지 못한다 |
| `bundled: intake.bundled_jars is off` | 동봉 jar 스캔이 꺼져 있다 |
| `transitive: <이유>` | `dependency:tree`가 실패했거나(Maven 오류, 루트 pom 없음) 대상 일부만 읽었다 |

범위 판정(`decide_scope`): `framework.scope`가 `jdk-jakarta`이고 `direct`·`parent`·`bundled` 근거가 있거나, `transitive` 근거가 있고
`framework.transitive: apply`이면 `jdk-jakarta`. 그 밖에는 `full`. 판정 이유는 영어 한 문장(`reason`)으로 저장한다.

## 4. 줄인 실행의 단계

- 분석: 찾으면 플래그 `framework-project`(attn 코드 아님), 저널 `framework_project`(`name`, `fix_loop` 포함), INFO 줄 하나, 인수인계 줄 하나(ADF·속 빈 reactor 줄 다음).
  못 찾으면 저널 `framework_check`만 남긴다. `profile.json`의 `framework`에 근거가 저장된다.
  못 찾았는데 `unchecked`에 `parent: …`와 `transitive: …`가 둘 다 있으면(부모 pom도 의존성 트리도 읽지 못함) 판단을 못 한 것이다: 플래그
  `framework-unchecked`(attn 코드 아님, 종료 코드 그대로), WARNING `분석: 사내 프레임워크 여부를 확인하지 못함 (…)`, 인수인계 줄 "framework check could not run
  (the parent pom and the dependency tree could not be read): if this project uses the in-house framework it must not be migrated in full — confirm
  before using the result"(ko 보고서는 「사내 프레임워크를 쓰는 프로젝트인지 확인하지 못했습니다 …」). 실행은 전체 범위로 계속한다.
- 계획(`sbmig/plan/planner.py`): `MigrationPlan.scope`가 `jdk-jakarta`이고 레시피는 `framework.recipe_sets`의 세트만이다. 단계는 `transform.rewrite[@서비스]`, (JSP가 있고 jakarta 세트를
  계획했고 `layout.jsp_jakarta_rename`이 켜져 있으면) `transform.jsp_jakarta`, `verify.build[@서비스]`뿐이다. `restructure.*`,
  `restructure.review`, `transform.compile`은 만들지 않는다. 저널 `plan`에 `scope`, `name`, `fix_loop`가 붙는다. 첫 계획 노트는 `scope: jdk-jakarta — framework project (…)`로 시작한다.
  `jakarta` 세트는 `pure_java`·`unknown` 유형에서 Java EE javax import가 없으면 빠진다.
- MSA: 루트 aggregator가 모든 서비스를 묶으면 루트에서 OpenRewrite 1회, 아니면 서비스마다(공유 라이브러리 루트 먼저).
- 소스 인코딩(OpenRewrite 전): JDK 18부터 javac의 기본 소스 인코딩이 UTF-8이라 선언 없는 MS949·EUC-KR 소스는 JDK 21에서 컴파일되지 않는다.
  또 OpenRewrite는 읽지 못하는 소스를 건너뛰므로(저널 `rewrite_parse_problems`, 플래그 `rewrite-parse-problems`) MS949 파일의 javax import가
  그대로 남는다. 그래서 레시피 전에 그런 모듈의 pom에 `project.build.sourceEncoding`을 LLM 없이 적는다(부모 체인에 맞는 선언이 없을 때만). 커밋
  `transform: source encodings written`, 저널 `source_encodings {step, entries}`, 메모 `source encoding written: <pom>: project.build.sourceEncoding=MS949 (…)`,
  스위치 `layout.legacy_context.write_source_encoding`. 유지하는 외부 부모가 이미 같은 값을 정하면 그대로 두고, 다른 값이면 부모 값을 두고
  인수인계 한 줄을 남긴다. 한 모듈에 UTF-8과 섞인 소스, 맞지 않는 컴파일러 `<encoding>`도 인수인계 줄이다.
- scope 되돌리기(OpenRewrite 뒤): jakarta 레시피는 `javax.servlet-api`의 `provided`를 `jakarta.servlet-api`의 compile로 바꿔 서블릿 API가 WAR에
  들어가게 한다. `transform.rewrite`가 끝나면 as-is에서 `provided`였던 scope를 되돌린다. 커밋 `transform: as-is provided scopes restored`, 저널
  `scopes_restored {step, entries}`. 스위치는 전체 이관과 같은 `layout.legacy_context.restore_asis_scopes`다.
- 커밋 순서: (고친 pom이 있으면) `transform: pom encodings corrected`([07](07-config-and-knowledge.md)), `transform: source encodings written`, `rewrite: transform.rewrite`, `transform: as-is provided scopes restored`,
  `transform: JSP javax → jakarta`. 전체 이관의 순서는 다르다(재구성이 레시피 뒤에 인코딩을 적는다).
- `transform.jsp_jakarta`: 재구성기를 거치지 않고 JSP 이름 바꾸기만 한다. 커밋 `transform: JSP javax → jakarta`, 저널 `jsp_jakarta`.
- 빌드(`verify.build`): 실패하면 수정 루프(5절), 그 뒤 최종 빌드. `framework.fix_loop: false`면 단계마다 한 번 빌드하고 사전 시도, 저장된 스크립트, LLM 호출이
  없고 KB에서 가져다 쓰는 것도 없다. 빌드 오류는 그대로 이 실행의 남은 오류가 된다(한국어 안내서의 절 「변환 뒤 남은 빌드 오류」, 6절). 오류 목록 항목은 열린 채로 남고,
  프레임워크 프로젝트의 오류는 여기서 고치지 않는다는 메모가 붙는다. 저널 `verify_loop_skipped {step, target, reason, errors, entries}`.
  빌드가 실패했으면 인수인계 맨 앞쪽(ADF·속 빈 리액터 줄 다음)에 같은 뜻의 한 줄이 들어간다. 계획 단계의 LLM 레시피 조언(`plan.advisor`)도 돌지 않는다.
- 부모 pom: as-is 부모를 회사 부모로 맞추지 않는다(`openrewrite.unresolvable_asis_parent`를 보지 않음). Maven이 부모 pom을 받지 못하면
  변환 단계나 수정 루프에서 LLM 없이 중단한다: 플래그 `aborted:parent-not-found`, 차단 사유 `parent_not_found`, 저널 `abort`, 중단 사유
  `Maven cannot resolve the parent pom <g:a:v>; a framework project keeps its own parent pom, so the repository must serve it (settings: …)`.
  부모가 프로젝트 자신의 모듈이면 수정 루프가 다룬다.
- 성공: 빌드가 성공이면 exit 0이고 취약점 조치가 이어 돈다. 재구성이 없으니 `restructure-failed`도 없다.
- 실패: 최종 빌드가 실패하면 exit 2이고 `result.zip`은 변환한 트리다. 남은 오류는 프로젝트 담당자와 프레임워크 담당자의 몫이다.
  취약점 조치는 수정 루프를 돌았든 아니든 이어서 돈다(8절). 이관은 실패(exit 2) 그대로다.

## 5. 수정 루프 (`framework.fix_loop: true`, 기본)

`false`면 이 절의 어느 것도 돌지 않는다. 켜져 있으면 `BuildFixLoop`(`sbmig/loop/fixloop.py`)가 `profile.reduced_scope`를 보고 아래처럼 돈다. 사다리 자체는 [04](04-cases-scripts-kb.md) 그대로다.

- 버전 사실: 프로젝트 자기 부모 체인이 기준이다. as-is 캡처가 "부모 체인" 자리에 들어가고, 회사 부모 체인의 사실과 목표 Boot BOM은
  쓰지 않는다. 순서는 부모가 관리하면 버전 없음, 아니면 as-is 버전, 아니면 허용될 때 저장소의 최신 릴리스.
- 프레임워크 버전은 sbmig가 쓰거나 바꾸거나 지우지 않는다. 프레임워크 키는 보호 키라서 사전 시도도 건드리지 않는다
  (`sbmig-version`은 버전을 박지 않고, `sbmig-jakarta`는 다른 라인으로 바꾸지 않는다). `sbmig-parent`는 돌지 않는다.
- 가드(`_framework_guard`): LLM의 스크립트나 편집이 프레임워크 아티팩트 선언을 지우거나 버전을 바꾸거나, 프로젝트가 쓰지 않던 버전으로
  더하거나, 어떤 pom의 `<parent>`를 바꾸면 커밋하지 않고 되돌린다. 버전의 `${…}`는 프로젝트 안 부모 체인으로 풀어 앞뒤를 비교하므로 다른 pom
  (루트)의 속성으로 바꾼 것, 프레임워크에 맞는 import가 쓰는 속성을 바꾼 것도 같다(g:a가 패턴에 맞지 않는 BOM import는 알아보지 못한다). 실패 문장은 `framework project:`로 시작하고 다음 요청의 이전 시도에
  보인다. WARNING `적용 취소 (…): 프레임워크 프로젝트에서 바꾸면 안 되는 것을 바꿈 — …`, 저널 `framework_guard {attempt, case, reason}`.
- javax 기준 프레임워크: javax 케이스의 라이브러리가 프레임워크 아티팩트 자신이면 사전 시도·LLM 없이 바로 `external_javax_api`로
  차단한다. 제안 문장은 그 아티팩트를 이름으로 적고 담당자가 jakarta 빌드를 내야 한다고 말한다. 로그 `프레임워크 아티팩트가 javax 로 컴파일됨: <g:a> …`.
  프레임워크를 거쳐서만 들어오는 javax 라이브러리는 마무리 때 최종 의존성 트리로 경로를 채운 뒤 같은 제안으로 바뀐다(`framework_via_suggestions`).
- 프롬프트: 시스템 프롬프트(`sbmig/fix/prompts/script_system_v1.md`)가 줄인 범위용 소개와 규칙을 쓰고, 사용자 프롬프트에는
  "Project after the restructure" 대신 "Scope of this run" 절(`FixContext.scope_facts`)이 들어간다. 프레임워크 근거마다 "버전을 바꾸지
  말고 지우거나 바꾸지 말 것", Spring Boot·회사 부모 추가와 `<parent>` 변경 금지가 적힌다.

## 6. 결과 읽기

사람이 이 실행을 만나는 자리마다 이름을 넣은 문장 `<이름> 프레임워크는 JDK 21, Jakarta만 적용합니다.`가 나온다(이름이 없으면
`사내 프레임워크 프로젝트는 JDK 21, Jakarta만 적용합니다.`). 덜 받는 줄 모르기 쉬워 눈에 띄게 둔다.

| 자리 | 줄인 실행에서 보이는 것 |
|---|---|
| `SBMIG-RESULT` 줄 | `major=` 뒤, `flow=`·`remediation=` 앞에 ` scope=jdk-jakarta`. 전체 이관에는 이 토큰이 없다. `framework.fix_loop: false`면 `llm=0`, 빌드가 실패하면 `exit=2 build=FAIL`과 함께 `remediation=INCOMPLETE`(이어 돈 조치가 버전을 바꿨을 때) |
| 변환 실패(`rewrite-failed`) | 빌드가 통과해도 성공이 아니다: `exit=2 build=ok … attn=rewrite-failed`, 머리 표 「❌ 변환 실패 (빌드는 통과)」, 프레임워크 절은 변환을 한 일에서 뺀다. 조치는 그래도 이어진다([00](00-what-sbmig-is.md)) |
| `report.md` 머리 표 | 한국어 안내서는 `SBMIG-RESULT` 줄의 `scope=jdk-jakarta`. en 보고서는 `Scope` 줄 "JDK 21 and jakarta only (in-house framework project)" |
| `report.md` 머리 표 바로 아래 | 절 `## <이름> 프레임워크 프로젝트`(이름이 없으면 `## 사내 프레임워크 프로젝트`). 첫 줄이 위 문장(굵게), 이어서 근거마다 한 줄(직접 선언, 부모 pom에서 상속, jar 파일로 포함, 다른 라이브러리를 통해 들어옴), 한 일(JDK 21, Jakarta 변환, 빌드, 수정 루프가 켜져 있으면 빌드 오류 수정, 조치가 돌았으면 취약점 조치), 하지 않은 일(구조 정리, 구조 검토, 꺼져 있으면 빌드 오류 자동 수정. 부모 pom과 프레임워크 버전은 그대로), "확인하지 못한 곳". en 보고서는 `## <Name> framework project`, 첫 줄 "**<Name> framework projects get JDK 21 and Jakarta only.**" |
| 「변환 뒤 남은 빌드 오류」 | 수정 루프가 꺼진 실행의 빌드가 실패하면 ko 안내서는 「해결하지 못한 오류」 대신 `## 변환 뒤 남은 빌드 오류 — N건`(`NO_FIX_TITLE`): 자동으로 고치지 않았고 프로젝트·프레임워크 담당자가 고칠 오류라는 앞 문장, 「확인해 주세요」 첫 줄이 이 절을 가리킨다. en 보고서는 "Error list"의 열린 항목(메모 `not tried: framework project — …`) |
| 변환 단계의 pom 정정 | pom 인코딩, 소스 인코딩, scope 정정은 빌드 오류가 아니다. 한국어 안내서는 「코드 수정 내역」의 자동 변환에 "sbmig가 변환 단계에서 직접 고친 pom" 으로 적는다. `errors.md` 에서는 `seen` `transform` 항목이다(en 보고서: Error list의 "transform — pom corrections by sbmig, verified by the next build", Handover의 `pom corrections by sbmig`). 고친 시도가 없었던 버전 행의 비고는 "OpenRewrite 레시피"(en: `recipe`)다 |
| 찾았지만 전체 이관 | 절 제목이 `## 사내 프레임워크`, 설정(`framework.scope: full` 또는 `framework.transitive: note`)에 따라 전체 이관했다는 한 줄 |
| 「확인해 주세요」(인수인계) | 사내 프레임워크 프로젝트라는 줄(근거, JDK 21과 jakarta만, 재구성·구조 검토 없음, 부모 pom·프레임워크 버전 유지). 수정 루프가 꺼져 있으면 빌드 오류를 고치지 않았다는 한 줄이 더 붙는다 |
| 웹 화면 | 실행 카드(저널이 알리는 즉시)와 결과 카드에 강조한 안내 상자: 위 문장을 굵게, 그 아래 하지 않는 일. 단계 줄의 재구성·구조 검토는 점선 원과 `하지 않음`으로 보인다(저널 `framework_project`나 `plan`의 `scope`를 받으면 켜짐). 서버 기본 설정에 `framework.artifacts`가 있고 `framework.scope`가 `jdk-jakarta`면 올리기 카드에도 늘 보인다. `remediation.after_migration`이 켜져 있고 `remediation.after_failed_build`가 `framework`면(수정 루프 설정과 무관) 안내에 「취약점 조치는 빌드 결과와 관계없이 이어서 합니다.」가 붙는다. 조치 표시는 8절 |
| HTTP | `GET /api/configs`의 `framework: {configured, name, fix_loop, remediation_always}`(`remediation_always` = 위 조건), `GET /api/runs/{id}`의 `framework: {name, scope, fix_loop}`(줄인 범위로 돈 실행만)와 `remediation`의 `baseline_tolerated`, `reasons` |
| 저장 파일 | `profile.json`과 `report.json`의 `framework`에 `name`, `fix_loop` |

## 7. 진단

물어볼 줄(회사 PC에서는 파일 대신 이 줄들을 묻는다):

1. `SBMIG-RESULT` 줄의 `scope=` 유무와 `attn=`.
2. `sbmig.log`의 INFO `분석: 사내 프레임워크 프로젝트 (…) - JDK와 jakarta만 적용`(또는 `- 설정에 따라 전체 이관`).
3. `journal.jsonl`의 `framework_project`(scope, name, fix_loop, artifacts, evidence, unchecked, reason) 또는 `framework_check`(found false, unchecked).
4. 저널 `plan`의 `scope`, 그리고 `framework_guard`, `abort`의 `reason`.

| 증상 | 볼 곳과 원인 |
|---|---|
| `scope=jdk-jakarta`이고 exit 2, `build=FAIL` | 수정 루프가 다 고치지 못했다: 「해결하지 못한 오류」와 `errors.md`의 시도를 본다(차단 사유, `framework_guard`). 루프가 꺼진 실행이면(`llm=0`, 저널 `verify_loop_skipped`) 고치지 않고 보고한 것이고 「변환 뒤 남은 빌드 오류」가 담당자에게 넘길 목록이다. 어느 쪽이든 `result.zip`은 변환한 트리, 이어 돈 조치의 `INCOMPLETE`(`baseline_build_failed`, 8절)는 정상이다 |
| 화면·보고서의 프레임워크 이름이 틀리거나 "사내 프레임워크"로만 나온다 | 저널 `framework_project`의 `name`. 만든 이름이 틀리면 회사 yml에 `framework.name`을 적는다. 비어 있으면 아티팩트 이름에서 만들 수 없었다(패턴이 모두 `*`를 쓰는 등) |
| 프레임워크 프로젝트인데 LLM이 불리지 않아야 한다 | 회사 yml에 `framework.fix_loop: false`. 저널 `framework_project`·`plan`의 `fix_loop`가 그 실행의 값이다 |
| 플래그 `framework-unchecked` | 부모 pom과 `dependency:tree`를 둘 다 읽지 못해 판단하지 못했다. 저널 `framework_check`의 `unchecked`와 `effective_depmgmt`, `deptree-*.txt`를 본다. 사내 프레임워크를 쓰는 프로젝트면 결과를 쓰지 말고, 저장소·settings를 고쳐 다시 돌린다 |
| 프레임워크 프로젝트인데 전체 이관이 돌았다 | `framework_check`가 있으면 못 찾은 것이다. `unchecked`에 `parent: no as-is effective pom`이면 부모 pom 상속을 확인하지 못했다(접수의 캡처 실패, 저널 `effective_depmgmt`). `transitive: …`면 `dependency:tree` 실패. 아무것도 없으면 패턴이 실제 groupId·artifactId와 다르다(대소문자, `groupId:` 부분). `framework_project`인데 `scope: full`이면 `reason`이 설정 때문이라고 말한다 |
| 프레임워크가 아닌데 줄인 실행이 돌았다 | `framework_project.evidence`의 `source`와 `where`. artifactId만 쓴 패턴은 다른 groupId의 같은 이름에도 맞는다 |
| BLOCKED `external_javax_api`, 제안에 프레임워크 이름 | 프레임워크 아티팩트(또는 그것이 가져오는 라이브러리)가 아직 javax 기준이다. 프로젝트에서 고칠 수 없고 프레임워크 담당자가 jakarta 빌드를 내야 한다. LLM 호출 없음이 정상이다 |
| exit 3, `aborted:parent-not-found` | 저장소가 프로젝트의 부모 pom을 주지 않는다. settings·미러를 고치거나 부모 pom을 Nexus에 올린 뒤 다시 돌린다 |
| `unmappable character for encoding UTF8` | 저널 `source_encodings`와 인수인계의 인코딩 줄. 부모가 다른 값을 정했거나 소스가 섞였거나 스위치가 꺼져 있다 |
| 서블릿 API가 WAR에 들어갔다 | 저널 `scopes_restored`가 없으면 `layout.legacy_context.restore_asis_scopes`가 꺼져 있거나 as-is가 `provided`가 아니었다 |
| 시도가 계속 되돌려진다 | 저널 `framework_guard`의 `reason`. LLM이 프레임워크 선언, 그 버전을 정하는 속성(루트 pom의 것 포함), `<parent>`를 건드렸다 |

간접 검사 비용: 앞의 세 곳에서 못 찾은 모든 프로젝트가 분석 단계에서 `dependency:tree`를 한 번 더 돈다. 문제면 `framework.transitive: off`.

## 8. 취약점 조치에서

`remediation.inhouse_groups`의 `framework` 그룹은 `framework.artifacts` 패턴에 맞는 아티팩트다. 취약한 라이브러리가 프레임워크 아티팩트
자신이거나 의존 경로에 프레임워크가 있으면 잔여 `inhouse_artifact`이고 제안도 override도 하지 않는다. 프레임워크 버전은 어떤 경우에도
제안하지 않는다. 보고서 표시와 정책 해시 규칙은 [12](12-remediation-report.md) 3절.

빌드가 실패한 줄인 실행도 수정 루프를 돌았든 아니든 조치로 이어진다(`remediation.after_failed_build: framework`, 기본; `never`면 통과한 실행만).
이관 보고서의 조치 절 첫 문장은 루프가 돌았으면 「빌드 오류 자동 수정을 거쳤지만 마이그레이션 빌드는 통과하지 않았습니다. …」다.
기준 빌드가 실패한 조치에서는 후보의 빌드 오류 수정([11](11-remediations.md) 2절)이 돌지 않는다. 버전을 바꿨으면 `INCOMPLETE`, 첫 사유 `baseline_build_failed`(빌드로 확인한 변경이 아니므로)이고,
요청자는 빌드 오류를 고친 뒤 바뀐 버전으로 한 번 빌드한다. 바꾼 것이 없거나 수정본이 빌드되면 보통 조치처럼 끝난다(`baseline_build_failed` 없음). 수정본에
새 빌드 오류가 생기면 `FAILED`. 규칙과 기록은 [11](11-remediations.md) 1.1.

화면: 사유가 `baseline_build_failed` 하나뿐인 `INCOMPLETE`는 「일부만 조치됨」 대신 「조치함 · 빌드 미확인」(조치 상태; 「지난 실행」에는
조치 상태를 붙이지 않는다), 「조치했지만 빌드로 확인하지 못했습니다」(이관 보고서)다. 다른 사유가 남으면 「일부만 조치됨」. 결과 ZIP의
「빌드 미확인」 표시와 「취약점 조치」 칸의 안내는 결과 ZIP이 `build_tolerated`이고 적용한 변경이 있을 때만, 새 오류를 만든 수정본은 「빌드 실패본」.

## 9. 고칠 자리

| 요청 | 파일 |
|---|---|
| 패턴 비교, 설정 키, 프레임워크 이름 | `sbmig/core/config.py` (`FrameworkConfig`, `display_name`), `config/default.yml`의 `framework:` 절 |
| 찾는 곳, 범위 판정, 인수인계 문장 | `sbmig/analyze/framework.py` (`detect_framework`, `transitive_evidence`, `decide_scope`, `framework_followup`) |
| 분석 단계 기록, JSP 단계, 부모 중단 | `sbmig/loop/orchestrator.py` (`_check_framework`, `_step_jsp_jakarta`, `_reduced_parent_abort`) |
| 줄인 계획, 레시피 세트 | `sbmig/plan/planner.py`, `framework.recipe_sets` |
| scope 되돌리기, 소스 인코딩 | `sbmig/loop/orchestrator.py` (`_restore_scopes_reduced`, `_write_encodings_reduced`), `sbmig/restructure/scope_restore.py`, `sbmig/restructure/source_encoding.py` |
| 보호 키, 가드, javax 차단, 제안 문장 | `sbmig/loop/fixloop.py` (`_framework_guard`, `_framework_javax_block`, `framework_owner_suggestion`, `framework_via_suggestions`) |
| 프롬프트 | `sbmig/fix/prompts/script_system_v1.md`, `sbmig/fix/context.py` |
| 보고서 절, 결과 줄 | `sbmig/report/markdown.py` (`framework_lines`, 한국어 안내서 `sbmig/report/guide_ko.py`가 같은 함수를 부름), `sbmig/report/result_line.py` (`run_scope`), 한국어 `sbmig/report/i18n.py` |
| 화면 | `sbmig/web/static/index.html` (`setFramework`, `SKIP_STEPS`, `fwBody`, `remBuildOnly`, `remUnconfirmed`, `remShort`), `sbmig/web/runs.py` (`framework_info`) |
| 조치의 `framework` 그룹 | `sbmig/remediations/inhouse.py` (`InhouseKeys`, `framework_patterns`) |

픽스처: `fixtures/projects/fw-pom-ms949`, `fixtures/projects/fw-direct`, `fixtures/projects/fw-parent`, `fixtures/projects/fw-transitive`, `fixtures/projects/fw-transitive-note`
(공개 대역 프레임워크 `fixtures/libs/example-fw`). 계약: `docs/INTERFACES.md`의 framework 절.

## 확인하는 법

```
grep -n "^def \|^FLAG" sbmig/analyze/framework.py
grep -n "def _check_framework" -A50 sbmig/loop/orchestrator.py | grep -n "journal\|log.info"
grep -n "framework_guard\|def _framework" sbmig/loop/fixloop.py
grep -n "framework:" -A30 config/default.yml
grep -n "def display_name" -A25 sbmig/core/config.py
.venv/bin/python -m pytest -o addopts="" -q tests/analyze/test_framework.py tests/plan/test_planner_framework.py tests/loop/test_fixloop_framework.py
```
