# LLM 프롬프트 검토 — Spring Framework → Spring Boot 전환 (2026-10-31)

대상: `flow-v2` @ `1b30159` (v2.1t). 질문: "스프링 프레임워크 프로젝트를 Boot 로 옮길 때 문제가 되는 지점이 있는가,
그리고 LLM 이 받는 프롬프트·노트에 모자란 것 / 빼야 할 것이 있는가". 읽기 전용 검토이며 코드는 바꾸지 않았다.
원본(영문, 서브에이전트 5개) 은 `docs/v2/reviews/2026-10-31/review-{A..E}-*.md` 에 그대로 두었다.

| 축 | 본 것 | 결과 |
|---|---|---|
| A 전환 자체 | 재구성이 만드는 boot 모듈 / Application / web.xml 등록 / 후속 작업 vs Spring Framework 웹앱의 32개 관용 패턴 | H 8, M 9, L 15 — **빌드를 깨는 건 4개뿐, 나머지는 기동 시점 문제** |
| B 수정 프롬프트 | `script_system_v1.md` / `edits_system_v1.md` / `verify_system_v1.md`, 항상 노트, `ContextPacker`, 루프가 넣는 사실 줄 | H 3, M 9, L 3 |
| C 노트 정확성 | `knowledge/notes/*.md` 17개를 공식 문서·`~/.m2` jar(javap/javac) 와 대조, `index.yml` 키워드 선택 시뮬레이션 | 틀림 12, 낡음 16, 빠짐 6, 키워드 누락 17 |
| D 구조 리뷰 | `review_v1.md` + `sbmig/review/inputs.py` 가 실제로 내보내는 규칙·사실 줄 (7개 e2e 실행을 재렌더링) | H 5, M 10, L 8 |
| E 실증 | 어제 e2e 의 spring_legacy / servlet 픽스처 14개를 `llm.record_dir` 켜고 재실행 → 실제 프롬프트·KB 스크립트·Handover | H 5, M 10, L 9 |

**검증 수준.** 고심각 항목은 PM 이 코드에서 직접 재확인했다(verdict 줄, fix-policy 미전송, EUC-KR XML 재인코딩,
MSA/필수/auto 규칙 문장, `profile_summary`, Boot 4.1 jar 의 `server.servlet.encoding.*` deprecated=error,
`allow-bean-definition-overriding` 기본 false). 기동 시점 장애는 앱을 띄워 본 것이 아니라 Spring/Boot 동작에서 추론한
것이며 원본 보고서가 "reasoned" 로 표시한다. **중요한 전제 하나:** 어제 e2e 실행 디렉토리에는 LLM 요청 기록이 없다
(`llm.record_dir` 미설정, replay 파일은 답만 보관). B/E 는 같은 픽스처를 재실행해 기록했고 spring5-security-batch 의
prompt_hash 8/8 이 어제 journal 과 일치하므로 "어제 LLM 이 받은 그 프롬프트" 가 맞다.

## 0. 한 줄 결론

1. 프롬프트의 가장 큰 결함은 **거짓 거절 유도** 다: "Dependency versions" 절이 평범한 Spring 7 API 제거 오류에
   `spring-webmvc → verdict: no jakarta build is known … answer <no_script>` 를 써 넣고(어제 e2e 22회), 그 옆에
   부모가 관리하는 7.0.8 보다 새 7.0.9/7.0.10 을 나열한다. 회사의 "정책상 버전을 추정하여 추가할 수 없습니다" 와 같은 부류.
2. 사실 줄(`as-is build:` / `new parent chain:` / `parent chain property:`) 은 설계와 달리 **거의 안 나간다**
   (컴파일 오류에는 `e.artifact` 가 없음: 전체 실행에서 2 / 0 / 0 회). v2.1l~t 가 넣은 사실이 LLM 에 닿지 않는다.
3. LLM 은 **프로젝트가 Boot 로 전환됐다는 사실, 모듈 역할, 생성 파일(Application / legacy-config / WebXmlRegistrations),
   boot pom 의 starter, 회사 아티팩트 규칙** 을 어디서도 듣지 못한다. 헤더는 as-is(`java=1.8 spring=5.3.31`) 를 현재처럼 보여준다.
4. 구조 리뷰어는 **부모 체인이 무엇을 관리하는지 전혀 못 받는다**. 회사에서 본 혼합 Boot 버전·`spring-boot.version=2.5.15`
   섀도잉을 리뷰가 못 잡은 이유다. 게다가 auto 모드 규칙 문장이 "리터럴 4.1.0 import" 라고 말해 섀도잉을 유도한다.
5. 노트 17개 중 **틀린 문장 12개**(HttpComponents timeout 반대, JSP 실행 jar, jboss-logging, leading `**`, Boot 4.1
   removed 목록 등), 그리고 회사 주 케이스(spring_legacy) 는 **Boot 4 노트 두 개를 아예 못 받는다**(`cases: [spring_boot, msa]`).
6. 전환 자체: 빌드는 통과해도 **기동에서 깨지는 지점 8개(H)** 가 재구성에서 생긴다 — 모든 XML 을 한 컨텍스트로 import(중복 bean id,
   테스트 XML 포함) + Boot 의 overriding=false, 순환 참조 금지, EUC-KR XML 을 UTF-8 로 복사, `CharacterEncodingFilter` 미반영,
   JNDI DataSource, WEB-INF 설정 파일 이동, `<mvc:default-servlet-handler/>`, `@EnableWebMvc` 미감지. LLM 도 빌드 게이트도
   볼 수 없는 영역이므로 프롬프트가 아니라 **재구성의 결정론적 처리 + Handover 문장** 으로 막아야 한다.

## 1. 수정 프롬프트 (fix loop) — B·E

### H
- **B-H1 / E-H1 거짓 `<no_script>` 판정.** `sbmig/build/repo_versions.py:792` `_verdict_line` 의 마지막 분기가 "jakarta 여부
  미상"(Spring 의 pom 은 `jakarta.*` 의존을 선언하지 않음) 을 "jakarta 빌드 없음" 으로 읽는다. `_library_hints`
  (`fixloop.py:2839`) 는 모든 컴파일 오류의 소유 아티팩트에 저장소 목록+판정을 붙인다. 제안: (1) `VersionResolver` 가
  managed/boot 버전을 아는 아티팩트는 `new parent chain: manages g:a at v — API 가 바뀐 것, 코드를 고치고 버전은 그대로`
  한 줄만; (2) 저장소 나열과 판정은 오류가 `javax.` 를 말할 때만(v2.1d 의 원래 범위); (3) 판정 마지막 분기는
  "`<no_script>`" 대신 "같은 메이저 안에서 코드를 고쳐라".
- **B-H2 / E-M2 전환 사실 부재.** `profile_summary`(`fixer.py:244`) 는 분석 시점 프로파일만 쓴다(`java=1.8 spring=5.3.31
  packaging=jar modules=1 javax-imports=…`). `RestructureResult` 가 이미 가진 `boot_module / comp_module / front_module /
  boot_packaging / generated_files / bom_imports / parents_set` 는 루프에 전달되지 않는다(`orchestrator.py:1395` 는
  `merge_conflicts` 만). 제안: `FixContext.layout_facts` 섹션 "Project after the restructure"(순서 0.5, 자르지 않음, ~800자)
  — `converted by sbmig: spring_legacy → Spring Boot application / boot module <dir> (artifactId, packaging, parent) —
  generated: … / back module … / front … / XML via @ImportResource(<list>) / company artifacts: root parent, mandatory at root,
  optional only where used`. 값은 전부 `res` / `cfg.artifacts` / `cfg.layout` 에서(리터럴 없음). 헤더의 `module(s): x-comp` 는
  디렉토리·역할로 매핑. 헤더는 `as-is (before sbmig): …` 로 이름 붙이고 `javax-imports=` 와 advisory id 는 뺀다.
- **B-M3 / E-H2 사실 줄이 안 나감.** `_version_facts`(`fixloop.py:3612`) / `_chain_property_facts`(:3266) 는 `e.artifact`
  만 본다. 제안: `_case_libraries` 가 classpath 에서 찾은 GAV 에도 적용(`as-is build: spring-batch-core 4.3.10; new parent
  chain: manages … at 6.0.4 (major 4 → 6)`). 추가로 `package … does not exist` 에는 **B-M4 `_package_facts`**: 패키지의
  최장 접두 groupId 를 체인/Boot BOM 관리 목록에서 찾아 artifactId ≤25개를 나열("add the one that holds <pkg>, version-less").
- **B-H3 / E-L2 노트 선택·절단.** 점수가 import 줄에도 매겨져(`context.py:765`) 오류와 무관한 9K 노트(jakarta.md 의 `Filter`
  키워드가 `FilterInvocationSecurityMetadataSource` 에 매칭) 가 들어오고, `SCRIPT_TRUNCATE_ORDER` 는 `notes` 를 **가장 먼저**
  자른다(:267) — 1,200줄 컨트롤러 시뮬레이션에서 오류를 이름으로 맞춘 노트 2개가 잘리고 "Other error cases"/"Plan notes" 는
  남았다. 제안: 오류 본문 매칭 ×3, import/pom 매칭 ×1, 오류 매칭 0 인 노트는 다른 후보가 없을 때만; 절단 순서를
  `other_errors, plan_notes, notes, …` 로; `jakarta.md` 키워드 `Filter` → `\b(Filter|FilterChain|FilterConfig)\b`.

### M
- **B-M1 / C-O1 rule 8 Jackson 문구가 레시피와 충돌.** `UpgradeSpringFramework_7_0` 은 `UpgradeJackson_2_3` 을 포함해 코드·pom 을
  이미 `tools.jackson` 으로 옮겼고 Boot 4.1 BOM 은 Jackson 2/3 두 줄을 모두 관리한다. "already on Jackson 3 일 때만" 은 LLM 이
  판단할 수 없어 레시피 결과를 되돌릴 수 있다. 제안: C-M1 의 결정 규칙 노트(§3) 를 만들고 rule 8 은 그 노트를 가리키는 두 줄로.
- **B-M2 / E-M7 시스템 프롬프트 하드코딩 + 레시피 문장 오류.** 1~4행이 "JDK 21 + Jakarta EE 11 + Spring Boot 4.1 (Framework 7,
  Security 7, Hibernate 7)" 과 "for Spring Boot projects, the Boot 4.0/4.1 recipes" 를 박아 둔다(spring_legacy 는 Framework 7 +
  Security 체인을 돌림). 제안: `target_text(cfg)` 를 `{{ target }}` 로 렌더(`load_script_prompts` / `load_edits_prompts` /
  verify 의 `.replace`), 레시피는 "Plan notes 에 열거된 것" 으로. rule 8 "relocated Spring Boot 4 packages" → "the target Boot
  line's relocated packages".
- **B-M3 rule 14 길이·낡음.** 2.2K 자. 채움 순서가 v2.1s 이전("as-is, else newest"), `target Boot BOM:` 줄 설명 없음, "look releases up
  yourself" 는 도구 없는 회사 게이트웨이에서 지어내기 유도. 보고서 B 에 1.35K 자 대체문 있음.
- **B-M5 / B-M6 / E-M1 거절 경로.** (1) 출력 형식 마지막 줄 "If nothing can fix this case safely, answer with only `<analysis>`" 는
  무료 탈출구 → "프로젝트 밖의 것(저장소에 없는 아티팩트, 소유팀 결정) 일 때만, 그 blocker 를 이름으로"; (2) rule 10 이 노트의
  `fix-loop:` 절을 포함하지 않아 legacy-ibatis 에서 rule 14 가 노트가 요구한 MyBatis 포팅을 금지 → 같은 원인 케이스 3개 × 3회 = **9회
  호출 후 BLOCKED**(CLAUDE.md 는 "최대 4회"). 제안: rule 10 에 "or a Migration note's `fix-loop:` section" 추가; 편집도 스크립트도
  없는 답은 `declined` 로 분류해 첫 거절에 BLOCKED `needs_owner`, 같은 파일의 형제 케이스는 호출 없이 "same cause as …"
  (`script.max_declines_per_case: 1`). `summarize_script_attempt` 는 분석을 300자에서 표시 없이 자른다 → `textwrap.shorten`.
- **B-M7 / E-H5 삭제된 v1 핸들러 문구.** `advisories.yml:255-270` "jdk-base64-shim … sbmig.compat shims (76자 줄바꿈 유지)" 가
  프롬프트(llm-0005) 와 Handover 에 그대로 나가고, `spring-security-7.md:64` "`add-known-dependency` adds it",
  `advisories.yml:739` "handler querydsl-jakarta-classifier". 실제 코드는 `Base64.getEncoder()` 로 줄바꿈이 사라졌는데
  `MIGRATION-TODO` 도 없다. 제안: 문구 교체 + `knowledge/` 에 삭제된 핸들러 id 가 남지 않는 테스트.
- **B-M8 `fix-policy.md` 는 LLM 에 가지 않는다.** `pack_script` 가 always-note 를 의도적으로 뺀다(`context.py:770-772`, 테스트가
  보장). 낡은 줄(테스트 수정, `-Dmaven.test.skip` 금지, confidence) 은 그래서 무해하지만 `docs/migration-agent/README.md` 는 "항상
  포함" 이라고 한다. 제안: 노트·index 항목·테스트 기대를 지우고 README 를 고친다(사용자 결정).
- **B-M9 / A-§3 `servlet-to-boot.md` 가 9c·v2.1q 와 모순.** "JSPs … in an executable jar", "do not move JSPs", "`war` → executable
  jar in the company layout", "`spring-boot-starter-tomcat-runtime`". 제안: "sbmig 가 이미 JSP 를 front 의
  `META-INF/resources/WEB-INF/jsp` 로 옮기고 boot 를 실행 WAR(`java -jar`, 외부 Tomcat 배포도 가능) 로 만들었다; `spring-boot-starter-tomcat`
  / `tomcat-embed-jasper` provided 는 그대로; 패키징·JSP 위치·스코프를 바꾸지 마라".

### L (토큰·문구)
- Plan notes 1.5~2.1K 자 중 절반이 소음(`case … (confidence 0.75)`, `optional artifacts: bumped`, `review:`, `advisory: … → guide`) →
  `transform/restructure/warning/msa/followup:` 만 전달(B-L1). 시스템 프롬프트 17.8K 자 중 1.4K 자가 반복(`<verify>` 설명 2회,
  intro 4항 재진술, "Script or no script" 절) → B-L2 의 삭제 목록. `verify_system_v1.md` "The build passes" 는 재빌드 전이라
  틀림(B-L3). `other_errors` 는 4,000자에서 줄 중간에 표시 없이 잘림(`fixloop.py:2425`). 후속 케이스 프롬프트는 어떤 스크립트가
  그 오류를 만들었는지 말하지 않는다(E-L9: "introduced by script:…:v1" 추가, 새 스크립트보다 augment 선호).

## 2. 구조 리뷰 — D·E

### H
- **D-H1 버전 사실 없음.** `fact_lines`(`inputs.py:628`) 는 mandatory/optional/javax/jakarta/중복만 내보내고 체인이 관리하는 버전·
  속성은 없다. `review_v1.md:24-25` 는 "explicit versions that override … worth a warning" 으로 경고 이상이 못 된다. 제안:
  `version_fact_lines(poms, cfg, results)` — `load_parent_chain_facts` / `load_boot_bom_facts` 에서 `parent chain property:`,
  `explicit version on a managed artifact: <pom> g:a v — managed m by …`, `Spring Boot line: <pom> … (target …)`, `property shadows the
  parent chain: …` 줄을 만들고, **다른 Boot 라인의 Boot 아티팩트 / 섀도잉 속성은 critical** 로 승격. 보통은 빈 줄(토큰 0) 이고
  재구성이 놓친 경우에만 리뷰가 첫 빌드 전에 고친다.
- **D-H2 auto 규칙 문장이 리터럴 import 를 말한다.** `inputs.py:486-492` "else spring-boot-dependencies 4.1.0 imported in the poms".
  v2.1t 는 `${spring-boot.version}` 을 쓰므로 LLM 이 "정의 안 된 속성" 으로 보고 `set_property spring-boot.version 4.1.0` 을 하면
  v2.1o 가 지운 섀도잉이 되돌아온다. 제안: `results` 의 실효값으로 "체인이 BOM 에 닿음 → 프로젝트에 Boot BOM/버전 없음 | 닿지 않음 →
  재구성이 <poms> 에 `${prop}` (= value, defined by <pom>) 로 import; **그 속성을 프로젝트 pom 에 정의하지 마라**".
- **D-H3 MSA 규칙 문장이 코드와 반대.** `inputs.py:503-506` "`*-boot` 이름의 서비스 모듈만 boot parent, 나머지 comp parent" vs 코드
  (`restructurer.py:5224-5260`): 모든 서비스 루트 → boot parent + 필수, 라이브러리 루트 → comp parent, 필수 없음. "wrong parent" 는
  critical 이므로 리뷰어가 서비스를 comp-jar 로 되돌리고 `common` 에 필수를 넣으려 한다. 제안: `profile.services` 이름으로 실제 규칙을 출력.
- **D-H4 / E-M4 패키징 문장 낡음.** `review_v1.md:16` "war only … JSPs for an external Tomcat", `inputs.py:508` 동일, 같은 프롬프트의
  후속 작업은 "executable WAR, java -jar" 라고 말해 **자기 모순**. `provided` 인 `spring-boot-starter-tomcat` / `tomcat-embed-jasper` 의
  이유를 말하지 않아 리뷰어가 스코프를 뒤집을 수 있고, "otherwise jar" 는 유지하기로 한 Boot as-is / MSA `war` 를 뒤집는다. 제안 문구는
  보고서 D-H4(실행 WAR, lib-provided, as-is 패키징 유지, 이 규칙과 어긋날 때만 보고).
- **D-H5 필수 아티팩트 = "BOM import".** `inputs.py:475-476` 이 모든 필수 spec 에 "type pom + scope import → dependencyManagement" 를
  붙인다(코드는 `scope: import` 일 때만). 회사 필수 아티팩트는 일반 의존이므로 "missing mandatory" critical 오판 위험. 제안: spec 별로
  `→ <dependencyManagement>` / `→ <dependencies>` 를 붙이고 "role parents 에서는 모듈이 루트 선언을 상속하지 않는 것이 규칙" 을 명시.

### M
- **D-M1 / E-M4** 사용자 프롬프트 마지막 줄이 `<script>` 를 요구하고 시스템은 `<edits>` 를 요구 → "`<analysis>`, `<solution>`, `<edits>`".
- **D-M2 / E-L3** 리뷰의 `<script>` + helper API 가 시스템 프롬프트의 ~30%(3.7K/12.4K) 인데 저장만 되고(`review|structure` 한 케이스)
  아무도 읽지 않는다 → edits-only 리뷰(사용자 결정; PLAN-v2.1 의 "스크립트 저장" 과 맞물림).
- **D-M3 / E-L1** 재구성 notes 가 "run notes" 로 한 번 더(1.1K 자, 14/14 실행) → 중복 제거, 대신 `parent_manages_spring_boot_effective`,
  `non_filtered_extensions`, `jsp_jakarta_*` 를 렌더.
- **D-M4** 리뷰 편집 뒤 재구성 불변식 재검사 없음(체인이 정의한 속성 추가, 관리 아티팩트에 버전 추가, protected key 변경, 낯선 `<parent>`)
  → 적용 후 체크, 위반이면 복원 + 재질의 1회. **D-M6** "What the restructure did" 의 버전 결정(예: spring-batch-core 4.3.10 → managed
  6.0.4) 은 final 이라고 못 박기(리뷰 규칙 "same major" 가 재핀을 유도).
- **D-M5** optional 규칙 문장이 v2.1n/t 이전(floor 가 as-is 더 새 버전 유지, `${artifact.version}`, `when_present` 추가/대체 미반영).
- **D-M7** 인코딩 체크가 루트 `pom.xml` 의 이슈를 모듈 해결로 간주하는데 role parents 에서 모듈은 루트를 상속하지 않음 → 한글 리터럴이
  조용히 깨짐. 제안: 프로젝트 내 체인일 때만 인정, 편집 후 재계산; 더 좋은 것은 **재구성이 `project.build.sourceEncoding` 을 직접 씀**(v2.1m 방식).
- **D-M8** boot 모듈이 받은 starter(`layout.starter_by_case`) 를 알려주고 Security/Batch/JPA starter 추가를 금지(자동 설정이 런타임을 바꿈).
- **D-M9 / E-M6** OpenRewrite 가 `javax.servlet-api provided` 를 `jakarta.servlet-api compile` 로 바꾼다(웹 픽스처 10개 중 8개, 리뷰는 3개만
  경고). 제안: 사실 줄 + 경고, 더 좋은 것은 재구성이 as-is 스코프 복원("scope restored: provided (as-is)" 행).
- **D-M10** 다른 모듈이 의존하는 pom 의 `spring-boot-maven-plugin` repackage → `BOOT-INF` 로 숨어 "package does not exist" (critical 제안, 빈도 미확인).
- **E-M5** 낡은 리뷰 replay 가 Handover 에 거짓 경고를 넣는다(4/13: 프롬프트에 없는 `5.0.0`, 이미 제거된 `spring.version`). e2e 는 개수만
  검사해 녹색. 제안: `parse` 후처리로 재료에 없는 버전/속성 이름은 "(sbmig check) not in the material" 로 Handover 제외 + replay 4개 재생성 +
  `issues_not_contain` 기대. **D-L5** 리뷰 입력을 `<run dir>/logs/review-input.md` 로 남기고 오프라인 테스트가 H1~H5 줄을 단언.

## 3. 노트 (`knowledge/notes/`) — C (+A-B, E-M7)

### 틀린 문장 (먼저 고칠 것; 출처는 보고서 C 의 URL / jar)
| # | 노트 | 지금 | 사실 |
|---|---|---|---|
| W1 | spring-framework-7.md:56-58 | `setConnectTimeout(int)` 만 deprecated, `setReadTimeout` 없으면 SocketConfig | **반대**: 7.0 은 `HttpComponentsClientHttpRequestFactory.setConnectTimeout` 두 오버로드를 모두 제거, `setReadTimeout(int|Duration)` 은 존재. 레시피는 주석만 달아 오류가 항상 LLM 에 온다 |
| W2 | servlet-to-boot.md:55-64 | JSP 가 실행 jar 에서 됨, war → 실행 jar | Boot 문서: executable jar 는 JSP 미지원; sbmig 는 v2.1q 부터 실행 WAR |
| W3 | hibernate-7.md:23-25 | jboss-logging 이 hibernate-core 로 classpath 에 있음 | hibernate-core 6.5~7.4 는 `runtime` 스코프 선언 → 컴파일 불가; `jboss-logging` (Boot 관리, version null) 추가 필요 |
| W4 | spring-security-7.md:58-59, spring-framework-7.md:88 | `/**/*.do` 가 기동에서 거부 | 7.0.8 파서로 실행: **앞의 `**` 는 허용**, 가운데 `**` 만 거부(6.2.12 는 둘 다 거부). `*.do` 앱에 중요 |
| W5 | boot40-to-41.md:22-24 | 4.1 에서 `spring-boot-jackson2`, `HttpMessageConverters`, `-starter-web` 제거 | 4.1.0 BOM/jar 에 모두 존재(deprecated); 4.0 에서 사라진 것은 `spring-boot-starter-aop` → `-aspectj` |
| W6 | boot2-to-3.md:53 | `matching-strategy=ant_path_matcher` 제거 | 4.1.0 `WebMvcProperties.MatchingStrategy.ANT_PATH_MATCHER` 존재 |
| W7 | spring-framework-7.md:80-81 | 버전 붙은 xsd 가 실패할 수 있음 | `spring.schemas` 가 2.0~4.3 을 현재 스키마로 매핑; 빌드 오류도 아님 |
| W8 | era-libraries.md:81-85 | collections 3→4 레시피 후 남은 import 를 4 로 | 그 레시피는 `sets: []`(안 돎), 3→4 는 rule 14 가 금지하는 교체; 3.2.2 는 JDK 21 에서 컴파일됨 |
| W9 | ibatis2.md:32 | mybatis-spring 3.0.x 이상 | 3.0 은 Spring 6 용; Spring 7 / Batch 6 는 **4.x 라인**(`jakarta_libraries.yml` 은 이미 4.0/4.1) |
| W10 | boot2-to-3.md:64 | JUnit 4 vintage 미관리 | 4.1.0 BOM 이 `junit:junit` 4.13.2, vintage 관리 (테스트는 어차피 스킵 → 삭제) |
| W11 | jakarta.md:83-84 | `createQuery(hql)` → `createQuery(hql, Result.class)` | raw `createQuery(String)` 은 7.4.1 에 존재(deprecated) — import 만 `org.hibernate.query.Query` 로 |
| W12 | boot40-to-41.md:37 | `spring-boot-batch-data-mongo` | `spring-boot-starter-batch-data-mongodb` |

### 구조적 문제
- **`boot3-to-4.md` / `boot2-to-3.md` 가 `cases: [spring_boot, msa]`** (C-0, A-B4): 회사 주 케이스 spring_legacy / servlet_tomcat 은
  Jackson 3 규칙, starter 이름, Boot 4 패키지 이동(`ErrorPage`, `ServletComponentScan`, `TomcatServletWebServerFactory`) 을 못 받는다.
  제안: Boot 4 **패키지/starter 이동** 부분을 `cases: []` 노트로 분리(키 `org\.springframework\.boot\.` / `spring-boot-starter-`).
- spring_legacy 는 `SpringBatch5To6Migration` / `MigrateToHibernate71` 레시피를 받지 않고(`sets: []`), servlet_tomcat 은
  `RemovalsServletJakarta10` 을 받지 않는다 → **Batch / 네이티브 Hibernate / Servlet 6 제거는 노트가 유일한 안내**인데 Batch 노트가 없다.
- 리터럴 버전이 지시문으로 쓰임(C-O4/O10, E-M7, A-§3): maven-plugins.md 표(Boot 4.1 자체 pluginManagement 보다 뒤짐; **BOM import 는
  플러그인 버전을 관리하지 않음**, parent 상속일 때만), jdk21.md(nashorn 15.4, MapStruct 1.6.3, Surefire 3.5.3), era-libraries.md,
  jakarta.md 표(servlet-api 6.1.0, persistence 3.2.0 …), ibatis 안내가 한 프롬프트 안에서 두 라인(3.0.x vs 4.1.0) 을 말함. 제안: 호환
  사실(Lombok ≥ 1.18.30 for JDK 21 등) 은 유지, "X 를 써라" 는 "`version: null` / Dependency versions 절 / 부모 체인 속성" 으로.
- 테스트 전용 안내가 살아 있다(O8): `junit4-to-5.md` 의 키워드 `Matchers` 가 `antMatchers` 에 매칭되어 노트 슬롯을 뺏음 → 키워드 축소 또는 제외.
- 낡음·부정확(C-O2~O16 요약): Servlet 6 제거 목록 불완전(`HttpSession.getValue/putValue`, `getRealPath`, `UnavailableException`);
  `DaoAuthenticationProvider` 무인자 생성자·`setUserDetailsService` 는 7.0 에서 **제거**; `Jackson2ObjectMapperBuilder` 의 Jackson 3 대응은
  `JsonMapper.builder()`; Hibernate 7.4 제거 애노테이션 목록(`@WhereJoinTable`, `@Proxy`, `@LazyCollection`, `@IndexColumn`,
  `@ForeignKey`, `@Loader`, `@OrderBy`…), Hibernate Validator 9.1 의 `org.hibernate.validator.constraints.NotBlank/NotEmpty/Email` 제거;
  `javax.annotation` / `javax.inject` 애노테이션은 Spring 7 이 무시하므로 **javax-annotation-api 를 추가해 컴파일만 맞추지 말 것**;
  spring-retry 는 유지(`version: null`) 하고 `core.retry` 포팅은 소유팀; advisory `notes:` 포인터 3개가 내용 없는 노트를 가리킴(log4j1, struts2/egovframe, oauth2-legacy).

### 빠진 노트 (빌드 오류가 필요로 하는 것만)
- **C-M1 `jackson-2-3.md` (가장 중요).** 사실: Boot 4.1 BOM 은 3.1.x 와 2.21.x 둘 다 관리; `spring-boot-starter-webmvc` 는 Jackson 3 만 가져옴;
  애노테이션은 `com.fasterxml.jackson.annotation` 그대로; 레시피가 이미 옮김. 규칙 A(모듈이 Jackson 3 → 그 파일의 이동을 마무리:
  `JsonProcessingException` → `tools.jackson.core.JacksonException`, 컨버터 → `JacksonJsonHttpMessageConverter(JsonMapper)`,
  `Jackson2ObjectMapperBuilder` → `JsonMapper.builder()`; 되돌리지 말 것), 규칙 B(라이브러리가 Jackson 2 타입을 요구 → 그 파일만 2 유지 +
  `jackson-databind` version null), 규칙 C(모듈에 3 이 없고 `package com.fasterxml.jackson.databind does not exist` → 2 추가), 규칙 D(spring_legacy
  에 `spring-boot-jackson2` 추가 금지). XML 의 `MappingJackson2JsonView`(eGov jsonView) 는 기동 문제 → Handover.
- **C-M2 / E-H2 `spring-batch-6.md`.** 패키지 이동(`item.*` → `infrastructure.item.*`, `core.Job/Step` → `core.job/step`, listener, explorer,
  repeat), `JobBuilderFactory` → `new JobBuilder(name, jobRepository)`, `.chunk(n).transactionManager(tm)`, `ItemWriter.write(Chunk)`,
  `DefaultBatchConfigurer` 제거; 런타임(`@EnableBatchProcessing` resourceless, `BATCH_JOB_SEQ`) 은 Handover. 지금은 llm-0003 이 **노트 0개**
  로 갔고 replay 저자가 답을 알아서 통과한 것.
- **C-M3 / A-B3 spring-framework-7.md 추가.** `APPLICATION_JSON_UTF8`, 제거된 `HttpStatus` 상수(`MOVED_TEMPORARILY` → `FOUND`,
  `REQUEST_ENTITY_TOO_LARGE` → `CONTENT_TOO_LARGE`…), `getStatusCodeValue()`, `org.springframework.remoting.*`(후속 없음 → TODO/Handover),
  `OkHttp3ClientHttpRequestFactory`, `@Required`, Theme 지원, `Assert.notNull(x)` 무메시지, `Log4jConfigurer`, `CommonsMultipartFile`,
  `NestedIOException`, `OpenSessionInViewFilter`; Spring 4→5: `AbstractExcelView`, `queryForInt/Long`, `SimpleJdbcTemplate`.
- **C-M4~M6**: Spring Data 1.x→2 (`findOne` → `findById().orElse(null)`, `new PageRequest` protected → `PageRequest.of`), Security 3/4
  `authentication.encoding.*`(해시 호환 대체 없음 → `<verify>`), commons-lang 2 / log4j 1 은 "as-is 좌표를 version null 로 재선언, 교체는 Handover".
- **A-B1 생성된 `WebXmlRegistrations` 의 javax 전용 필터/서블릿**(`cannot be converted to jakarta.servlet.Filter`): 지금 노트는 "생성 파일이
  아니라 클래스를 고쳐라" 라고만 해 javax 전용 라이브러리는 BLOCKED 까지 돈다. 제안: jakarta 빌드/classifier 로 pom 수정, 없으면 **그 @Bean 하나만**
  `MIGRATION-TODO` 주석으로(사용자 결정 — "코드 주석 처리 금지" 와 충돌). **A-B2** `CommonsMultipartResolver` → 삭제 + `spring.servlet.multipart.*`
  한도 이전(지금 노트는 크기 setter 가 없는 `StandardServletMultipartResolver` 만 말해 한도가 사라짐).
- **키워드**(C §4 표, 17줄): Jackson 2/3, Batch, `APPLICATION_JSON_UTF8`, 제거 `HttpStatus` 상수, remoting, `util.concurrent`, `@Required`/`Assert`,
  Servlet 6 세션 메서드, Spring Data `findOne`, Security encoding, commons-lang/log4j 는 **지금 노트가 전혀 선택되지 않는다**. 오탐: `release`
  가 Nexus `maven-releases` URL 에, `layout` 이 `com.x.layout` import 에, `Optional<` 이 Spring Data 오류에, `is deprecated` 가 javac 경고에 매칭.

## 4. 전환 자체 — LLM 이 볼 수 없는 지점 (A, + E-H4/M6/M9/M10)

32개 패턴 중 빌드를 깨는 것은 4개(§3 의 A-B1~B4). 나머지는 **기동** 에서 드러나고 재구성이 만든 문제가 대부분이다. 낮은 위험의 결정론적
처리(D) 가 가능한 것과 Handover 문장만 필요한 것을 나눴다(상세·문장·훅 함수는 보고서 A §2b, 한 YAML 표 `legacy_context_rules.yml` 로 묶는 안 §2c).

| # | 패턴 | 지금 | 제안 |
|---|---|---|---|
| 1 | root(ContextLoaderListener) + servlet 컨텍스트 두 XML, `<import>` 그래프, 테스트 XML | `xml_bean_configs` **전부**(src/test 포함) 를 `legacy-config/` 에 복사해 한 컨텍스트로 `@ImportResource`; Boot 는 overriding=false | **D2** web.xml `contextConfigLocation` 의 **진입 XML 만** import, `src/test/` 제외; **D1** `spring.main.allow-bean-definition-overriding=true` + `allow-circular-references=true` 를 YAML 표에서 쓰고 중복 id 목록을 Handover 에(사용자 결정) |
| 3 | EUC-KR XML | `c.write(.., c.ws.read(cur))` 가 UTF-8 로 재인코딩, 헤더는 EUC-KR 그대로(probe 확인) | **D3** `c.copy()` 바이트 복사(한 줄) |
| 4 | `CharacterEncodingFilter`(EUC-KR) | 건너뛰고 힌트가 `server.servlet.encoding.*`(4.1 에서 deprecated=error) | **D4** init-param 에서 `spring.servlet.encoding.charset/force` 를 씀; `HiddenHttpMethodFilter`/`FormContentFilter`/multipart-config 도 같은 표 |
| 5 | JNDI DataSource | `spring.datasource.*=__SET_ME__` 만; `<jee:jndi-lookup>` 빈이 legacy-config 에 남고 `spring-boot-jdbc` 가 없어 바인딩도 안 됨 | **D5** Handover 문장(빈 제거 + starter-jdbc) 또는 플래그 뒤 결정론 수정; E-H4 는 XML 옆 `<!-- MIGRATION-TODO -->` |
| 6/7 | `/WEB-INF/config/*.properties`, `classpath:…${profile}…` 위치 | WEB-INF 파일은 읽히지 않는 `asis-webinf/` 로, 패키지된 값이 `external` 로 보고되어 `__SET_ME__` 가 **실제 값을 덮어씀** | **D6** 사본의 위치를 `classpath:asis-webinf/…` 로 재작성, 분석기가 WEB-INF properties 를 정의로 셈; **D7** `${…}` 를 `*` 로 바꿔 glob |
| 8 | `<mvc:default-servlet-handler/>` | 없음(company-spring5-web 픽스처에 있음, Handover 침묵) | **D8** `server.servlet.register-default-servlet=true` + 문장 |
| 9 | XML `CommonsMultipartResolver` | XML 은 컴파일 안 됨 → LLM 못 봄, 기동 실패 | **D9** 사본에서 빈 제거 + `spring.servlet.multipart.*` |
| 10/11 | `@EnableWebMvc` / `WebMvcConfigurationSupport`, `<mvc:resources>` | 미감지; `spring.mvc.view.*` 를 써도 무시됨, front `static/` 404 | **D10** 감지 → `spring.mvc.view.*` 쓰지 않고 Handover; 사본의 resolver prefix 재작성 |
| 12/13 | `DelegatingFilterProxy`(비보안 대상 포함), `OpenEntityManagerInViewFilter` | 모두 "Security 자동설정이 등록" 으로 skip(probe: XSS 필터 탈락); OEMIV 는 `spring-boot-jpa` 없으면 없음 | **D11** `springSecurityFilterChain` + starter-security 일 때만 skip, 아니면 `DelegatingFilterProxyRegistrationBean`; 규칙에 `requires:` |
| 14~19 | `*.do` 매핑(`spring.mvc.servlet.path` 는 접두만), `<jsp-config>`, `WebApplicationInitializer`, WAR context root, Log4j 2, Security XML | 후속 작업 없음/부정확 | **D12** Handover 문장(보고서 A 에 문장·훅) |
| E-M6 | `javax.servlet-api provided` → `jakarta.servlet-api compile` | 레시피 결과 그대로 | 재구성이 as-is 스코프 복원 |
| E-M9 | 웹이 아닌 spring_legacy(CLI) | `starter_by_case` 로 webmvc starter → Tomcat 이 뜸 | 웹 증거(spring-webmvc / servlet-api / web.xml) 있을 때만 webmvc, 아니면 `spring-boot-starter` |

낡은 문구(A §3): `Application.java.j2:26`, `config/default.yml:44`, `config.py:195`, restructurer 독스트링 여러 곳, `web_xml_rules.yml:13/14/17`,
`webxml_registrations.py:252-255`, `restructurer.py:2507`(`@ServletComponentScan` 주장이 설정 플래그 기준), `:3131`, `:2933`(context.xml 조언),
`externalized.yml:53`. 설계 질문: `_populate_front` 가 `WEB-INF/views/x.jsp` 를 `WEB-INF/jsp/views/x.jsp` 로 옮겨 모든 prefix / include / forward 경로를
바꾼다 — 회사 규칙이 허용하면 WEB-INF 상대 경로를 그대로 두는 편이 한 부류의 장애를 없앤다.

## 5. 실행 데이터에서 본 것 (E)
- 13개 spring_legacy / servlet 픽스처 중 11개는 **리뷰 호출 1회뿐**(fix loop 가 LLM 없이 끝남). fix 호출은 spring5-security-batch(27 케이스 → 8회 new,
  빌드 ok) 와 legacy-ibatis(1 원인 → 9회, BLOCKED) 뿐.
- **E-H3** KB 스크립트 `1b37194675` 가 비멱등(두 번 돌리면 `new JobBuilder(.., jobRepository, jobRepository)`); sbmig 는 멱등성을 검사하지 않는다 →
  `status == ok` 직후 한 번 더 실행해 변경이 있으면 `non_idempotent` 로 표시(재사용 금지).
- 다음 프로젝트가 재사용할 replay 스크립트 중 rule 9(`MIGRATION-TODO`) 위반 2건(BASE64 줄바꿈, DB 로드 패턴의 PathPattern 변환).
- 픽스처 소스에 답이 새어 있음(E-L5: "add-known-dependency adds it", "jdk-base64-shim", 프로젝트 레시피 이름) — 실제 프롬프트에 그대로 들어간다.
- Handover 문구: "web.xml … registered via @ServletComponentScan" 은 틀림(스캔은 web.xml 을 읽지 않음); view prefix 후속이 `@Controller` 0개인 서블릿
  앱 5개에 소음; 중복 4건; legacy-ibatis 는 포팅 안 됐는데 "포팅된 DAO 를 검토". 비밀·절대경로·`.env` 유출은 31개 기록에서 없음.
- 픽스처 공백: log4j 1 설정, `WebApplicationInitializer`, `CommonsMultipartResolver`, Sitemesh/Tiles, `spring.profiles.active` context-param 을 가진
  픽스처가 없다 → §4 의 Handover 를 e2e 로 검증하려면 하나 추가.

## 6. 제안 작업 순서 (증분)
| 증분 | 내용 | 규모 |
|---|---|---|
| **v2.1u 프롬프트·노트 텍스트 정정** | B-H1 판정 분기 / rule 8·10·14 / 마지막 줄 / 타깃 `{{ target }}` / B-L1·L2 삭제; D-H2~H5·M1·M5·M6 문장; 노트 W1~W12 + servlet-to-boot·Application 템플릿·`default.yml`·`web_xml_rules.yml` 의 외장 Tomcat 문구; advisories 의 v1 핸들러 문구; `boot3-to-4` 분리; 키워드 17줄 + 오탐 5건 | 코드 거의 없음, 테스트 기대 갱신. 반나절 |
| **v2.1v 사실 배관** | `layout_facts` 섹션(B-H2), `_version_facts` 를 located GAV 로(B-M3), `_package_facts`(B-M4), 노트 점수·절단 순서(B-H3), `version_fact_lines` + critical 승격(D-H1), `parent_manages_spring_boot_effective` 렌더(D-H2), run notes 중복 제거(D-M3), `review-input.md` 보존 + 오프라인 렌더 테스트(D-L5) | 1일 |
| **v2.1w 새 노트** | `jackson-2-3.md`, `spring-batch-6.md`, spring-framework-7 추가 항목, Spring Data 1.x / Security 3·4 / commons-lang2·log4j1 절, A-B1/B2 | 반나절 + 픽스처 1개 |
| **v2.1x 전환 결정론 처리** | D3(바이트 복사), D4(encoding/multipart 속성), D7(`${}` glob), D8(default servlet), D6(위치 재작성), D2(진입 XML 만), D1(overriding/circular — 결정 필요), D9/D10/D11, E-M6 스코프 복원, E-M9 starter 판정, Handover 문장 D5/D12, 낡은 문구 | 2일, 픽스처 1~2개 |
| **v2.1y 루프·리뷰 정책** | `declined` → BLOCKED `needs_owner` + 형제 보류(E-M1), 멱등성 2회 실행(E-H3), 리뷰 편집 후 불변식 검사(D-M4), 인코딩 체크(D-M7) 또는 재구성이 sourceEncoding 작성, replay 거짓 경고 차단(E-M5), 리뷰 edits-only(D-M2, 결정 필요) | 1일 |

## 7. 사용자 결정이 필요한 것
1. `spring.main.allow-bean-definition-overriding=true` / `allow-circular-references=true` 를 legacy 전환에 자동으로 쓸지(A-D1; as-is Framework 동작 복원이지만 Boot 기본과 다름).
2. 리뷰의 `<script>` 를 없애고 edits-only 로 갈지(D-M2; 저장되나 재사용 0).
3. `fix-policy.md` 를 삭제할지(B-M8; LLM 에 안 가고, 회사 `fix.plugin` 이 읽는지 미확인).
4. 생성된 `WebXmlRegistrations` 에서 javax 전용 라이브러리의 @Bean 하나를 `MIGRATION-TODO` 로 주석 처리하도록 허용할지(A-B1; "코드 주석 처리 금지" 와 충돌).
5. JSP 를 `WEB-INF/jsp/` 아래로 모으는 규칙을 유지할지(A 설계 질문; WEB-INF 상대 경로 유지가 prefix/include 장애를 없앰).
6. `spring_legacy` 의 webmvc starter 를 웹 증거가 있을 때만 줄지(E-M9).
7. 레시피 세트: spring_legacy 에 `SpringBatch5To6Migration` / `MigrateToHibernate71`, servlet_tomcat 에 `RemovalsServletJakarta10` 을 넣을지(C-0; "레시피는 아는 것만" 원칙과의 균형).

미검증으로 남긴 것(원본에 "unverified"): 기동 장애 전부(앱을 띄우지 않음), MapStruct JDK 21 하한, querydsl 5.1 + Hibernate 7.4, `server.forward-headers-strategy` WAR 무시,
`has_spring_boot_plugin` 의 pluginManagement 처리, 회사에서 D-M10 빈도.
