# 사내 프레임워크 프로젝트의 사내 규정 전환 (처리 방법 `convert`)

> 다루는 것: 사내 프레임워크 프로젝트를 Spring Boot 프로젝트로 새로 만드는 두 번째 처리 방법. 누가 고르는지, 전환이 하는 일, 이름이 어디서 오는지, `convert` 설정 키, 실행에 남는 것, 멈추는 경우와 할 일, 옮기지 않는 것, 증상별로 볼 곳, 고칠 자리.
> 언제 읽나: 플래그에 `framework-converted` 나 `aborted:convert-unavailable` / `aborted:convert-failed` 가 있을 때, 보고서의 「…를 사내 규정으로 전환했습니다」 절을 설명해야 할 때, 전환한 프로젝트가 빌드되지 않거나 화면이 404를 받을 때, 전환 규칙을 바꾸기 전에.
> 함께 읽을 파일: [13-framework-projects.md](13-framework-projects.md)(첫 번째 방법과 프레임워크 찾기), [06-company-rules-restructure.md](06-company-rules-restructure.md)(전환 뒤의 구조 정리), [03-run-debug-and-logs.md](03-run-debug-and-logs.md), [07-config-and-knowledge.md](07-config-and-knowledge.md)

전환 규칙 전체(파일 종류, activity 분류, 이름 규칙, 생성하는 클래스, 확인 항목)는 `docs/FW-CONVERT.md` 에 있다. 이 파일은
운영자가 실행을 읽고 진단하는 데 필요한 것만 쓴다. 프레임워크의 실제 이름은 저장소 어디에도 쓰지 않는다. 예시는 `Example`,
패키지는 `com.example` 이다.

## 1. 두 가지 처리 방법과 누가 고르나

| 값 | 화면 이름 | 하는 일 |
|---|---|---|
| `reduced` | 「JDK 21, Jakarta만 적용」 | 프레임워크를 그대로 두고 JDK 21 과 jakarta 만 적용한다([13](13-framework-projects.md)) |
| `convert` | 「사내 규정으로 전환」 | 프로젝트를 Spring Boot 프로젝트(부모 pom + boot / domain / service / store 모듈)로 새로 만들고, 그 결과를 보통 프로젝트처럼 끝까지 이관한다 |

정하는 곳은 셋이고 앞의 것이 이긴다.

1. 화면: 올리기 카드의 「<이름> 프레임워크 프로젝트 처리 방법」(이름이 없으면 「사내 프레임워크 프로젝트 처리 방법」). 프레임워크 검사가 줄인 범위로 설정되어 있고(`framework.artifacts` 가 있고 `framework.scope: jdk-jakarta`) 전환을 쓸 수 있을 때(`convert.enabled` 가 켜져 있고 프레임워크 이름이 있음)만 보인다(`GET /api/configs` 응답의 `framework` 안 `convert_available`). 취약점 해결 흐름을 고르면 숨는다. 고른 값은 `POST /api/runs?framework_mode=` 로 간다. Job 모드에서는 Job 환경 변수 `SBMIG_FRAMEWORK_MODE` 와 Worker 의 `--framework-mode` 로 이어진다.
2. CLI: `sbmig run <프로젝트> --framework-mode reduced|convert`. 빈 값은 주지 않은 것과 같다.
3. 서버 기본값: `framework.mode`(기본 `reduced`). 화면이 고르지 않은 요청과 CLI 에서 값을 주지 않은 실행이 쓴다. 이 값이 `convert` 인데 전환을 쓸 수 없는 서버(위 조건이 맞지 않음)라면, 아무것도 고르지 않은 업로드는 `reduced` 로 처리한다.

`convert` 는 흐름 `full` 에서만 뜻이 있다. 프레임워크 프로젝트가 아닌 업로드에서는 이 선택을 쓰지 않는다. 고른 값은
`report.json` 의 `framework_mode` 와 저널 `framework_mode` 에 남는다.

## 2. 전환이 하는 일 (접수 단계)

전환은 접수(intake)에서 돈다. as-is 스냅숏 커밋 바로 뒤, 빌드 도구 검사 전이다. 코드는 `sbmig/loop/orchestrator.py`
`_intake_convert`, 도우미는 `sbmig/loop/convert.py`, 변환기는 `sbmig/fwconvert/pipeline.py` `convert_project` 다.

1. 쓸 수 있는지 본다: `convert.enabled`, 프레임워크 이름(`framework.name` 또는 아티팩트에서 만든 이름), 이름의 출처(3절). 안 되면 멈춘다(6절).
2. `convert.versions` 에서 값이 빈 좌표는 저장소에서 최신 정식 릴리스를 찾는다(수정 루프의 버전 사전 시도와 같은 저장소 집합). 못 찾으면 멈춘다.
3. 규칙을 읽는다: `knowledge/fwconvert/rules.yml` 에 `convert.rules` 와 이름 설정을 덧씌운다.
4. 변환기를 실행한다. 원본 트리는 읽기만 하고, 결과는 `out/<run id>/convert/out/` 에 쓴다. 순서는 파일 분류 → 이름 정하기 → SQL 파일을 MyBatis mapper 로 → 서비스 XML 을 서비스·저장소·컨트롤러 클래스로 → 화면이 부르는 주소 읽기 → Java 생성 → DB 연결 정보 읽기 → 뼈대(pom 다섯 개, 애플리케이션 클래스, Web MVC 설정, `application.yml`) → 화면 파일 복사, JSP 경로와 ajax 주소 고치기 → 화면 컨트롤러 → 경계 검사(받는 컨트롤러 없는 ajax 호출, 겹치는 주소, 복사되지 않은 화면 파일).
5. 전환할 것이 없으면(서비스 XML 도 SQL 파일도 없음) 트리를 그대로 두고 `reduced` 처럼 계속한다(5절).
6. 전환했으면 작업 트리를 생성한 프로젝트로 바꾼다. `.git` 과 sbmig 의 실행 파일만 남고 as-is 파일은 모두 지워진다(as-is 는 첫 커밋에 남아 있다). 그리고 커밋 `convert: generated Spring Boot project` 를 만든다.

그 뒤는 보통 이관이다. 분석은 모듈이 여럿인 Spring Boot 프로젝트(`SPRING_BOOT`)를 보고, 계획은 `transform.rewrite`(jdk21 +
Spring Boot 4) → `restructure.boot_pom`(루트는 회사 부모와 필수 아티팩트, boot 모듈은 boot 부모, 나머지는 comp 부모) →
`restructure.review` → `verify.build` 와 수정 루프 → 마무리 → 이어지는 취약점 조치다. 트리에 프레임워크가 없으므로 범위
`jdk-jakarta` 도 프레임워크 플래그도 붙지 않는다. 루트 pom 이 없는 프레임워크 프로젝트도 이 방법으로는 받는다(생성한 트리에
pom 이 있다).

코드 변경 화면(`codediff/`)과 `changes.patch` 는 전환 커밋을 기준으로 비교한다. 그래서 생성한 파일 수천 개가 아니라 그 뒤
이관이 바꾼 것만 보인다.

## 3. 이름은 어디서 오나

- 루트 pom 이 있고 groupId(없으면 `<parent>` 의 groupId)와 artifactId 를 읽을 수 있으면 그것을 쓴다. 프로젝트 이름은 artifactId 의 camelCase(`my-shop` → `myShop`, `Project` 를 붙이지 않음), 모듈은 `myShop-boot` / `myShop-domain` / `myShop-service` / `myShop-store`, 패키지는 `<groupId>.myShop`, 생성 pom 의 groupId 는 그 groupId 다. 이때 `convert.package_root`, `convert.domains`, `convert.default_domain` 은 쓰지 않는다.
- 루트 pom 이 없거나 값을 못 읽으면 `convert.package_root` 와 실행의 프로젝트 이름(웹에서 입력한 이름, CLI 는 폴더 이름)을 쓴다. 이름에서 `naming.name_prefixes` 의 접두사(`sample-`, `demo-` …와 `convert.name_prefixes`)를 떼고 `Project` 를 붙이며, 이름이 `<코드>-` 로 시작하고 그 코드가 `convert.domains` 에 있으면 도메인으로 넣는다. `convert.package_root` 도 비어 있으면 멈춘다.

## 4. 설정 키 (`convert`)

| 키 | 기본 | 뜻 |
|---|---|---|
| `convert.enabled` | `true` | 끄면 이 방법을 쓸 수 없고 화면에 선택도 나오지 않는다 |
| `convert.package_root` | `""` | 루트 pom 이 없는 프로젝트의 패키지 앞부분 |
| `convert.domains` | `[]` | 루트 pom 이 없는 프로젝트에서 프로젝트 이름 앞의 도메인 코드 |
| `convert.default_domain` | `""` | 위에 맞는 코드가 없을 때의 도메인. 비우면 패키지에 도메인을 넣지 않는다 |
| `convert.name_prefixes` | `[]` | 프로젝트 이름에서 더 떼어 낼 접두사 |
| `convert.group_id` | `""` | 루트 pom 이 없을 때 생성 pom 의 groupId. 비우면 `convert.package_root` 에 도메인을 붙인 값 |
| `convert.version` | `"1.0.0"` | 생성 pom 의 version |
| `convert.versions` | MyBatis starter 하나, 값 `""` | 부모 pom 이 관리하지 않아 버전을 적어야 하는 의존성. 빈 값은 저장소의 최신 릴리스 |
| `convert.jdbc_drivers` | 벤더별 드라이버 좌표 | 찾은 DB 의 드라이버를 boot 모듈에 넣는다. 없는 벤더는 확인 항목 `[DATASOURCE_DRIVER]` |
| `convert.rules` | `{}` | `knowledge/fwconvert/rules.yml` 에 덧씌울 회사 값(회사 activity 클래스, 외부 호출 낱말 등, `docs/FW-CONVERT.md` 1절) |
| `framework.mode` | `reduced` | 화면·CLI 가 고르지 않았을 때의 처리 방법 |

회사 값은 회사 `config/artifacts.yml` 에 적고 이미지를 다시 굽는다. 생성 pom 에는 안내서의 버전을 쓰지 않는다. Spring Boot
버전은 sbmig 의 목표(`target.spring_boot_version`), Java 는 `target.java_version` 이다.

## 5. 실행에 남는 것

| 어디 | 무엇 |
|---|---|
| git | 두 번째 커밋 `convert: generated Spring Boot project`. 그 앞은 as-is, 그 뒤는 보통 이관의 커밋들 |
| 플래그 | `framework-converted`(주의 코드가 아니다. 결과 줄의 `attn=` 에 들어가지 않는다) |
| 저널 | `framework_mode`, `framework_converted {framework, project, package, modules, counts, queue, errors, datasource, commit, removed}`. 전환할 것이 없으면 `framework_convert_skipped`(reason `nothing to convert`), dry run 이면 같은 이벤트에 reason `dry_run` |
| 로그 | `접수: 사내 규정 전환 시작 (…)`, `접수: 사내 규정 전환 완료 - <프로젝트> (<패키지>), 모듈 …, 확인 항목 N건, 오류 N건` |
| 파일 | `out/<run id>/convert/queue.json`(확인 항목 전부, 원본 발췌 2,000자까지), `out/<run id>/convert/summary.json`(`report.json` 의 `conversion` 과 같은 내용) |
| 교체 중 표시 | 작업 트리를 바꾸는 동안에만 `out/<run id>/convert/swap.json` 이 있다. 원본은 지우지 않고 `out/<run id>/convert/asis/` 로 옮겨 두었다가, 커밋이 끝나면 그 폴더를 없앤다. 실패하면 원본을 되돌린다. 실행이 끝났는데 이 파일이 남아 있으면 교체 도중 프로세스가 죽은 것이다: `state` 가 `moving` 이나 `incomplete` 이고, 원본 파일은 `asis_dir` 에 있다 |
| `report.json` | `framework_mode`, `conversion`: `converted`, `framework`, `project`, `package`, `modules`, `commit`, `counts`, `datasource`(`configured:<벤더>`, `not_found`, `jndi`, `unknown_vendor`, `unresolved`), `queue`(발췌 없이 200건까지), `queue_total`, `unexposed`, `skipped`, `errors` |
| 한국어 보고서 | 머리 표 바로 아래 「<이름> 프레임워크 프로젝트를 사내 규정으로 전환했습니다」: 만든 프로젝트, 개수 표, 그 뒤에 실제로 한 단계(계획에 있던 단계만. 최종 빌드까지 갔으면 한 단계를, 그 빌드가 실패했으면 성공처럼 읽히지 않게 한 단계만 쓰고 결과는 머리 표에 맡긴다. 중간에 끝났으면 한 단계와 하지 않은 단계, 아무것도 못 했으면 그렇다고. `markdown.conversion_stages`), DB 연결, 「직접 확인해 주세요」(확인 항목을 종류별로 묶어 묶음마다 한 문장과 항목 20개까지), 「노출하지 않은 주소」(화면이 부르지 않아 뺀 주소), 「주소가 없는 서비스 기능」(`unexposed` 의 `no address: …` 줄: 서비스 클래스에는 있지만 주소가 없는 메서드와 그 이유), 「읽지 않은 파일」, 「전환 중 남은 오류」. 「확인해 주세요」의 첫 항목이 이 절을 가리킨다 |
| 영어 보고서 | 머리 표 아래 `## Framework conversion`(같은 사실, 이유 꼬리표별 묶음) |
| 화면 | 실행·결과 카드의 안내 「<이름> 프레임워크 프로젝트를 사내 규정의 Spring Boot 프로젝트로 전환합니다.」, 재구성·구조 검토 단계는 「하지 않음」으로 표시하지 않는다(실제로 돈다) |

전환할 것이 없던 실행은 보고서에 전환 절이 없고, 「확인해 주세요」에 「사내 규정 전환은 하지 않았습니다. 이 프로젝트에는
사내 프레임워크의 서비스 XML도 SQL 파일도 없어서 …」 한 줄이 나온다. 생성한 컨트롤러에 주소가 겹치면 「겹치는 주소」 한 줄이
「확인해 주세요」 앞쪽에 나온다(겹친 채로는 애플리케이션이 시작하지 않는다).

### 확인 항목의 종류 (보고서의 묶음)

이유 문장의 앞 꼬리표로 묶는다(`sbmig/report/guide_ko.py` `CONVERT_GROUPS`, `conversion_group`). 모르는 대괄호 꼬리표는 외부
호출 분류로 보고 꼬리표마다 묶음을 하나 만든다. 어느 모양에도 맞지 않으면 「그 밖의 확인 항목」이다.

| 꼬리표 | 보고서 묶음 | 사람이 할 일 |
|---|---|---|
| `[DATASOURCE]` | DB 연결 설정 | `spring.datasource` 값을 직접 넣는다 |
| `[DATASOURCE_PASSWORD]` | DB 비밀번호 | 실행 환경에 `SPRING_DATASOURCE_PASSWORD` 를 넣는다 |
| `[DATASOURCE_DRIVER]` | JDBC 드라이버 | boot 모듈 pom 에 드라이버를 넣거나 `convert.jdbc_drivers` 에 벤더를 더한다 |
| `[STUB_SPRING_LOGIC]` | 빈 구현으로 남긴 서비스 | router 만 있고 SQL 문이 없는 서비스를 직접 구현한다 |
| `[java_only]`, `[2b]`, `[3]` | Java 코드로만 동작하는 기능 / SQL 문을 알 수 없는 DB 기능 / 원본 클래스를 확인하지 못한 기능 | 빈 구현으로 남은 메서드를 원본 클래스를 보고 채운다 |
| `[AMBIGUOUS_SQL_KEY]` | 어느 SQL 문인지 정할 수 없는 기능 | `sql-key` 를 SQL 파일 둘 이상이 정의한다. 메서드에 SQL 문이 연결되지 않아 호출하면 오류가 난다. 쓸 SQL 문을 정해 연결한다 |
| `[NO_SQL_KEY]` | SQL 키가 없는 DB 기능 | 프레임워크의 DB activity 가 SQL 문을 적지 않았다. 메서드가 부를 것이 없어 호출하면 오류가 난다. 원본 서비스 정의를 보고 SQL 문을 연결한다 |
| `[UNKNOWN_SQL_KEY]` | SQL 문을 찾지 못한 기능 | `sql-key` 가 어느 SQL 파일과 mapper 의 SQL 문도 가리키지 않는다. 키를 확인하고 SQL 문을 연결한다 |
| 그 밖의 `[<꼬리표>]` | 외부 시스템 호출 | 호출 방식을 정해 구현한다(꼬리표는 `convert.rules` 의 외부 호출 분류) |
| `[CUSTOM_ACTIVITY]` | 프로젝트에서 만든 activity 클래스 | SQL 호출 하나로 바뀐 클래스의 빠진 로직을 확인한다 |
| `[BEAN_DIALECT]` | 전환하지 않은 서비스 XML | bean 형식 서비스를 직접 옮긴다 |
| `[ENDPOINT_DUPLICATE]`, `[ENDPOINT_VERB]` | 주소가 겹쳐 만들지 않은 기능 / 지원하지 않는 HTTP 메서드 | 필요하면 주소를 직접 더한다 |
| `[AJAX_NO_CONTROLLER]`, `[AJAX_UNMATCHED]` | 받는 컨트롤러가 없는 화면 호출 / 주소를 바꾸지 못한 스크립트 | 서비스 이름과 호출 주소를 고친다 |
| `[JSP_PATH]`, `FRONTEND_MISSING` | 경로를 고치지 못한 JSP / 복사되지 않은 화면 파일 | 빠진 파일과 경로를 확인한다 |
| `risk_score>=0.7` | 직접 검토가 필요한 클래스 | 리플렉션·스레드·배치 코드를 어떻게 옮길지 정한다 |

`MAVEN_BUILD` 묶음 문장도 있지만 지금 변환기는 이 꼬리표를 쓰지 않는다(생성한 프로젝트의 빌드는 이관의 빌드가 한다).

## 6. 멈추는 경우와 할 일

두 플래그 모두 접수에서 멈추고 종료 코드 3, 빌드는 하지 않는다. 두 거부 모두 전환할 프레임워크 파일(서비스 XML 이나 SQL 파일)이 있는 프로젝트에서만 생긴다. 전환할 것이 없는 업로드는 설정과 상관없이 손대지 않고 그대로 계속한다(5절). 중단 이유는 영어로 저장되고 보고서·화면에는 한국어로
나온다(`sbmig/report/i18n.py`).

| 플래그 | 중단 이유(앞부분 `framework conversion is not available:` / `framework conversion failed:`) | 할 일 |
|---|---|---|
| `aborted:convert-unavailable` | `convert.enabled is false` | `reduced` 로 다시 신청하거나 `convert.enabled` 를 켠다 |
| 〃 | `the framework has no name (…)` | `framework.name` 이나 `framework.artifacts` 를 설정한다 |
| 〃 | `the project has no root pom to take the package from and convert.package_root is not set` | `convert.package_root` 를 설정하거나 루트 pom 이 있는 형태로 다시 올린다 |
| 〃 | `no version for <g:a> (…)` | `convert.versions` 에 그 좌표의 버전을 적거나 저장소 설정을 확인한다 |
| 〃 | `the conversion rules could not be loaded (…)` | `knowledge/fwconvert/rules.yml` 과 `convert.rules` 의 YAML 을 확인한다 |
| `aborted:convert-failed` | `the output folder was refused (…)` | 변환기가 출력 폴더(`out/<run id>/convert/out/`)를 거부했다(파일이거나 원본 프로젝트 안에 있다). 실행 폴더가 프로젝트 폴더 안에 있지 않은지 본다 |
| 〃 | `the generated project has no poms (…)` | 뼈대 옵션 오류다(`skeleton: … no pom written`). 버전·groupId 설정을 본다 |
| 〃 | `<예외 종류>: <메시지>` | 변환기나 트리 교체의 예외다. `sbmig.log` 의 `framework conversion failed` 스택을 본다 |

`aborted:convert-failed` 는 끝에 `the project was left as uploaded`(as-is 커밋으로 되돌림) 또는 `the as-is tree could not be
restored` 가 붙는다. 뒤쪽이면 작업 트리를 믿지 말고 실행을 새로 시작한다.

## 7. 옮기지 않는 것

- 원본의 Java 클래스. 서비스·저장소·컨트롤러는 서비스 XML 과 SQL 파일에서 새로 만들고, 유틸·엔티티·프록시 같은 나머지 클래스와 그 어노테이션 매핑(`docs/FW-CONVERT.md` 3.8)은 아직 없다.
- 안내서 도구의 검증 게이트(LLM 검사 + 빌드 + 화면 파일 비교): 빌드는 이관의 빌드가 하고, 화면 파일 비교는 확인 항목 `FRONTEND_MISSING` 이 된다.
- 뼈대에서 만들지 않는 것: 예외 처리기, HTTP 메서드 필터, Gson 변환기, OpenAPI, 인증·요청 로그 인터셉터, H2 데모 설정과 H2 용 SQL 바꾸기.
- `docs/FW-CONVERT.md` 5절: 애플리케이션 시작·종료, 포트 검사, 데모 화면, 목록 값(LOV) 컨트롤러의 데모 열, 빌드 때의 mapper `resultType` 고치기.
- `docs/FW-CONVERT.md` 6절의 열린 결정: DB 를 못 찾은 경우(D3), 인증(D7, 모두 손님), 외부 호출과 배치 클래스(D8, D9, 확인 항목), 응답 JSON 모양(D10), 목록 값 컨트롤러(D13) 등. 이 값들은 회사와 정하기 전까지 지금 동작 그대로다.

## 8. 증상별로 볼 곳

| 증상 | 먼저 볼 것 | 보통 원인 |
|---|---|---|
| `framework_mode=convert` 인데 전환 커밋이 없다 | 저널 `framework_convert_skipped` 의 reason | `nothing to convert`(서비스 XML 이 `*-service.xml` 이 아니거나 SQL 파일 확장자가 `*.{fw}_sql` 이 아니다), `dry_run` |
| 화면에 처리 방법 선택이 없다 | `GET /api/configs` 응답의 `framework` 안 `configured`, `convert_available` | `framework.artifacts` 가 비었거나 `convert.enabled` 가 꺼졌거나 프레임워크 이름이 없다 |
| 패키지·모듈 이름이 이상하다 | 로그 `접수: 사내 규정 전환 시작 (…, 이름: …, 출처: …)` | 출처가 `root pom` 이면 루트 pom 의 groupId / artifactId, `convert.package_root` 면 3절 두 번째 규칙 |
| 애플리케이션이 시작하지 않는다(DataSource 설정 오류) | 보고서 「DB 연결」 줄, `conversion.datasource` | 원본에서 DB 를 못 찾았다(`not_found`, `jndi`, `unknown_vendor`, `unresolved`) |
| 애플리케이션이 시작하지 않는다(같은 매핑 두 번) | 「확인해 주세요」의 「겹치는 주소」, `conversion.errors` | 생성한 컨트롤러 두 개가 같은 주소를 받는다 |
| 화면이 404 를 받는다 | 「노출하지 않은 주소」, 확인 항목 `[AJAX_NO_CONTROLLER]` / `[AJAX_UNMATCHED]` | 화면이 부르지 않는다고 본 기능이거나, 호출 주소를 못 바꾼 스크립트 |
| 화면 파일이 없다 | 확인 항목 `FRONTEND_MISSING`, 「읽지 않은 파일」 | `WEB-INF` 안의 정적 파일(일부러 복사하지 않음), 이름 충돌 |
| 기능을 부르면 `UnsupportedOperationException` | 확인 항목 `[STUB_SPRING_LOGIC]`, `[java_only]`, `[2b]`, `[3]`, `[AMBIGUOUS_SQL_KEY]`, `[UNKNOWN_SQL_KEY]`, `[NO_SQL_KEY]`, 외부 호출 | 빈 구현으로 남긴 메서드다. `convert/queue.json` 의 `excerpt` 에 원본이 있다 |
| 빌드가 실패한다 | 보통 이관과 같다([03](03-run-debug-and-logs.md), [04](04-cases-scripts-kb.md)) | 생성 코드가 아니라 회사 부모·버전 쪽이 많다. 생성 코드 문제면 `sbmig/fwconvert/` 를 고친다 |
| 코드 변경 화면이 비어 있다 | `report.json` 의 `code_changes_base` | 기준이 전환 커밋이라 전환이 만든 파일은 보이지 않는다(정상) |

## 9. 고칠 자리

| 바꾸려는 것 | 파일 |
|---|---|
| 접수 순서, 이름의 출처, 멈추는 조건과 중단 이유 | `sbmig/loop/orchestrator.py`(`_intake_convert`, `_convert_unavailable`, `_convert_failed`), `sbmig/loop/convert.py` |
| 중단 이유·인수인계 줄의 한국어 | `sbmig/report/i18n.py`(테스트 `tests/report/test_convert_abort_reasons_ko.py`, `tests/report/test_convert_followups_ko.py`) |
| 보고서의 전환 절 | `sbmig/report/guide_ko.py`(`framework_conversion_lines`, `CONVERT_GROUPS`), `sbmig/report/markdown.py`(`framework_conversion_lines`). 둘 다 고친다 |
| 변환 단계 순서, 경계 검사, 개수 | `sbmig/fwconvert/pipeline.py` |
| pom·애플리케이션 클래스·`application.yml` | `sbmig/fwconvert/skeleton.py`, DB 연결 읽기는 `sbmig/fwconvert/datasource.py` |
| SQL 파일, 서비스 XML, 생성 클래스, 화면 복사, ajax | `sbmig/fwconvert/` 의 해당 모듈(`docs/FW-CONVERT.md` 8절 표) |
| 회사 낱말(activity 클래스, 외부 호출 낱말, 위험 점수) | 회사 yml 의 `convert.rules`(코드가 아니다) |
| 화면의 선택과 안내 | `sbmig/web/static/index.html`, `sbmig/web/runs.py`(`convert_available`), `sbmig/web/app.py` |
| 처리 방법 값의 전달(웹 → Job → Worker → CLI) | `sbmig/web/contract.py`, `sbmig/web/worker.py`, `sbmig/cli.py` |

규칙을 바꾸면 `docs/FW-CONVERT.md` 를 같이 고친다. 새 확인 항목 꼬리표를 만들면 보고서 묶음(`CONVERT_GROUPS`)에도 한 줄을 더한다.
없으면 외부 호출로 묶인다.

## 확인하는 법

```
grep -n "def _intake_convert" -A30 sbmig/loop/orchestrator.py           # 접수 순서와 이름의 출처
grep -n "PREFIX\|_LINE\|FLAG_" sbmig/loop/convert.py                    # 플래그, 중단 이유와 인수인계 줄의 원문
grep -n "^convert:" -A32 config/default.yml                             # 설정 키와 한국어 설명
grep -n "CONVERT_GROUPS" -A3 sbmig/report/guide_ko.py                   # 보고서 묶음
grep -n "framework conversion" sbmig/report/i18n.py                     # 한국어
.venv/bin/python -m pytest tests/fwconvert tests/loop/test_intake_convert.py tests/report/test_framework_conversion_report.py -q
```
