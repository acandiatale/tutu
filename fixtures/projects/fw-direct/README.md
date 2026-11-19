# fw-direct fixture (framework project, direct dependency; reduced scope `jdk-jakarta`)

* `packaging` war, Java 8 (`maven.compiler.source/target` 1.8), no `<parent>`, **no `project.build.sourceEncoding`** and
  no compiler `<encoding>`
* declares the framework core stand-in `com.example.fw:example-fw-core:1.0.0` itself (`fixtures/libs/example-fw`,
  installed by `fixtures/libs/install.sh`) and `javax.servlet:javax.servlet-api` 3.1.0 `provided`
* `GreetingServlet` / `AuditFilter` on `javax.servlet` (registered in `web.xml`), `GreetingService` uses `FwStrings` /
  `FwResult`
* `src/main/webapp/index.jsp`: `<%@ page import="javax.servlet.http.HttpSession" %>` and a scriptlet with
  `javax.servlet.http.Cookie[]`
* `src/main/java/com/example/fwapp/web/NoticeText.java` is saved in **MS949** (written with Python
  `encoding="cp949"`): Korean in a comment, in the string literal `"안녕하세요, 공지사항입니다"` and in an enum name
  (`등급 { 일반, 긴급 }`, so a wrong encoding breaks the build loudly). Its `fromRequest(HttpServletRequest)` imports `javax.servlet.http.HttpServletRequest`, so the jakarta
  recipe must parse the MS949 file; `expected-migrated/NoticeText.java.ms949` is the expected result (same bytes, only
  the import on jakarta), compared byte for byte after the run. Do not open-and-save it with a UTF-8 editor. Only this
  fixture has it (`fw-parent` shares the other sources).
* `sbmig.e2e.yml`: `framework.artifacts: ["example-fw-core"]` (artifactId-only pattern)

## Build as-is

```bash
fixtures/libs/install.sh            # once: example-fw-core into the local repository
JAVA_HOME=/Users/acandiatale/Library/Java/JavaVirtualMachines/corretto-1.8.0_362/Contents/Home \
JAVA_TOOL_OPTIONS=-Dfile.encoding=MS949 \
  mvn -q -DskipTests package
```

`JAVA_TOOL_OPTIONS` reproduces a Korean Windows PC (platform encoding MS949). Without it the build fails
(`unmappable character for encoding UTF-8`, `illegal character: '\ufffd'` in `NoticeText.java`) — what JDK 21 does
too, since its default is UTF-8 (JEP 400).

## Expected sbmig outcome (asserted by `scripts/e2e.py`, see `expected.json` → `expect`)

Flag `framework-project`, journal `framework_project` with scope `jdk-jakarta` and evidence `direct` (`pom.xml`); the plan
has exactly `transform.rewrite`, `transform.jsp_jakarta`, `verify.build` (journal `plan`, scope `jdk-jakarta`); no
`restructure` / `structure_review` event. Success, exit 0, **0 LLM calls**. The pom keeps packaging war, no parent, no
modules, `example-fw-core` at 1.0.0; the servlet API and the sources / JSP are on jakarta; no `*-boot` / `*-back` /
`module/` directory. After the recipes the reduced run restores the as-is `provided` scope of the servlet API (journal
`scopes_restored`; the WAR has no servlet-api jar) and writes `project.build.sourceEncoding` MS949 into `pom.xml`
(journal `source_encodings`); the encoding is written BEFORE OpenRewrite (journal order `source_encodings` → `rewrite` → `scopes_restored` →
`jsp_jakarta`), so the recipes parse `NoticeText.java` (no `rewrite_parse_problems`) and its import moves to jakarta
with the Korean bytes intact. `report.md` mentions no Spring Boot version.

Since v2.1z-aw (`framework.fix_loop: false`, the default) the journal events `framework_project` and `plan` carry the
framework's name `Example` (derived from `example-fw-core`) and `fix_loop: false`; the run builds once and would hand
any build error over without a fix loop (no `verify_loop_start`, no `plan_advice`, no `fixer` event). `report.md` opens
the block `## Example framework project` with "**Example framework projects get JDK 21 and Jakarta only.**" and lists
"one build" as done, "automatic build error fixes" as not done. The failing counterpart is `fw-build-error`.
