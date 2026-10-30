# sbmig v2.1 — 실행 흐름, 케이스, KB 적재물, LLM 개입 지점과 한도 (사내 설명용)

> 2026-10-29: Ant 프로젝트(`build.xml` 만 있고 pom.xml 없음)는 접수에서 pom.xml 을 합성해 받는다(`intake.*`, 아래 §1 접수 참고).
> 코드를 직접 읽을 때: 모든 모듈·공개 함수에 한국어 docstring 이 먼저 온다(규약 `docs/v2/CODE-COMMENTS-KO.md`).
> 패키지 지도는 `sbmig/__init__.py`, 코드를 읽으며 발견한 의심 버그·정책 질문은 `docs/v2/HANDOVER-FINDINGS-2026-10-28.md`.


2026-10-28, 브랜치 `flow-v2` (v2.1f 포함). 아래 샘플은 전부 로컬 e2e 실행(`pure-java` → `java6-legacy`, KB `out/e2e/kb-jaxb`)의
실물을 줄여서 옮긴 것이다. 자세한 계약은 `docs/INTERFACES.md`(v2 · v2.1 · v2.1d · v2.1e 절), 결정 이력은 `docs/v2/PLAN-v2.1.md`.

## 0. 한 장 요약

- 한 프로젝트 = 한 번의 `sbmig run`. OpenRewrite 를 **한 번** 돌리고(Java 21 · jakarta · Boot 4), 회사 구조로 **재구조화**하고,
  LLM 이 pom·모듈을 **점검**한 뒤 `mvn clean package` 를 돌린다. 빌드 오류는 **케이스**(같은 원인의 오류 묶음)별로 하나씩 고친다.
- 케이스를 고치는 순서: **KB 탐색 → 저장된 스크립트 실행(LLM 호출 없음) → 실패하면 LLM 에 보강 요청 → 코드로 안 되면 이유를 적고
  직접 수정 → 처음 보는 케이스는 LLM 이 직접 수정 + 스크립트 저장**. 모든 시도는 git 커밋 → 재빌드로 채점 → 나아지지 않으면 되돌린다.
- KB 는 파일뿐이다(DB 없음): `errors.jsonl`(모든 오류 레코드), `cases/<case_id>/`(케이스별 스크립트·오류 JSON), `cases.md`(사람용 표),
  `libraries.yml`(사람이 넣는 라이브러리 표).
- LLM 이 개입하는 곳은 두 군데뿐: **구조 점검**(프로젝트당 1~3회: 점검 1 + 형식 재시도 1 + 편집 적용 실패 재시도 1)과 **수정 루프**(케이스당 최대 3회 + 보조 호출). 나머지 단계는 전부 결정적이다.
- 운영자가 읽는 줄: `SBMIG-RESULT run=… exit=0 build=ok … llm=3 attn=- review=ok major=0`.

## 1. 실행 흐름 — 단계별로 무엇이 일어나는가

> **Ant 프로젝트 접수(v2.1j)**: `build.xml` 이 있고 pom.xml 이 없으면 접수가 pom.xml 을 만들어 `intake: pom.xml synthesized from
> build.xml` 로 커밋한 뒤 보통 Maven 프로젝트처럼 진행한다. 소스·웹 루트·리소스·Java 수준·인코딩은 build.xml 의 javac / war 와 폴더
> 구조에서, lib 의 jar 좌표는 ① jar 안 `pom.properties` ② 이름 표(`knowledge/libraries/jar_names.yml` + 사람이 적는 `<kb>/jar_names.yml`)
> ③ sha1 저장소 조회 순으로 찾는다. 못 찾은 jar 중 소스가 쓰는 것만 `system` 범위로 선언해(as-is 위치 그대로) 빌드는 통과시키고,
> 보고서 맨 앞 "Ant 프로젝트: pom.xml 합성" 에 Nexus 에 올리는 `install-file` 명령과 함께 적는다(플래그 `pom-synthesized`,
> `jar-coordinates-unknown`). 합성 pom 의 groupId 는 자리표시자 `com.company.migrated` — 회사 설정 `intake.synth_group_id` 로 바꾼다.

로그의 모든 줄에는 단계 태그가 붙는다. 실제 한 실행의 태그 순서:

```
[intake] [analyze] [rewrite] [compile] [restructure] [review] [build] [fix r1] [fix r1 c5d089fead] [kb] [fix r2] [fix r2 7a52b1cea8] … [build] [kb] [report]
```

| 단계 (plan step id) | 하는 일 | 남기는 것 | LLM |
|---|---|---|---|
| `intake` | zip/폴더를 복사해 git 저장소로 만든다. Maven 프로젝트만 받는다(Gradle/Ant, pom 없는 트리 → exit 3). 전제 점검(JDK 21, Maven, settings). | `project/`, `profile.json` | 없음 |
| `analyze` | pom·소스 분석: 케이스 타입(pure_java / spring_legacy / spring_boot / servlet_tomcat / msa), Java·Boot 버전, 라이브러리, 권고(EOL 라이브러리 등). | `profile.json` | 없음 |
| `plan` | 아래 단계들을 결정적으로 나열한다(어떤 레시피, 어떤 재구조화 모드). | `plan.json` | 없음 (`plan.advisor: llm` 은 꺼져 있음) |
| `transform.rewrite` | OpenRewrite 한 번: `UpgradeToJava21`, (필요 시) `JavaxMigrationToJakarta`, Boot 프로젝트는 `UpgradeSpringBoot_4_0` + `SpringBootProperties_4_1`. 빌드 없음. | 커밋 `rewrite: …`, `logs/002-rewrite-*.log` | 없음 |
| `transform.compile` | `mvn clean compile` 한 번. 나온 오류 전부를 **오류 목록**에 넣는다(`seen=compile`). 고치지 않는다. | `errors.jsonl` 첫 레코드들 | 없음 |
| `restructure.*` | 회사 구조: 루트 pom → `base-parent`, `{name}-boot` / `{name}-back` / `{name}-front` 모듈, 필수 2개 의존성, 선택 8개 버전 올림(쓰는 것만). web root 의 JSP 는 front 의 `META-INF/resources/WEB-INF/jsp`, 그 밖의 모든 파일(html·js·css·이미지·mp4·exe …)은 front 의 `static/<web 경로>` 로; web root 안의 `<resource>` 디렉토리 파일은 comp classpath 에 남기고 front static 에도 **복사**; WEB-INF 의 설정 아닌 파일은 front jar 의 `META-INF/resources/WEB-INF/` 아래 같은 경로, 설정(xml/properties)은 boot 의 `asis-webinf/`. 파일이 제자리에 간 뒤 모든 JSP / 태그 파일의 javax EE 이름(page import, useBean, 스크립틀릿)을 jakarta 로 바꾼다(`layout.jsp_jakarta_rename`, v2.1h — OpenRewrite 는 JSP 를 읽지 않음; 보고서 "What we changed" 에 `JSP javax → jakarta: N files, M references`, 남은 것은 Handover). 부모를 바꾸면서 사라진 의존성 버전(옛 사내 부모가 관리하던 것)은 as-is effective pom 에서 가져와 그대로 적는다(v2.1l, `version carried over from the as-is build`, Handover ⚠ carried over). 필터링되는 리소스 아래의 바이너리 파일(웹폰트·압축·키스토어·문서, 회사 확장자 `dmdb` 등 — 내용으로도 판별)은 그 pom 의 maven-resources-plugin `nonFilteredFileExtensions` 에 확장자를 적어 Maven 필터가 바이트를 깨뜨리지 않게 한다(v2.1m, `build.non_filtered_extensions` 로 회사 확장자 추가). pom 을 고친 항목 하나하나를 **`pom-verify` 레코드**로 남긴다(다음 빌드가 검증). | 커밋 `restructure: …`, `pom-verify` 레코드 | 없음 |
| `restructure.review` | **구조 점검**: LLM 에 모든 pom·모듈 구조·소스 인코딩·필터링 대상 바이너리를 주고 회사 규칙 위반을 찾게 한다. critical 은 LLM 이 **직접 수정**(pom 만, 소스 금지) → 커밋 `review: …`; warning 은 보고서에. | `report.md` "Structure review", 오류 목록의 `review` 레코드 | **1회 (+형식 오류 시 1회, 편집 적용 실패 시 1회 재시도 — 최대 3회)** |
| `verify.build` | `mvn clean package -Dmaven.test.skip=true`. 성공이면 끝. 실패면 **새 오류만** 목록에 추가하고(`pom-verify` 중 오류 없는 pom 은 resolved) 수정 루프로. | `logs/006-build-verify.log` | 없음 |
| 수정 루프 `fix r1 … r50` | 라운드마다: 빌드 → 직전 시도 채점(되돌림·크레딧) → 새 오류 추가 → 케이스로 묶기(근본 원인 먼저, 한 라운드 최대 8케이스) → 케이스마다 사다리(§5) → 커밋. | 시도 레코드(원인·해결·수정·diff·결과), `cases/<id>/fix_vN.py` | **케이스당 최대 3회** (+보조 호출, §6) |
| 최종 빌드 | 루프가 끝난 뒤 무조건 한 번 더 `mvn clean package`. | `SBMIG-RESULT` 줄 | 없음 |
| `report` / `kb` | `report.md/json`, `errors.md`, `result.zip`, `changes.patch`; 오류 목록을 KB `errors.jsonl` 에 덧붙이고 케이스 폴더·`cases.md` 갱신. | 아래 §3 | 없음 |

루프가 끝나는 조건: BUILD SUCCESS · 50 라운드 소진 · 손댈 케이스 없음 · BLOCKED 케이스만 남음 · 240분 초과. 어느 경우든 **마지막은 빌드**다.

## 2. 케이스란 무엇이고, 왜 디렉토리 이름이 해시인가

- 빌드 오류 한 건마다 두 개의 시그니처를 만든다.
  - **L1**(구체): 같은 문제의 같은 오류. 프로젝트 소유 패키지는 `*` 로 가린다.
  - **L2**(일반): 프로젝트가 달라도 같은 종류의 문제. 예 `compile|cannot_find_symbol|BASE64Encoder`, `compile|package_does_not_exist|jakarta.xml.bind`,
    `plugin_execution|resources:resources|MalformedInputException …`. 경로·줄 번호·모듈 이름은 들어가지 않는다.
- **케이스 = L2 하나.** 한 빌드에서 같은 L2 를 가진 오류들(여러 파일)이 한 케이스로 묶여 한 번에 고쳐진다.
- **case_id = sha1(L2)[:10]** (예 `7a52b1cea8`). 디렉토리 이름을 해시로 둔 이유:
  1. L2 에는 `|`, `<`, `>`, `:`, 한글, 공백, 200자 넘는 메시지가 들어간다. 파일 시스템(특히 Windows 경로 260자 제한)에 안전한 고정 길이 키가 필요하다.
  2. 매칭은 **L2 문자열**로 한다. 폴더 이름은 키일 뿐이라 사람이 읽기 좋게 바꿔도 매칭에는 아무 도움이 안 되고, 두 사람이 다르게 이름 붙이면 깨진다.
  3. 같은 L2 면 어느 PC, 어느 프로젝트에서도 같은 폴더 → 500개 프로젝트가 한 KB 를 공유해도 충돌이 없다.
  4. 사람이 읽을 이름은 `case.json` 의 `name` / `l2` 와 `cases.md` 표에 있다. `sbmig kb cases show 7a52b1cea8` 이 한 번에 보여 준다.
- 케이스가 없는 레코드(재구조화의 `pom-verify`, 점검 warning 메모)는 `cases/_other/errors/` 에 들어간다.

## 3. KB 디렉토리와 파일

```
kb-v2/
├── errors.jsonl                 모든 실행의 모든 오류 레코드, 한 줄 = 오류 1건 (기계용 원본)
├── cases.md                     사람용 표: 케이스 · 이름 · 프로젝트 수 · seen/resolved · 스크립트(성공/실패 — 마지막 실패 이유) · 마지막 결과
├── libraries.yml                (선택) 사람이 넣는 라이브러리 표: "이 좌표는 이 버전으로", allow_major 승인  ← 재배포 없이 반영
└── cases/
    ├── 7a52b1cea8/              케이스 폴더 (case_id)
    │   ├── case.json            케이스 요약 + 스크립트 버전들(상태·성공/실패·실행 이력·왜 보강됐는지)
    │   ├── fix_v1.py            저장된 수정 스크립트 v1 (`def fix(p)`), 보강되면 fix_v2.py …
    │   └── errors/
    │       ├── run-20261028-003026-835d61-d9d7088de011.json   오류 1건 = JSON 1개 (사람이 읽는 복사본: 원인·해결·수정·as-is/to-be·diff·결과)
    │       └── run-20261028-003100-ac83d6-a1047cd7e2fc.json
    ├── 26271fddc9/ …
    └── _other/errors/           케이스 없는 레코드 (pom-verify, 점검 메모)
```

- `errors.jsonl` 은 실행이 끝날 때 **덧붙이기**만 한다. 실행 중에는 실행 디렉터리의 `errors.jsonl` / `errors.md` 에 쓴다.
- `sbmig kb reindex` 가 `errors.jsonl` 에서 `case.json`(seen/resolved/projects/실행 이력)과 `cases.md`, 빠진 에러별 JSON, 빠진 as-is/to-be 를 다시 만든다. 즉 `errors.jsonl` 이 원본, 나머지는 파생이다.
- 한 실행이 남기는 것(실행 디렉터리): `report.md` / `report.json` / `errors.md` / `errors.jsonl` / `changes.patch` / `result.zip` / `journal.jsonl` / `sbmig.log` / `logs/NNN-*.log` / `project/`.

## 4. 케이스 폴더 샘플과 속성 설명

### 4.1 `case.json` — 두 프로젝트에서 쓰인 스크립트 (실물)

```json
{
  "case_id": "7a52b1cea8",
  "l2": "compile|package_does_not_exist|jakarta.xml.bind",
  "name": "jakarta.xml.bind",
  "kind": "compile",
  "projects": ["inventory-core", "ledger-utils"],
  "seen": 5,
  "resolved": 5,
  "scripts": [
    {
      "version": 1, "file": "fix_v1.py", "status": "proven", "source": "llm-new",
      "created_by": "run-20261028-003026-835d61", "project": "inventory-core",
      "successes": 2, "failures": 0, "projects": ["inventory-core", "ledger-utils"],
      "last_result": "improved", "created_at": "2026-10-27T15:30:49Z",
      "parent": null, "reason": "", "last_failure": "",
      "runs": [
        {"run_id": "run-20261028-003026-835d61", "project": "inventory-core", "round": 1, "n": 1,
         "result": "build_ok", "reason": "", "errors_before": 23, "errors_left": 0, "errors_new": 0,
         "files": ["module/comp-jar/pom.xml"], "at": "2026-10-27T15:30:52Z"},
        {"run_id": "run-20261028-003100-ac83d6", "project": "ledger-utils", "round": 2, "n": 1,
         "result": "improved", "reason": "", "errors_before": 7, "errors_left": 4, "errors_new": 0,
         "files": ["module/comp-jar/pom.xml"], "at": "2026-10-27T15:31:26Z"}
      ]
    }
  ],
  "last_analysis": "stored fix script 7a52b1cea8 v1 (reused, no LLM call)",
  "last_solution": "",
  "updated_at": "2026-10-27T15:31:40Z",
  "last_result": "resolved 1 (r2 try 1) — ledger-utils run-20261028-003100-ac83d6"
}
```

| 속성 | 뜻 |
|---|---|
| `case_id` / `l2` / `name` / `kind` | 케이스 키(해시), 일반 시그니처, 사람용 이름(시그니처에서 뽑음), 오류 종류(compile / dependency_resolution / plugin_execution …) |
| `projects` / `seen` / `resolved` | 이 케이스가 나온 프로젝트들, 오류 건수 누적, 그중 해결된 건수 |
| `scripts[]` | 저장된 스크립트 버전들. 같은 코드는 한 번만 저장된다(sha1 비교), 번호는 재사용하지 않는다 |
| `scripts[].status` | `unverified` 만든 프로젝트에서만 통함 → `proven` **다른** 프로젝트에서도 케이스를 해결함 → `retired` 사람이 끔(`sbmig kb cases retire`) |
| `scripts[].source` | `llm-new` 처음 작성 · `llm-augment` 보강본 · `review` 구조 점검이 남김 |
| `scripts[].successes` / `failures` / `projects` / `last_result` | 채점 결과 누적: improved / build_ok 는 성공, no_change / regressed / reverted / apply_failed / blocked 는 실패 |
| `scripts[].parent` / `reason` | 보강본이면 어느 버전을 이었는지, **왜 만들었는지**(부모 버전의 실패 이유 문장, `v1: no_change: 3 of 3 targeted error(s) remained — …`) |
| `scripts[].last_failure` | 가장 최근 실패 이유 한 줄 |
| `scripts[].runs[]` | 실행 이력(최근 30건): 어느 실행·프로젝트, 몇 라운드 몇 번째 시도, 결과, **이유**, 오류 수(전 → 남음, 새로 생김), 바꾼 파일 |
| `last_analysis` / `last_solution` | 마지막 LLM 답변의 원인 / 해결 방법(재사용 실행이면 "reused, no LLM call") |
| `pin` (선택) | 사람이 `block` / `block:<이유>` 를 적으면 이 케이스는 시도하지 않고 Handover 로 넘긴다(`sbmig kb cases pin`) |

### 4.2 `fix_v1.py` — 저장된 스크립트 (실물, 앞부분)

```python
JAXB = (("jakarta.xml.bind", "jakarta.xml.bind-api", None), ("org.glassfish.jaxb", "jaxb-runtime", "runtime"))

def module_pom(p, path):
    """The pom.xml of the module that holds `path` (nearest folder upwards with a pom.xml)."""
    ...

def fix(p):
    poms = []
    for e in p.errors:                       # 이 케이스의 오류 전부 (파일·줄·메시지)
        path = e.get("file")
        if path and p.exists(path):
            pom = module_pom(p, path)
            if pom not in poms:
                poms.append(pom)
    for path in poms:
        pom = p.pom(path)
        for g, a, scope in JAXB:
            if pom.has_dependency(g, a):
                p.log(f"{path}: {g}:{a} already declared")
                continue
            pom.add_dependency(g, a, scope=scope)      # version managed by the parent (Boot BOM)
        pom.save()
```

- 규칙: 모듈 수준 `def fix(p)` 하나. 파일 접근은 전부 `p`(프로젝트 폴더 안만: `p.errors`, `p.files`, `p.read/replace/sub/write/create/delete/move`, `p.pom(path)`, `p.log`). `open`/`os`/`subprocess`/네트워크 금지(정적 검사에서 거부). 별도 `python -I` 프로세스, 60초 타임아웃.
- 멱등이어야 한다(두 번 돌려도 변화 없음) — 저장본은 다른 프로젝트에서 그대로 돌기 때문.
- 사람이 직접 고쳐도 된다. 고친 뒤 `sbmig kb reindex` 는 필요 없고, 다음 실행부터 바로 쓰인다(코드가 바뀌면 다음 저장 때 새 버전이 된다).

### 4.3 `errors/<run_id>-<entry_id>.json` — 오류 한 건의 전체 기록 (실물, diff 는 줄임)

```json
{
  "id": "8aeabf607152",
  "run_id": "run-20261028-003100-ac83d6",
  "project": "ledger-utils",
  "case_id": "26271fddc9",
  "l2": "compile|cannot_find_symbol|BASE64Encoder",
  "kind": "compile",
  "file": "module/comp-jar/src/main/java/com/example/legacy/util/Codec.java",
  "line": 7,
  "module": "ledger-utils-comp",
  "message": "cannot find symbol",
  "raw": "[ERROR] module/comp-jar/src/main/java/com/example/legacy/util/Codec.java:[7,16] cannot find symbol\n  symbol:   class BASE64Encoder\n  location: package sun.misc",
  "seen": "fix r2",
  "status": "resolved",
  "tries": [
    {
      "n": 1, "round": 3, "source": "llm-new", "script_version": 1,
      "analysis": "sun.misc.BASE64Encoder / BASE64Decoder are JDK internals that javac 21 no longer exposes … ports every call to java.util.Base64 …",
      "solution": "",
      "no_script_reason": "",
      "applied": "script",
      "files": ["module/comp-jar/src/main/java/com/example/legacy/util/Codec.java"],
      "edits": [
        {
          "path": "module/comp-jar/src/main/java/com/example/legacy/util/Codec.java",
          "where": "", "change": "Codec.java 6행 수정, 23행 수정, 33행 수정", "lines": "~6, ~23, ~33", "source": "diff",
          "hunks": [
            {"lines": "~6",  "before": "import sun.misc.BASE64Decoder;\nimport sun.misc.BASE64Encoder;", "after": "import java.util.Base64;", "truncated": false},
            {"lines": "~23", "before": "            return new BASE64Encoder().encode(text.getBytes(CHARSET));", "after": "            return Base64.getEncoder().encodeToString(text.getBytes(CHARSET));", "truncated": false},
            {"lines": "~33", "before": "        byte[] raw = new BASE64Decoder().decodeBuffer(base64);", "after": "        byte[] raw = Base64.getMimeDecoder().decode(base64);", "truncated": false}
          ]
        }
      ],
      "major_upgrades": [],
      "verify": "", "verify_source": "",
      "diff": "diff --git a/module/comp-jar/src/main/java/… (unified diff, 6000자까지; 전체는 실행 디렉터리의 changes.patch)",
      "commit": "454af95dbc058c62989f3d241e773de2e660d62d",
      "result": "build_ok",
      "note": "",
      "llm_calls": 1,
      "seconds": 2.2
    }
  ],
  "resolved": "r3 try 1",
  "script": "cases/26271fddc9/fix_v1.py",
  "note": "",
  "at": "2026-10-27T15:31:23Z"
}
```

| 속성 | 뜻 |
|---|---|
| `id` | `sha1(run_id | case_id | file | line | message)[:12]` — 파일 이름의 뒷부분 |
| `run_id` / `project` | 어느 실행, 어느 프로젝트 |
| `case_id` / `l2` / `kind` | 케이스 키 · 일반 시그니처 · 종류. 케이스 없는 레코드는 `""` 이고 `_other/` 에 들어간다 |
| `file` / `line` / `module` / `message` / `raw` | 오류 위치와 Maven 로그 원문(600자까지) |
| `seen` | 처음 나온 단계: `compile`(OpenRewrite 직후 컴파일) · `restructure`(pom-verify) · `review` · `build`(첫 package) · `fix rN`(N라운드 빌드에서 새로 나옴) |
| `status` | `open` → `resolved` / `blocked` / `unresolved`; `pom-verify`(재구조화가 고친 pom, 다음 빌드가 검증) · `note`(점검 warning) |
| `tries[]` | 이 오류를 품은 케이스에 대한 시도들. 한 시도는 케이스의 모든 오류 레코드에 똑같이 기록된다 |
| `tries[].source` | `kb-script` 저장본 실행(LLM 0회) · `llm-new` 새 답변 · `llm-augment` 보강 답변 · `llm-edit` 스크립트 없이 직접 수정 · `review` 구조 점검 |
| `tries[].script_version` | 돌린/만든 저장 버전(0 = 없음) |
| `tries[].analysis` / `solution` / `no_script_reason` | LLM 의 **원인** / **해결 방법** / 스크립트로 못 만드는 **이유** |
| `tries[].applied` / `files` / `commit` | 무엇이 적용됐나(`script` / `edits` / `none`), 바뀐 파일, git 커밋 |
| `tries[].edits[]` | **수정 기록**: 파일 · 위치(`where`, LLM 이 쓴 것) · 무엇을 어떻게(`change`) · 줄 범위(`lines`: `~12` 바뀜, `+42–47` 추가, `-3` 삭제) · `source`(llm / diff) |
| `tries[].edits[].hunks[]` | **as-is / to-be 코드**: 바뀐 구역마다 `before`(원래 줄들) / `after`(바뀐 줄들). 캡 없음(`errors.snippet_lines: 0`), 비밀값은 마스킹 |
| `tries[].major_upgrades[]` / `verify` / `verify_source` | 이 시도가 의존성 **메이저를 올렸거나 라이브러리를 바꿨으면** 그 목록과 운영자 확인 가이드(LLM `<verify>` 또는 sbmig 생성) — §7 |
| `tries[].diff` | 적용된 unified diff(6000자 캡) |
| `tries[].result` / `note` | 채점: `improved`(이 케이스 오류 전부 사라짐) · `build_ok` · `partial` · `no_change` · `regressed`(새 오류 → 되돌림 `reverted`) · `inconclusive`(빌드가 판단 못 함) · `apply_failed` · `blocked`; `note` 는 실패·되돌림 설명 |
| `tries[].llm_calls` / `seconds` | 이 시도의 LLM 호출 수와 소요 시간 (구조 점검이 한 번에 고친 critical 여러 건은 점검의 호출 수를 첫 항목에만 싣고 나머지는 0) |
| `resolved` | 무엇이 해결했나: `r3 try 1`(3라운드 1번째 시도) · `build`(시도 없이 다른 수정으로 사라짐) · `review` |
| `resolved_by[]` | `build` 로 닫힌 오류가 **누구 덕에 사라졌는지**: 같은 라운드에 이 파일(또는 이 모듈의 pom)을 바꾼 시도 — `case_id` · `round` · `n` · `commit` · 맞은 파일 · `match`(file / pom / reindex) · 한 줄 설명(`7a52b1cea8 r1 try 1 — module/comp-jar/pom.xml +28–36 — jakarta.xml.bind-api 추가`). 표와 `cases.md` 에는 `build ← 7a52b1cea8 r1 try 1` 로 보인다 |
| `script` | 해결한 저장 스크립트 경로 |

사람이 읽기엔 `sbmig kb errors show 8aeabf607152` 가 같은 내용을 이렇게 보여 준다:

```
entry 8aeabf607152  [resolved]  case 26271fddc9  seen fix r2  resolved r3 try 1
  module/comp-jar/src/main/java/com/example/legacy/util/Codec.java:7  cannot find symbol
  tries: 1 llm-new v1 script build_ok
  try 1 (round 3, llm-new v1, applied script, result build_ok, commit 454af95dbc, 1 LLM call(s), 2s)
    원인: sun.misc.BASE64Encoder / BASE64Decoder are JDK internals that javac 21 no longer exposes …
    수정:
      …/Codec.java ~6, ~23, ~33 — Codec.java 6행 수정, 23행 수정, 33행 수정
        as-is (~6):  import sun.misc.BASE64Decoder; / import sun.misc.BASE64Encoder;
        to-be (~6):  import java.util.Base64;
        as-is (~23): return new BASE64Encoder().encode(text.getBytes(CHARSET));
        to-be (~23): return Base64.getEncoder().encodeToString(text.getBytes(CHARSET));
```

### 4.4 `errors.jsonl`, `cases.md`, `libraries.yml`

- `errors.jsonl`: 위 JSON 과 같은 레코드가 한 줄에 하나. 실행이 끝날 때 실행 디렉터리의 목록이 통째로 덧붙는다. 기계용 원본.
- `cases.md` (실물 한 줄): `| 7a52b1cea8 | jakarta.xml.bind | 2 | 5 / 5 | v1 proven 2/0 | - | resolved 1 (r2 try 1) — ledger-utils run-… |`
  — 스크립트 칸은 `v2 unverified 0/1 — no_change: 3 of 3 targeted error(s) remained …` 처럼 **마지막 실패 이유**까지 보여 준다.
- `libraries.yml` (사람이 작성, 예시 `knowledge/libraries/libraries.company.example.yml`):

```yaml
version: 1
libraries:
  - key: opensymphony:sitemesh          # 같은 메이저(2.x)의 jakarta 릴리스로 고정
    jakarta_since: "2.6.0"
    version: "2.6.0"
    note: SiteMesh 2.6.0 is the jakarta.servlet build of the 2.x line; packages and the filter class are unchanged.
  - key: org.sitemesh:sitemesh          # 메이저 업그레이드를 사람이 승인
    jakarta_since: "3.2.0"
    version: "3.3.0"
    allow_major: true
```

## 5. 케이스는 어떻게 쓰이나 — 사다리와 채점

한 케이스, 한 실행에서:

```
KB 탐색 (L2 → cases/<id>/case.json)
 ├─ 저장본 있음 → 가장 좋은 버전 실행 (kb-script, LLM 0회)
 │     ├─ 재빌드에서 improved/build_ok → 성공 +1, 다른 프로젝트였으면 proven
 │     └─ 실패 → 되돌리고 LLM 에 "보강" 요청 (프롬프트에 스크립트 + 무엇을 했고 뭐가 남았는지)
 │           ├─ 새 스크립트 → 실행·저장 (fix_v2.py, parent=1, reason="v1: no_change: …")
 │           └─ <no_script> + edits → 이 프로젝트만 직접 수정 (llm-edit)
 └─ 저장본 없음 → LLM 에 "새 수정" 요청 (llm-new)
       → <edits> 바로 적용 + <script> 저장(unverified); 스크립트만 왔으면 실행
```

- 케이스당 한 실행에서 **저장본 1회 + LLM 3회**(`script.max_augments_per_case`)까지. 그 뒤 `BLOCKED (repeated_failure)` → Handover.
  `script.reuse_versions` 를 2 이상으로 올리면, 이번 실행에서 실패한 저장본 다음에 **다음 저장본**을 LLM 전에 더 돌린다(같은 순서: proven 최신 → unverified 최신, retired 제외; 도움이 된 재사용 — improved / partial — 이 있었으면 다음 저장본 대신 그것을 보강).
- 특정 이유는 더 짧다(`loop.blocked_policy`): javax 라이브러리·호환 안 되는 외부 API·없는 아티팩트·플러그인 2회, 사내 parent 미해결·인코딩·지원 안 하는 소스 0회(바로 Handover).
- 채점은 **재빌드**로만 한다. `improved` = 이 케이스의 오류가 모두 사라졌고 빌드가 그 단계까지 갔음. 나아지지 않으면 다음 시도 전에 되돌린다(HEAD 면 reset, 아니면 `git revert`). 새 오류가 생기면 `regressed → reverted`.
- 한 라운드에 근본 원인 케이스부터(의존성 해결 → 컴파일 → 플러그인), 같은 파일을 이미 건드린 케이스는 다음 라운드로 미룬다.
- 저장 스크립트의 `runs[]` 에 매 실행의 결과와 **이유**가 남으므로, 사람이 "왜 v2 가 생겼나 / v1 은 어디서 깨졌나" 를 JSON 만 보고 알 수 있다.

## 6. LLM 은 어디서 개입하고, 한도는 어떻게 되나

| 개입 지점 | 무엇을 주고 받나 | 호출 수 |
|---|---|---|
| 구조 점검 (`restructure.review`) | 모든 pom(60,000자까지, 40개까지) + 모듈 구조 + 인코딩 + sbmig 가 계산한 사실 → 판정 JSON + critical 수정(`<edits>`) | 프로젝트당 **1회**, 답변 형식이 깨지면 **1회 재시도**, 고친 편집이 적용되지 않으면(되돌린 뒤) 실패 이유를 주고 **1회 더** — 최대 **3회**(`_ask` 2 + `_apply_retry` 1) |
> **`fix.mode: edits` (v2.1i, 토큰 비교용 스위치)**: LLM 에게 수정 내용(`<edits>`)만 요청한다 — 프롬프트에서 스크립트 규칙·도우미 API 가
> 빠지고(약 36% 짧음), 스크립트 저장·재사용·보강이 없다. 케이스당 LLM 요청 횟수는 기본 **무제한**(`fix.edits_max_asks_per_case: 0` —
> "전부 LLM 에게 맡겼을 때" 를 재는 용도; 라운드 50·실행 시간·`loop.max_llm_calls` 만 남고 `loop.blocked_policy` 의 양수 횟수도 풀린다.
> 3 으로 두면 script 모드와 같은 횟수)
> 맞췄다. 웹 UI 의 "LLM 직접 수정만" 체크박스, `sbmig run --fix-mode edits`, `sbmig fleet run --fix-mode edits`. "KB 사용 안 함"
> (`--no-kb`)까지 켜면 "전부 LLM" 실험이 된다. 결과 구분: `SBMIG-RESULT … fix=edits`, 보고서 "LLM usage and cost" 의 `fix mode` 줄.

| 수정 루프 — 새 수정 (`llm-new`) | 케이스의 오류들, 오류 파일(창 단위), pom 발췌, "Dependency versions"(저장소 버전 사실), KB 힌트, 이전 시도 → 원인 / 해결 / `<edits>` / `<script>` | 케이스당 **1회**; LLM 이 파일을 더 달라면(`<need_files>`) 최대 **2회** 추가(`llm.max_context_rounds`); 스크립트가 못 쓰는 형식이면 **1회** 피드백 |
| 수정 루프 — 보강 (`llm-augment`) | 위 + 저장 스크립트 코드와 실행 결과 | 케이스당 최대 **3회**(새 수정 포함 총 3) |
| 운영자 가이드 (`verify_guide`) | 메이저 업그레이드가 읽혀 나왔는데 답변에 `<verify>` 가 없을 때. 요청 role 은 `verify_guide`(v2.1h 부터 정식 `LLMRole`; 재생 파일은 `replay_role: verify_guide`) | 그 시도당 **1회**(저장본 재사용 시도는 0회, sbmig 가 생성) |
| 에스컬레이션 모델 | 같은 오류 집합이 2번 반복(`loop.stall_limit`)되거나 케이스의 2번째 보강부터 | 호출 수는 같고 모델만 `llm.escalation_model` 로 |

LLM 이 **개입하지 않는** 곳: intake · analyze · plan · OpenRewrite · compile · restructure(회사 구조) · 빌드 · 채점/되돌림 · 저장본 재사용 · 보고서 · KB 적재.

실행 전체 한도:

| 항목 | 값 | 설정 키 |
|---|---|---|
| 빌드 라운드 | 50 | `loop.phases.verify.max_iterations` |
| 프로젝트당 LLM 호출 | **무제한**(0) · 양수면 상한 · -1 이면 LLM 없이 저장본만 | `loop.max_llm_calls` |
| 플릿 전체 LLM 호출 | 무제한(0) | `fleet.max_llm_calls_total` |
| 실행 시간 | 240분 | `loop.max_total_minutes` |
| 한 라운드에 다루는 케이스 | 8 | `loop.max_clusters_per_iteration` |
| 케이스당 LLM 시도 | 3 | `script.max_augments_per_case` |
| 케이스당 저장본 재사용 | 1 버전 (2 이상이면 실패한 저장본 다음 버전도) | `script.reuse_versions` |
| 스크립트 실행 | 60초, 바뀐 파일 40개 넘으면 경고 | `script.timeout_seconds`, `script.max_files_changed_warn` |
| Maven 빌드 / OpenRewrite | 30분 / 40분 | `build.timeout_minutes`, `openrewrite.timeout_minutes` |
| LLM 요청 | 180초, 재시도 3, 출력 8,000 토큰, 프롬프트 60,000자, 전송 실패 2연속이면 60초 쉬고 재시도(한 번) | `llm.*` |

실제 샘플 실행(`ledger-utils`, 오류 8건 · 케이스 5개): LLM 호출 **3회**(점검 1 + 새 수정 2), 저장본 재사용 1회, 빌드 4회, 전체 40초.

## 7. 의존성 버전 정책 (v2.1d) — 메이저는 올리지 않는다

- LLM 규칙: 의존성의 **메이저를 올리거나 라이브러리를 바꾸지 않는다**. javax 라이브러리는 **같은 메이저의 jakarta 빌드**로만 올린다.
- sbmig 가 검색한다: 오류 뒤의 라이브러리를 classpath 에서 찾아 Nexus(`settings.xml` mirror)의 `maven-metadata.xml` 과 후보 pom 을 읽고,
  프롬프트에 넣는다. 로그 `의존성 버전 조회: opensymphony:sitemesh 2.4.2 → 같은 메이저 2.5.0, 2.6.0 (jakarta ✓) / 상위 3.3.0 (jakarta ✓)`.
  마지막 줄은 판정이다: `→ verdict: raise to 2.6.0 (same major 2.x, jakarta build) — no <verify> needed` 또는
  `→ verdict: the 1.x line has NO jakarta build, so a major upgrade to 4.2.1 is allowed here … write <verify>` (QLRM 같은 경우).
- 그래도 메이저가 바뀌면 **막지 않는다**(빌드는 통과해야 하니까). 대신 pom diff 를 읽어 내서 운영자 가이드를 남긴다:
  `errors.jsonl` 의 `major_upgrades` / `verify`, `report.md` Handover 맨 위 **"운영자 확인 필요 (메이저 업그레이드 N건)"**, `SBMIG-RESULT … major=N`.
- 사람이 미리 정해 둘 수 있다: `kb-v2/libraries.yml` 에 "이 좌표는 이 버전으로" 또는 `allow_major: true`. 코드 재배포 없음.

### 7.1 버전은 잃지도, 추측하지도 않는다 (v2.1l, 2026-10-30)

현업에서 "의존성만 추가하다가 끝나고, 마지막엔 LLM 이 '정책상 버전을 추정하여 추가할 수 없습니다' 라고 답한다" 는 실행이 여럿 나왔다.
원인은 두 가지가 겹친 것: ① 재구성이 pom 의 부모를 `comp-jar` / `boot-jar` 로 바꾸면 **옛 사내 부모가 관리하던 버전이 사라지는데**
v1 의 이어받기 핸들러가 v2 4단계에서 같이 지워졌고, ② 규칙 14 가 버전 추측을 금지하니 LLM 도 채울 수 없었다. 이제 한 가지 순서로 푼다.

1. 새 부모 체인이 관리한다(재구성 뒤 effective pom) → 버전 없이 둔다.
2. as-is 빌드가 쓰던 버전이 있다(`logs/depmgmt.json`, 접수 때 캡처) → 그 버전을 적는다(이어받기, Handover ⚠ carried over).
3. LLM 이 **새로 추가한** 의존성만: 저장소(사내 Nexus → settings 미러 → Central)에서 정식 릴리스 중 최신.
4. 어디에도 없다 → 비워 두고, 빌드의 `version … is missing` 이 케이스가 되어 루프가 다시 2–3 을 시도한다; 끝내 못 찾으면 Handover 가 좌표를 적는다.

어디서 도는가: 재구성 안(첫 빌드 전, `versions_pinned`; 새 부모 체인의 effective pom 을 못 얻으면 되돌림 — Boot 2.7 스타터를 2.7 로
박아 두는 사고를 막기 위해), 수정 루프의 **LLM 0회 1차 시도 `sbmig-version`**(`missing_version` 케이스, 사다리 앞), 그리고 모든 수정
시도가 적용된 직후(`version_filled` 저널). 프롬프트의 "Dependency versions" 절은 이제 `as-is build: g:a <v> (managed by …)` /
`new parent chain: manages g:a at <v>` 줄로 시작하고, 규칙 14 는 "모르는 버전은 `null` 로 두어라, sbmig 가 채운다 — 버전을 몰라서 수정을
거부하지 말라" 로 바뀌었다. 구조 검토는 박아 넣은 목록을 재료로 받는다.

## 8. 운영자가 보는 것, 사람이 KB 를 고치는 법

- 콘솔/로그 마지막 줄: `SBMIG-RESULT run=<id> exit=0|2|3 build=ok|FAIL unresolved=N blocked=N open=N resolved=N llm=N attn=<flags> review=ok|issues|skipped|failed major=N`
  - `exit 0` 빌드 성공 · `2` 빌드 실패(BLOCKED/미해결 있음) · `3` 입력 거부(Gradle, pom 없음, JDK 아님).
- `report.md` 순서: Handover(운영자 확인 필요 → 교체된 버전 → 주의 라이브러리 → 이제부터 할 일) → Error list(단계별 표) → Fixes by case → Structure review → Blocked → Manual follow-ups → Flags → What we changed → LLM usage and cost → Outputs.
- `errors.md`: 오류 표(단계·상태·케이스·파일·시도 요약) + 시도별 원인/해결/수정 줄.
- KB 명령: `sbmig kb stats` · `kb cases list` · `kb cases show <id>`(스크립트 코드·parent·reason·runs) · `kb errors list` · `kb errors show <id>`(as-is/to-be 포함) ·
  `kb cases retire|activate <id>`(스크립트 끄기/켜기) · `kb cases pin <id> block:<이유>`(이 케이스는 사람 몫) · `kb reindex` · `kb export|import`.
- 사람이 개선하는 방법: ① `fix_vN.py` 를 직접 고친다(다음 실행부터 쓰임). ② 나쁜 버전은 `retire`. ③ 라이브러리 지식은 `libraries.yml`. ④ 케이스 폴더를 지우고 `kb reindex`(기록은 `errors.jsonl` 에서 복원, 스크립트만 사라짐).

## 9. 자주 나오는 질문

- **폴더 이름을 예외 이름으로 바꾸면 매칭이 좋아지나?** 아니다. 매칭은 L2 로 한다(§2). 폴더는 키다. 이름은 `cases.md` 와 `case.json` 에 있다.
- **첫 프로젝트인데 "KB match" 가 떴다?** 지금 로그는 탐색 **결과**를 말한다: `KB 탐색: 저장된 스크립트 없음 (처음 보는 케이스) → LLM 에 새 수정 요청` / `KB 탐색: 저장된 스크립트 v1 (proven, 성공 2 / 실패 0, 프로젝트 2) → 실행`.
- **케이스에 스크립트도 시도도 없는데 seen 4 / resolved 4 인 건?** 다른 케이스의 수정으로 같은 라운드에 함께 사라진 오류다(예: `does not override
  renderMergedOutputModel` 은 같은 파일의 javax → jakarta import 케이스가 적용되자 사라진다). `resolved: build` 이고 `resolved_by` 가 그 시도를 가리킨다
  (`cases.md`: `resolved 4 (build ← 7a52b1cea8 r1 try 1)`). 과거 기록은 `sbmig kb reindex` 가 채운다.
- **스크립트가 실패하면?** 되돌리고 LLM 에 보강을 요청한다. 실패 이유는 `case.json` 의 `scripts[].runs[].reason` 과 `last_failure`, 그리고 보강본의 `reason` 에 남는다.
- **LLM 이 코드를 막 고치나?** 오류가 난 자리만, 기록을 남기며, 되돌릴 수 있게. 규칙(리팩터링 금지, 로직 삭제 금지, 메이저 금지)은 프롬프트에, 가드(프로젝트 폴더 밖 금지, 재빌드 채점, 되돌림)는 코드에 있다. 동작 변화는 빌드가 못 잡으니 수정 기록(as-is/to-be)을 사람이 본다.
- **빌드는 됐는데 런타임이 걱정이면?** Handover 의 "운영자 확인 필요"와 `MIGRATION-TODO` 주석, `Manual follow-ups` 를 본다. 런타임은 as-is 담당팀 몫이라는 게 회사 결정이다.
