# 모듈이 하나인 Spring Boot 앱 나누기

> 다루는 것: 루트 pom 이 곧 앱인 Spring Boot 프로젝트를 회사 모듈 셋으로 나누는 규칙. 언제 적용되는지, 모듈마다 무엇이 가는지, 테스트·의존성, 인수인계 세 줄, 기록, 알려진 한계, `wrap`.
> 언제 읽나: Boot 프로젝트의 결과 모듈 구조를 설명해야 할 때, 나눈 뒤 앱이 뜨지 않거나 화면 파일이 안 보일 때, 이 규칙을 바꾸기 전에.
> 함께 읽을 파일: [06-company-rules-restructure.md](06-company-rules-restructure.md)(3절 모듈 셋, 4절 Boot as-is), [02-flow-and-nodes.md](02-flow-and-nodes.md), [07-config-and-knowledge.md](07-config-and-knowledge.md)

## 1. 언제 적용되나

세 조건이 모두 맞을 때다.

- 유형이 `spring_boot` 이고 `layout.boot_as_is: in-place`(기본).
- 루트 pom 이 앱 자체다: `<modules>` 가 없고 소스가 있다.
- `layout.boot_single_module: split`(기본). `wrap` 이면 예전처럼 소스와 자원을 통째로 `{name}-boot` 하나에 넣는다(6절).

모듈이 여럿인 Boot 프로젝트와 MSA 는 이 값과 상관없이 디렉토리를 그대로 두고 pom 만 바꾼다([06](06-company-rules-restructure.md) 4절).

계획(`sbmig/plan/planner.py` `boot_single_split`)은 이때 `restructure.boot_pom` 대신 `restructure.apply` 를 넣는다(`params`
`{"mode": "layout", "starter": "auto", "boot_single": True}`, `starter` 는 `layout.starter_by_case` 의 `spring_boot` 값). 계획 노트는
`restructure: rule boot-single-module=split (layout.boot_as_is=in-place, single-module Boot app split like the other cases) → layout split into …`.
`boot_pom` 모드로 들어와도 `Restructurer._boot_single_split` 이 같은 판정을 하고 `layout` 으로 넘긴다(`_Ctx.boot_split`).

## 2. 모듈마다 가는 것

모듈 이름과 부모는 비 Boot 프로젝트와 같다([06](06-company-rules-restructure.md) 3절): `layout.boot_module` / `layout.comp_module` /
`layout.front_module`, boot 모듈은 `artifacts.boot_parent`, 나머지 둘은 `artifacts.comp_parent`, 루트는 `artifacts.parent` 를 상속하는 aggregator.

| 모듈 | 내용 |
|---|---|
| boot (`{name}-boot`) | `@SpringBootApplication` 클래스와 그 클래스를 이름으로 쓰는 소스(그 소스를 쓰는 소스까지 이어서; 소스 모듈은 boot 모듈을 참조할 수 없다). Boot 설정은 `layout.boot_resources_glob`(`application*.yml` / `.yaml` / `.properties`, `bootstrap*`, `logback*.xml`, `log4j2*.xml`, `banner.txt`). Boot 플러그인과 패키징. 소스·front 모듈에 의존한다 |
| 소스 (`comp_module`) | 나머지 Java 소스, 코드가 읽는 나머지 리소스(mapper XML, 메시지, SQL …), 프로젝트의 의존성 |
| front (`front_module`) | 그런 파일이 있을 때만. `src/main/resources` 의 `static`, `public`, `resources`, `META-INF/resources`, `templates` 는 front 의 같은 경로로, `src/main/webapp` 웹 루트의 정적 파일은 `layout.front_static_dir` 로 간다 — Boot 가 classpath 의 jar 에서 서빙·해석하는 자리라 URL 과 뷰 이름이 그대로다 |

- JSP 는 웹 경로를 지킨다: `WEB-INF` 아래 JSP 도 front jar 의 `META-INF/resources/WEB-INF/…` 같은 경로로 간다(`layout.front_jsp_dir`
  을 쓰지 않음). 그래서 앱이 이미 가진 `spring.mvc.view.prefix` 가 그대로 맞고 재구성은 뷰 설정을 고치지 않는다. JSP 가 있으면 boot 모듈은
  실행 가능한 WAR(`layout.jsp_boot_packaging: war`).
- `layout.front_module: null`: front 를 만들지 않고 웹 루트와 위 classpath 폴더를 as-is 와 같은 경로로 boot 모듈에 둔다. 웹 루트에 JSP 가
  있거나(`jsp_boot_packaging: war`) as-is 가 WAR 이면 boot 모듈은 실행 가능한 WAR. 노트
  `boot split: no front module (layout.front_module: null) — … moved into <boot> at the as-is paths`.
- 테스트는 시험하는 클래스를 따라간다. 앱 컨텍스트가 필요한 테스트(`@SpringBootTest` 와 `@WebMvcTest`, `@DataJpaTest` 같은 슬라이스
  테스트 — 패키지에서 애플리케이션 클래스를 찾는다)와 boot 로 옮긴 클래스를 이름으로 쓰는 테스트는 boot 모듈로(Boot 테스트 리소스와 함께),
  나머지는 소스 모듈에. 노트 `boot split: N test(s) that need the application context moved to …`.

## 3. 인수인계 세 줄

영어 원문은 `Restructurer._boot_split_moves` / `_boot_split_checks`, 한국어는 `sbmig/report/i18n.py`(원문을 바꾸면 둘 다).

| 언제 | 한국어(요지) | 사람이 확인할 것 |
|---|---|---|
| 애플리케이션 클래스를 쓰는 소스를 boot 로 옮겼을 때 | 「애플리케이션 클래스(…)를 사용하는 클래스 N개를 함께 … 모듈로 옮겼습니다(…). … 모듈은 boot 모듈을 참조할 수 없기 때문입니다. 이 클래스들이 그 모듈에 있어도 되는지 확인해 주세요.」 | 옮긴 클래스가 boot 에 있어도 되는지 |
| 애플리케이션 클래스에 명시 스캔(`scanBasePackages`, `scanBasePackageClasses`, `@ComponentScan`)이 없고 소스 모듈에 그 패키지 밖의 패키지가 있을 때 | 「… 모듈의 패키지 N개(…)는 애플리케이션 클래스(…)가 있는 … 패키지 밖에 있어 기본 컴포넌트 스캔에 들어가지 않습니다. 나누기 전에도 스캔 범위는 같았으니, 이 클래스들을 어떻게 빈으로 등록했는지와 지금도 그대로 등록되는지 확인해 주세요.」 | 그 빈이 어떻게 등록됐는지(나누기는 스캔을 바꾸지 않는다; 추측해서 고치지 않는다) |
| 웹 파일을 옮겼는데 소스가 `getRealPath(` 나 `"src/main/webapp` 로 읽을 때 | 「소스 N개가 웹 파일을 파일 경로로 읽습니다(…). 웹 파일은 이제 … jar 안에 있어 그런 경로가 없으니 클래스패스 자원으로 읽도록 바꿔 주세요.」 | 그 코드를 classpath 자원 읽기로 바꾸기 |

## 4. 기록

- 저널 `restructure` 의 모드 `layout`, 단계 `restructure.apply`. 커밋 `restructure: comp-jar / boot-jar layout`(옮긴 파일은 코드 변경 기록에 원래 위치와 함께).
- 요약 노트 `boot split: single-module Spring Boot app split into <모듈들> (layout.boot_single_module: split) — the application class and the Boot configuration in <boot>, every other source in <소스>`(front 를 만들었으면 `, static files / JSPs / templates in <front>`).
- 구조 검토 입력(`sbmig/review/inputs.py`)은 이 경우를 비 Boot 분할과 같은 구조로 설명한다.
- 다시 돌려도 결과가 같다: 이미 나뉜 트리는 노트 `already restructured` 와 함께 그대로 둔다.

## 5. 알려진 한계

1. 분석은 `resources` / `META-INF/resources` 를 웹 파일로 세지 않는다. 그래서 계획 노트가 front 모듈이 없다고 해도 재구성이 front 를 만들 수 있다.
2. 소스 모듈의 테스트가 front 로 옮긴 파일을 classpath 로 읽으면 그 테스트에서는 보이지 않는다(테스트는 빌드에서 건너뛴다).
3. `@Import` 나 `spring.factories` 로 짠 스캔은 보지 않는다. 명시 스캔으로 치는 것은 `scanBasePackages` / `scanBasePackageClasses` / `@ComponentScan` 뿐이다.
4. `front_module: null` 과 WAR 의 조합은 실제 Maven 으로 빌드해 보지 않았다.
5. `layout.boot_as_is: layout` 에 `front_module: null` 이면 정적 파일이 소스 모듈에 남는다.

## 6. `wrap` 과 fixture

- `layout.boot_single_module: wrap`: 계획은 `restructure.boot_pom`, 노트 `… single-module Boot app → wrapped into <boot> (layout.boot_single_module=wrap)`, 모든 소스와 자원이 `{name}-boot` 하나에 들어간다.
- fixture: `fixtures/projects/company-boot-single/`(나누기: back / front / boot), `fixtures/projects/company-boot-wrap/`(`wrap`),
  `fixtures/projects/boot-asis-parent-unpublished/`(나누기). 단위 테스트 `tests/restructure/test_boot_single_split.py`.

## 확인하는 법

```
grep -n "boot_single_module" -B8 -A2 config/default.yml                 # 키와 설명
grep -n "def boot_single_split" -A10 sbmig/plan/planner.py               # 계획 판정
grep -n "boot split:" sbmig/restructure/restructurer.py                  # 노트·인수인계 원문
grep -n "^BOOT_WEB_RESOURCE_DIRS\|^BOOT_CONTEXT_TEST_RE\|^EXPLICIT_SCAN_RE\|^REAL_PATH_RE" sbmig/restructure/restructurer.py
grep -n "boot split" sbmig/report/i18n.py                                # 한국어
.venv/bin/python -m pytest tests/restructure/test_boot_single_split.py -q
```
