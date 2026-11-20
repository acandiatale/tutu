# fw-transitive fixture (framework project, transitive dependency; reduced scope `jdk-jakarta`)

* `packaging` war, Java 8, no `<parent>`; declares only `com.example.fw:example-fw-web:1.0.0`, which depends on
  `com.example.fw:example-fw-core:1.0.0` (`fixtures/libs/example-fw`, installed by `fixtures/libs/install.sh`), and
  `javax.servlet:javax.servlet-api` 3.1.0 `provided`
* `GreetingServlet` uses `FwPaging` (web) and `FwResult` (core); the rest as `fw-direct` (filter, service, JSP)
* `fw-transitive` and `fw-transitive-note` have the same tree; only `sbmig.e2e.yml`, `expected.json`, this README and
  `replay/` differ
* `sbmig.e2e.yml`: `framework.artifacts: ["example-fw-core"]`, `framework.transitive` default `apply`,
  `framework.recipe_sets: [jdk21, jakarta, servlet_jakarta_10]` (the Servlet 6 removals run with the jakarta set)

## Build as-is

```bash
fixtures/libs/install.sh            # once: example-fw-web + example-fw-core into the local repository
JAVA_HOME=/Users/acandiatale/Library/Java/JavaVirtualMachines/corretto-1.8.0_362/Contents/Home \
  mvn -q -DskipTests package
```

## Expected sbmig outcome (asserted by `scripts/e2e.py`, see `expected.json` → `expect`)

Flag `framework-project`, journal `framework_project` with scope `jdk-jakarta` and evidence `transitive` whose `via`
starts at `example-fw-web` and ends at `example-fw-core` (found by `dependency:tree` at analyze, nothing declares the core);
otherwise as `fw-direct`: 3 plan steps, no restructure / review, success, exit 0, 0 LLM calls, `example-fw-web` still at
1.0.0, no `<parent>` added. The transform runs `UpgradeToJava21`, `JavaxMigrationToJakarta` and `RemovalsServletJakarta10`.

Since v2.1z-aw (`framework.fix_loop: false`, set explicitly in `sbmig.e2e.yml` since v2.1z-bi made `true` the default) the journal events `framework_project` and `plan` carry the
framework's name `Example` (derived from `example-fw-core`) and `fix_loop: false`; the run builds once and would hand
any build error over without a fix loop (no `verify_loop_start`, no `plan_advice`, no `fixer` event). `report.md` opens
the block `## Example framework project` with "**Example framework projects get JDK 21 and Jakarta only.**" and lists
"one build" as done, "automatic build error fixes" as not done. The failing counterpart is `fw-build-error`.
