# 회사 규칙과 재구성

> 다루는 것: 회사 레이아웃·사내 부모·필수 / 선택 아티팩트·버전 정책·동봉 jar·javax 시절 라이브러리·Ant 접수·루트 없는 트리·ADF·속 빈 리액터·pom 복사본·JSP jakarta·필터링 바이너리·테스트 건너뛰기를 코드가 어떻게 구현하는지. 규칙마다 무엇 / 왜 / 어디 / 설정 키 / 보고서에 남는 것.
> 언제 읽나: 결과 pom·모듈 구조가 왜 그렇게 나왔는지 설명해야 할 때, 회사 규칙을 바꾸거나 새 규칙을 넣기 전에.
> 함께 읽을 파일: [02-flow-and-nodes.md](02-flow-and-nodes.md), [07-config-and-knowledge.md](07-config-and-knowledge.md), [04-cases-scripts-kb.md](04-cases-scripts-kb.md), [../docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md)

## 0. 읽기 전에

- 회사의 실제 좌표는 이 저장소에 없다. 아래 좌표는 모두 `config/artifacts.example.yml` 의 **자리표시자**다:
  루트 부모 `com.company.platform:base-parent`, boot 모듈 부모 `com.company.platform:boot-jar`, 그 밖 모듈 부모
  `com.company.platform:comp-jar`, 필수 `com.company.platform:company-core` / `com.company.platform:company-logging`,
  선택 `com.company.lib:lib-auth` … `com.company.lib:lib-web`(8개), 버전 줄 4.0.0. 회사는 이 파일을 복사해 `config/artifacts.yml`
  을 만들고 `--config` 로 넘긴다. 좌표를 코드에 넣지 않는다(불변조건 2).
- 재구성은 **LLM 없이 결정적**이다. 본체는 `sbmig/restructure/restructurer.py` 의 `Restructurer.apply(ws, profile, cfg, mode=…)`,
  pom 편집은 서식을 지키는 `sbmig/restructure/pom_editor.py`. 다시 돌려도 결과가 같고(이미 재구성된 프로젝트는 pom 정리만),
  프로젝트 밖 경로는 읽지도 바꾸지도 않는다. 실패하면 `ok=False` + 플래그 `restructure-failed`(성공 아님).
- 보고서의 "Handover"(인수인계) 절이 규칙의 결과를 사람에게 말한다. 문구는 `sbmig/report/handover.py`, 번역은 `sbmig/report/i18n.py`.

## 1. 루트 pom → `base-parent`, aggregator

| 항목 | 내용 |
|---|---|
| 무엇 | 이관된 루트 pom 은 `packaging pom` 집계 pom 이 되고 부모는 `artifacts.parent`(`base-parent`) |
| 왜 | 회사 레이아웃: 모든 루트 pom 이 사내 base-parent 를 상속한다 |
| 어디 | `Restructurer._layout` / `_boot_pom` / `_msa`(+ `_msa_aggregator`) |
| 키 | `artifacts.parent`, `artifacts.parent_manages_java_version`(false 면 `maven.compiler.release` 를 적음) |
| 보고서 | "What we changed" 의 부모 변경. 부모가 저장소에서 안 풀리면 `aborted:parent-not-found`(exit 3, 설정·Nexus 문제) |

사전 점검이 `artifacts.parent` 가 비었거나 `CHANGE` 자리표시가 남아 있으면 "artifacts.parent is not configured: pass the company
artifacts yml with --config" 후속 조치를 남긴다.

## 2. 필수 / 선택 사내 아티팩트

| 항목 | 내용 |
|---|---|
| 필수 | `artifacts.mandatory` 2개를 `artifacts.mandatory_target: root` 에 따라 모든 루트 pom 에 선언(MSA 는 서비스 루트마다 + 맨 위 집계 pom; `modules` 는 예전 방식). `version: null` = 부모가 관리 — sbmig 는 버전을 지어내지 않는다 |
| 선택 | `artifacts.optional` 8개는 **이미 쓰는 것만** 버전을 맞춘다(별칭 `aliases` → 정식 이름, `property_name` 속성 경유 포함). `artifacts.optional_version_policy`: `floor`(기본, 내리지 않음) / `pin` |
| 트리거 | 선택 아티팩트에 `when_present`(g:a 또는 glob)가 있으면 그 의존성을 선언한 pom 에 **새로 추가**. `replaces: true`(기본)면 찾은 원래 선언을 지운다 — 회사가 다듬은 아티팩트가 원래 라이브러리를 대신(예: `hibernate-core` → 사내 JPA 스타터) |
| 지우기 | `artifacts.remove_when_present` 의 g:a 는 어느 pom 에 있든 지운다(옛 사내 BOM 등) |
| 한 버전 줄 | 사내 아티팩트는 모두 한 버전 줄(예: 4.0.0)을 쓴다. 옛 이관 도구가 남긴 옛 `boot-jar` / `comp-jar` 버전도 설정의 버전으로 옮긴다 |
| 어디 | `Restructurer` 의 모든 모드(선택 아티팩트 처리와 `remove_when_present` 는 모든 pom 에), 판정 `ArtifactSpec.triggered_by`(`sbmig/core/config.py`) |
| 보고서 | 재구성 노트 `mandatory added: … ; optional bumped: …`, 저널 `restructure` 의 `mandatory_added` / `optional_bumped` / `optional_added` / `optional_replaced`, `optional_triggers` |

## 3. 비 Boot 프로젝트의 분할: `{name}-boot` / `{name}-back` / `{name}-front`

`pure_java`, `spring_legacy`, `servlet_tomcat` 은 모드 `layout`(단계 `restructure.apply`).

| 모듈 | 부모 | 내용 | 키 |
|---|---|---|---|
| `{name}-boot` | `artifacts.boot_parent` | Boot 설정만: 생성한 `Application` 클래스, boot 리소스, spring-boot-maven-plugin repackage | `layout.boot_module`, `layout.application_class_name`, `layout.starter_by_case` |
| `{name}-back` | `artifacts.comp_parent` | as-is 의 **모든** 소스(컨트롤러 포함)와 의존성 | `layout.comp_module`, `layout.split_policy`(`minimal` 기본; `web-layer` 는 컨트롤러·설정 클래스를 boot 로) |
| `{name}-front` | `artifacts.comp_parent` | as-is 에 웹 리소스가 있을 때만: 정적 파일 → `src/main/resources/static`, JSP → `src/main/resources/META-INF/resources/WEB-INF/jsp`(Servlet 3) | `layout.front_module`(null = 안 만듦), `layout.front_static_dir`, `layout.front_jsp_dir`, `layout.front_copy_resource_dirs` |

- `{name}` = 루트 artifactId(`layout.project_name` 으로 덮어씀). 모듈은 루트 바로 아래(`layout.module_dir` 빈 값). artifactId 는
  `<root>-boot` / `<root>-back` / `<root>-front`(`layout.boot_artifact_suffix`, `layout.comp_artifact_suffix`).
- **JSP 가 있으면** boot 모듈은 **실행 가능한 WAR**(`layout.jsp_boot_packaging: war`): `java -jar` 가 내장 Tomcat 을 띄우고(이관의 목표),
  같은 파일을 외장 Tomcat 에 배포할 수도 있다. 실행 가능한 jar 에서는 JSP 를 서빙할 수 없기 때문이다. JSP 가 없으면 실행 가능한 jar.
- as-is 다중 모듈: `layout.multi_module_policy: merge`(기본) = 모든 모듈 소스를 back 으로 접는다(boot 모듈 이름과 같은 as-is 모듈은
  boot 모듈로 유지). 같은 경로가 여러 모듈에 있으면 `layout.merge_collision: first-wins` — 버려진 사본은 실행 디렉토리
  `merge-conflicts/` 에 두고 인수인계에 적으며, 수정 프롬프트에는 읽기 전용 참고로만 보인다. `wrap` = 모듈을 그대로 두고 boot 를 더함.
- web.xml 의 서블릿·필터·리스너·오류 페이지·세션 시간 → 생성 클래스 `WebXmlRegistrations`(`layout.web_xml_registrations: generate`,
  `sbmig/restructure/webxml_registrations.py`, 규칙 표 `knowledge/config/web_xml_rules.yml`). 플래그 `web-xml-registered`.
- Spring XML 컨텍스트를 Boot 로 옮길 때 기동에서 깨지는 지점(빈 덮어쓰기, 순환 참조, 멀티파트, 기본 서블릿, `/WEB-INF/…` 위치 …)은
  `layout.legacy_context` 의 스위치들이 다룬다(`sbmig/restructure/legacy_context.py`, `sbmig/restructure/legacy_apply.py`). 꺼진 스위치는
  인수인계 문장만 남긴다.

## 4. Boot as-is 와 MSA: pom 만

| 경우 | 규칙 | 어디 / 키 |
|---|---|---|
| Spring Boot 2/3 (`spring_boot`) | 디렉토리 그대로(`layout.boot_as_is: in-place` 기본). 루트 → `base-parent`, 이름이 `layout.boot_module_pattern`(`*-boot`)에 맞고 pom 이 Boot 앱인 모듈 → `boot-jar`, 나머지 모듈 → `comp-jar`. 단일 모듈 Boot 앱은 `{name}-boot` 로 감싼다. 옛 사내 도구로 이관된 프로젝트도 같은 규칙 | 모드 `boot_pom`, `Restructurer._boot_pom` |
| MSA (`msa`) | 서비스마다 pom 만(부모, 필수 / 선택, BOM, java 속성). 디렉토리 구조는 손대지 않음. 서비스 루트 → 역할 부모, 공유 라이브러리 루트 → `comp-jar`(필수 아티팩트 없음). `layout.msa_parent_policy: per-service-root`(기본) / `top-aggregator` | 모드 `msa_pom`, `Restructurer._msa`, 라이브러리 판정 `is_library_root`(`sbmig/analyze/classifier.py`) |
| MSA 의 비 Boot WAR 서비스 | pom 만 바뀌고 Boot 앱 생성·레이아웃 분할은 없다(현업: "비-boot war 는 일단 그대로 두자") | 같은 `msa_pom` |
| 소스 없는 pom 전용 프로젝트 | 단계 `restructure.pom_only`(모드 `msa_pom`) | `sbmig/plan/planner.py` |

pre-9c 레이아웃(`module/boot-jar` + `module/comp-jar`, front 없음, Boot 프로젝트도 분할)은 `layout.module_dir: module`,
`layout.boot_module: boot-jar`, `layout.comp_module: comp-jar`, `layout.front_module: null` 로 고를 수 있다 — 옛 fixture 를 위해
`config/artifacts.local-e2e.yml` 이 이렇게 고정한다.

## 5. 버전 정책 — 버전은 추측하지 않는다

현업 결정: "정책상 버전을 추정하여 추가할 수 없습니다" — LLM 도, sbmig 도 버전을 짓지 않는다. 사실(부모 체인·BOM·as-is 빌드·저장소)만 쓴다.

**결정 순서**(`VersionResolver`, `sbmig/loop/fixloop.py`; 재구성과 수정 루프가 같은 규칙):

```
버전 없는 <dependency> g:a
 (1)  새 부모 체인이 관리?            -> 버전을 적지 않음 ("managed")
 (1b) 아니고 목표 Boot BOM 이 관리?   -> 그 버전 ("boot-bom"), 단 as-is 가 더 새것이면 as-is (내리지 않음)
 (2)  as-is 빌드가 쓴 버전?           -> 그 버전 ("asis", logs/effective-depmgmt.json)
 (3)  LLM 이 방금 더한 의존성 / 누락 버전 케이스일 때만 -> 저장소의 최신 정식 릴리스 ("repository")
 (4)  모름 -> 비워 둠 ("unknown")
```

재구성 안의 pom 정리 순서(모든 pom 모드 공통, `Restructurer` 의 연속 호출):

| 단계 | 메서드 | 무엇 | 키 |
|---|---|---|---|
| 1 | `_non_filtered_extensions` | 필터링되는 리소스의 바이너리 확장자를 `nonFilteredFileExtensions` 에(§9) | `build.non_filtered_extensions` |
| 2 | `_pin_asis_versions` | 새 부모로 옮기며 버전을 잃은 의존성에 as-is 빌드의 버전을 적음 → 인수인계 ⚠ "carried over" | — |
| 3 | `_drop_managed_versions` | 새 부모 체인이 같거나 더 새 버전으로 관리하는 명시 버전을 지움. 사내·필수·선택·system scope·자기 모듈은 건드리지 않음 | `layout.drop_managed_versions`(`gte` 기본 / `any` / `off`) |
| 4 | `_drop_bom_property_overrides` | 부모 체인이 정의하는 버전 속성(`spring-boot.version=2.5.15` …)을 프로젝트 pom 에서 지움 — Maven 속성 상속으로 부모 값을 덮기 때문. 아직 `${이름}` 으로 참조되면 남기고 메모 | `layout.drop_bom_property_overrides`, `layout.parent_property_names`(비상구, 기본 빈 목록) |
| 5 | `_drop_orphan_version_properties` | 이름과 상관없이 값이 버전·Java 수준 모양이고 어느 pom 도 참조하지 않는 속성을 지움(`java-version=1.8`, `org.springSecurity-version=4.2.7.RELEASE` 같은 철자). 부모 체인 사실을 읽었을 때만 | 같은 스위치 |
| 6 | `_wire_bundled_jars` | 동봉 jar 연결(§6) | `layout.wire_bundled_jars` |

그 밖의 규칙:

- **부모 체인 사실**(`sbmig/build/parent_chain.py`, `load_parent_chain_facts`): 설정한 사내 부모 pom 을 저장소에서 읽어 `<parent>` 와
  BOM import 를 따라 관리 버전·속성 이름을 모은다. 재구성 뒤 `help:effective-pom` 캡처가 실패하면 이것이 대체 출처다. 저널 `parent_chain_facts`.
- **`artifacts.parent_manages_spring_boot: auto`**(기본): 체인이 `spring-boot-dependencies` 에 닿으면(상속·import) true, 아니면 false 로 보고
  재구성이 Boot BOM 을 직접 import 한다(버전 자리에는 체인이 정의한 속성 이름 — `layout.bom_version_properties` — 이 있으면 그것, 없으면
  값). 체인을 못 읽으면 true. 명시한 `true` 인데 체인에 Boot BOM 이 없으면 플래그 `parent-chain-no-boot-bom` + 인수인계 맨 앞 줄(그 실행의
  고정은 목표 Boot BOM 을 따랐다는 문장). Boot BOM 사실은 `sbmig/build/boot_bom.py`, Spring Cloud 트레인 짝은 `sbmig/build/cloud_bom.py` +
  `knowledge/config/spring_cloud_bom.yml`.
- **자기 `<dependencyManagement>` 항목**: as-is 의 관리 항목이 역할 부모 모듈에 복사되면 import 한 Boot BOM 을 덮는다(자기 항목이 이김 —
  `spring-security-*:4.2.7` 이 7.x 를 덮음). 그래서 체인·import BOM 이 같은 g:a 를 같거나 더 새 줄로 관리하는 자기 항목은 지운다
  (`_drop_managed_management_entries`). BOM import, 자기 모듈, 보호 좌표, system, 관리 버전보다 새 것은 남긴다.
- **구조 검토의 짝 규칙**(`sbmig/review/prompts/review_v1.md`): "Spring Boot artifact on another line", "property shadows the parent chain",
  그리고 프로젝트 pom 이 Spring 계열(`<dependencies>` 이든 자기 `<dependencyManagement>` 이든)을 체인과 다른 줄로 고정한 것은 **critical**.
- 수정 루프 쪽: LLM 이 더한 버전 없는 의존성은 커밋 전에 채우고, `missing_version` 케이스는 LLM 전에 사전 시도 `sbmig-version` 이 고친다.
  LLM 이 메이저를 올리거나 라이브러리를 바꾸면 되돌리지 않고 "운영자 확인 필요"(`SBMIG-RESULT … major=<n>`)로 남긴다. 옛 Spring
  아티팩트(spring-dao 등)는 버전 고정이 아니라 제거한다(`knowledge/notes/spring-removed-artifacts.md`).

## 6. 동봉 jar (bundled jar)

| 항목 | 내용 |
|---|---|
| 무엇 | 트리 안에 있지만 어떤 pom 도 선언하지 않은 jar(`WEB-INF/lib` 의 벤더 암호화 / SSO jar 등). IDE 빌드는 봤고 Maven 은 모른다 |
| 스캔 | 분석 단계, 폴더 규칙이 아니라 내용 기반(`sbmig/analyze/bundled_jars.py`, `scan_bundled_jars`; 좌표는 jar 의 pom.properties → `knowledge/libraries/jar_names.yml` + KB 의 `jar_names.yml` → sha1 저장소 조회). 키 `intake.bundled_jars`. 플래그 `bundled-jars`, 저널 `bundled_jars`, `logs/bundled-jars.json` |
| 판정 | `sbmig/restructure/bundled_jars.py` 의 `plan_bundled_jars`, jar 마다 하나: `superseded`(목표 부모 체인 / Boot BOM 이 그 g:a 를 관리 — 옛 spring-webmvc 4.x 같은 것, 연결하지 않음) · `container`(서블릿 / JSP API 사본, Tomcat 이 줌) · `unused`(소스·JSP·XML·properties 어디서도 참조 안 함 — 인수인계에 이름만) · `declared`(대상 pom 이 이미 선언) · `compile`(좌표를 알고 저장소에 있음 → 보통 의존성) · `system`(좌표 모름 / 저장소에 없음) · `skip`(`intake.unknown_jar: skip` 또는 연결 오류) |
| 연결 | **첫 빌드 전에** 재구성이 연결한다(`layout.wire_bundled_jars`, 쓰기는 `wire_bundled_jar` in `sbmig/restructure/system_jars.py`): `system` 이면 jar 를 `<module>/lib/<name>` 으로 복사하고 `${project.basedir}/lib/<name>` system scope 로 선언 + boot 모듈 spring-boot-maven-plugin 에 `includeSystemScope`. 좌표는 추측하지 않고 자리표시 좌표(프로젝트 groupId + 파일 stem, 버전 = 파일명 버전 또는 "0") |
| 왜 재구성에서 | 런타임 전용 jar(JDBC 드라이버, web.xml 의 필터, taglib 구현)는 컴파일 오류를 내지 않으므로 빌드 오류를 기다리면 영영 연결되지 않는다 |
| 안전망 | 빌드 오류가 그 jar 의 패키지를 필요로 하면 수정 루프의 사전 시도 `sbmig-bundled`(LLM 0회). LLM 에게는 "the project bundles `<jar>` which provides package `<p>`" 사실을 주고, 동봉 jar 가 패키지를 주는데도 좌표·라이브러리 없음으로 거절하면 그 거절은 기각된다(저널 `decline_rejected`) |
| 금지 | **찾은 jar 를 전부 연결하는 것은 안티패턴**(현업 지적). `superseded` jar 는 절대 연결하지 않고, LLM 에게도 다시 넣지 말라고 한다 |
| 보고서 | "Bundled jars" 절(구조 검토 바로 뒤)의 jar 별 단계·상태, 인수인계 ⚠ "Nexus 에 올리고 system 항목을 바꿔 달라", 저널 `bundled_jar_wired`(stage restructure / fix), `bundled_jars_skipped` |
| fixture | `fixtures/projects/webapp-bundled-jar/` |

## 7. javax 시절 라이브러리와 jakarta 빌드 찾기

| 항목 | 내용 |
|---|---|
| 무엇 | 아직 `javax.*` 를 드러내는 의존성(`external_javax_api` 케이스)의 jakarta 빌드를 sbmig 가 직접 찾아 LLM 전에 교체(사전 시도 `sbmig-jakarta`) |
| 찾는 곳 | 저장소 집합: `build.repository_url`(있으면 먼저) → settings 의 모든 미러(Maven 의 `mirrorOf` 규칙) → 활성 프로필 저장소(`<pluginRepositories>` 포함 — 빌드 확장은 Maven 이 여기서 받는다) → 미러가 덮지 않는 pom 의 `<repositories>` → 전역 settings → Central(온라인일 때) |
| 순서 | 표의 `jakarta_key` / classifier(`knowledge/libraries/jakarta_libraries.yml`, 회사 표 `libraries.company_file`) → 같은 좌표의 상위 릴리스(`2.0.5-jakarta`) → Nexus 3 검색 API(`libraries.nexus_search`) → 형제 이름 탐침(`-jakarta`, 숫자 올리기; `libraries.max_jakarta_probes`) |
| 확인 | 버전 한정어·classifier 가 jakarta → pom 이 jakarta.* 선언 → 상한 있는 jar 스캔(`libraries.max_jar_scans`, `libraries.max_jar_scan_mb`). 확인 안 된 후보는 보고만, 적용하지 않음 |
| 메이저 | 상위 메이저로의 교체는 이것만이 정당한 메이저 변경이고 운영자 확인 ⚠ 로 남는다 |
| 어디 | `sbmig/build/jakarta_discovery.py`(`discover_jakarta_builds`), 저장소 조회 `sbmig/build/repo_versions.py` |
| 키 | `libraries.discover_jakarta_builds`(false = 끄기: 릴리스 목록만, 사전 시도 없음), `libraries.lookup`, `build.repository_server_id`, `build.settings_file` |
| 진단 | `sbmig repo-probe g:a:v` — 물어본 URL 과 각각의 응답(ok / 404 / 401 / timeout), 찾은 버전, jakarta 판정, 형제 탐색; 마지막 줄 `REPO-PROBE ok|none …`. 저널 `library_lookup` / `library_mapped` / `library_unmapped`; 못 읽은 원격은 `remote repositories unreadable: …` |
| 권고 | 막힌 javax 항목이 EOL 권고(`knowledge/libraries/advisories.yml`)에 걸리면 그 권고와 대체물을 함께 말한다. 동봉 jar 로 들어온 라이브러리도 권고에 맞춰 본다(spring-mobile → `knowledge/notes/spring-mobile.md`: LLM 이 호환 대체 코드를 쓴다, 바이트코드 변환 금지) |

## 8. 접수에서의 트리 모양 규칙

| 규칙 | 무엇 / 왜 | 어디 | 키 | 보고서 |
|---|---|---|---|---|
| Maven 만 | Gradle 은 거부(exit 3). pom 도 build.xml 도 없는 트리(풀어 놓은 Tomcat 웹앱)는 회사 결정으로 거부 — `sbmig inventory <dir>` 가 담당팀용 설명서를 만든다 | `sbmig/analyze/build_tool.py`, `sbmig/analyze/webapp_inventory.py` | — | `unsupported-build-tool:<x>`, `aborted` |
| Ant → Maven | build.xml 만 있으면 pom.xml 을 합성(레이아웃은 build.xml 에서, jar 좌표는 pom.properties → 이름 표 → sha1 조회). 모르는 jar 는 as-is 경로의 `system` scope + 인수인계에 `install-file` 명령. 그다음은 Maven 프로젝트와 같다 | `sbmig/analyze/ant.py`, `sbmig/analyze/jar_coords.py` | `intake.ant_to_maven`, `intake.unknown_jar`, `intake.jar_lookup`, `intake.synth_group_id`, `intake.min_java_source` | 플래그 `pom-synthesized`, `jar-coordinates-unknown`, 인수인계 "Ant pom.xml 합성" |
| 루트 pom 없는 트리 | 형제 pom(또는 `../DEV` 를 묶는 `APP/pom.xml` 하나 — 회사 모양)에 루트 aggregator 를 생성. 꼭대기가 하나여도. 집계 pom 하나만 있으면 다른 연결 안 된 pom 이 옆에 있어도 그것이 루트이고, 빠진 pom 은 인수인계에 이름이 남는다 | `sbmig/analyze/aggregator.py` | `layout.generate_aggregator` | `aggregator-generated` |
| reactor 중복 | 두 번 닿는 모듈의 중복 `<module>` 줄 제거. `duplicated in the reactor` 오류는 LLM 에 가지 않는다(`loop.blocked_policy` 의 `reactor_duplicate: 0`) | `sbmig/analyze/aggregator.py` | — | `reactor-duplicate-removed` |
| 배포 전송 확장 | `<build><extensions>` 의 wagon-webdav / wagon-ssh / wagon-ftp 류(목록 `knowledge/libraries/build_extensions.yml`)를 첫 Maven 호출 전에 모든 pom(프로필 포함)에서 제거. `mvn deploy` 전용이라 패키징에 필요 없고, 저장소에 없으면 OpenRewrite 까지 `Unresolveable build extension` 으로 막는다. 목록에 없는 확장은 기록만 | `sbmig/analyze/build_extensions.py` | `intake.strip_deploy_extensions` | `deploy-extensions-removed` + 인수인계 한 줄씩 |
| EAR 루트 | 모듈이 트리에 없는 EAR 루트는 거부(EAR 은 코드가 없고 WAR 의존성이 back 으로 가면 안 됨) | `ear_without_modules` | — | `ear-without-modules` |
| pom 복사본 | IDE 출력·백업 폴더(`boot/bin/`)의 모듈 통째 복사본: 등록 안 된 pom 의 g:a 가 다른 pom 과 같으면(그 아래 중첩이거나 reactor 에 닿는 것 옆) 모든 파싱에서 빼고 그 폴더를 모든 스캔에서 가지치기. 구조 검토 불변식 검사는 그런 복사본을 모듈로 넣은 편집을 복원한다 | `sbmig/analyze/pom_parser.py`(`stray_pom_copies`, `find_stray_pom_copies`), `Workspace.ignored_dirs` | `review.invariant_check` | `stray-pom-ignored` + 인수인계 한 줄 |
| 속 빈 리액터 | 모듈이 컴파일·포장하는 소스 루트에는 소스가 없고 트리 다른 곳(JDeveloper 작업공간 `DEV/`, `STATIC/`)에 있음 → 계속 돌지만 **성공 아님**(exit 2). 그 소스를 모듈로 옮기는 것은 다음 단계의 일 | `sbmig/analyze/hollow.py` | — | `hollow-reactor`, 인수인계 첫 줄, `attn=hollow-reactor` |
| ADF | Oracle ADF(JDeveloper) 는 변환하지 않는다. 근거 네 계열(BC4J XML, `oracle.jbo` / `oracle.adf` / `oracle.binding` import, ADF Model 바인딩, ADF Faces / 태스크 플로) 중 **둘 이상**이면 분석에서 중단. 하나뿐이면 흔적 메모만. `.jpr` 이름·`adf-config.xml` 만으로는 아무 뜻 없음 | `sbmig/analyze/adf.py` | `intake.adf_policy`(`stop` 기본 / `continue`) | `adf-project`(exit 3, "ADF sizing" 절) / `adf-traces` |

## 9. JSP javax → jakarta, 필터링되는 바이너리, 테스트

| 규칙 | 무엇 / 왜 | 어디 | 키 | 보고서 |
|---|---|---|---|---|
| JSP jakarta 이름 | OpenRewrite 는 JSP 를 파싱하지 않으므로, 파일이 최종 위치에 간 뒤 모든 모드에서 JSP / 태그 파일의 **코드 영역**에 있는 javax EE 이름을 jakarta 로 바꾼다(표 `sbmig/analyze/jakarta_packages.yml`). taglib URI 는 그대로(JSTL 3 이 `java.sun.com` URI 도 서빙). 재구성이 실패했으면 손대지 않음 | `sbmig/restructure/jsp_jakarta.py`(`rename_jsp_files`) | `layout.jsp_jakarta_rename`(false = 인수인계 한 줄만) | 커밋 `restructure: JSP javax -> jakarta`, 저널 `jsp_jakarta`, 노트 "JSP javax → jakarta: N files" |
| 필터링되는 바이너리 | `<filtering>true</filtering>` 리소스 안의 바이너리(웹폰트, 압축, 회사 `.dmdb`)를 maven-resources-plugin 이 텍스트로 읽다 `MalformedInputException: Input length = 1` 로 실패 → 재구성이 그 확장자를 `nonFilteredFileExtensions` 에 미리 적는다(필터링은 끄지 않고 파일은 지우지 않음, 플러그인 기본값·텍스트 확장자는 적지 않음). 바이너리 판정은 알려진 표 또는 내용 — 설정 없이 `.dmdb` 도 잡힌다 | `sbmig/restructure/resources_filtering.py` | `build.non_filtered_extensions`(텍스트로 읽히는 형식만) | 저널 `non_filtered_extensions`; 안전망은 사전 시도 `sbmig-resources` |
| 테스트 건너뛰기 | 범위 = 패키징 + 빌드 통과. 모든 빌드가 `-Dmaven.test.skip=true` | `_step_verify`, `sbmig/build/runner.py` | `build.skip_test_compile: true`(기본) | 플래그 `tests-not-compiled`, 후속 조치 "담당팀이 `mvn test-compile` 로 테스트를 고친다" |
| 소스 인코딩 | MS949 / EUC-KR 소스 모듈에 `project.build.sourceEncoding` 을 적는다(소스는 UTF-8 로 바꾸지 않음) | `layout.legacy_context` 의 `write_source_encoding` | `build.encoding_candidates` | 구조 검토는 누락을 critical 로 본다 |
| 외부 설정 자리표시 | as-is 가 외장 Tomcat 에서 받던 설정 키 중 패키지된 리소스가 정의하지 않는 것 → boot 모듈 `application.properties` 에 `__SET_ME__` 자리표시 | `sbmig/analyze/properties.py` | `externalized.*` | 저널 `placeholders`, 인수인계 |

## 10. 규칙을 바꾸는 법

1. 회사 값·스위치는 설정(`config/artifacts.yml`)에서 바꾼다. 새 키가 필요하면 `sbmig/core/config.py` 에 기본값과 함께
   더하고 `config/default.yml` 에도 적는다([07-config-and-knowledge.md](07-config-and-knowledge.md)).
2. 규칙 로직은 `sbmig/restructure/restructurer.py`(거대한 파일 — 메서드 이름으로 찾는다: `_layout`, `_boot_pom`, `_msa`, `_pin_asis_versions`,
   `_drop_managed_versions`, `_drop_bom_property_overrides`, `_drop_orphan_version_properties`, `_wire_bundled_jars`, `_manages_boot`).
3. 시나리오마다 fixture(`fixtures/projects/<이름>/` + `expected.json` + `replay/`)와 `tests/restructure/` 테스트를 더한다
   ([08-dev-conventions-and-tests.md](08-dev-conventions-and-tests.md)).
4. 운영자가 보는 변화(설정 키, 보고서 문장, 플래그)는 `docs/INTERFACES.md` / `docs/PLAN.md` 에 기록한다.

## 확인하는 법

```
cat config/artifacts.example.yml                                       # 자리표시 좌표와 규칙 설명
grep -n "^layout:" -A60 config/default.yml                              # 레이아웃 키 기본값
grep -n "self._non_filtered_extensions(c)" -A6 sbmig/restructure/restructurer.py   # pom 정리 순서
grep -n "class VersionResolver" -A12 sbmig/loop/fixloop.py              # 버전 결정 순서
grep -n "def _manages_boot" -A20 sbmig/restructure/restructurer.py      # parent_manages_spring_boot: auto
grep -n "^VERDICTS" sbmig/restructure/bundled_jars.py                   # 동봉 jar 판정 7가지
grep -n "critical" sbmig/review/prompts/review_v1.md | head             # 구조 검토의 critical 규칙
grep -n "^ADF_FLAG\|^TRACES_FLAG" sbmig/analyze/adf.py; grep -n "_FLAG =" sbmig/analyze/hollow.py
.venv/bin/python -m sbmig.cli repo-probe --help
.venv/bin/python -m pytest tests/docs/test_agent_doc.py -p no:cacheprovider -q
```
