# fw-transitive-note fixture (framework only transitive and `framework.transitive: note`; full migration)

* `packaging` war, Java 8, no `<parent>`; declares only `com.example.fw:example-fw-web:1.0.0`, which depends on
  `com.example.fw:example-fw-core:1.0.0` (`fixtures/libs/example-fw`, installed by `fixtures/libs/install.sh`), and
  `javax.servlet:javax.servlet-api` 3.1.0 `provided`
* `GreetingServlet` uses `FwPaging` (web) and `FwResult` (core); the rest as `fw-direct` (filter, service, JSP)
* `fw-transitive` and `fw-transitive-note` have the same tree; only `sbmig.e2e.yml`, `expected.json`, this README and
  `replay/` differ
* `sbmig.e2e.yml`: `framework.artifacts: ["com.example.fw:example-fw-core"]`, `framework.transitive: note`

## Build as-is

```bash
fixtures/libs/install.sh            # once: example-fw-web + example-fw-core into the local repository
JAVA_HOME=/Users/acandiatale/Library/Java/JavaVirtualMachines/corretto-1.8.0_362/Contents/Home \
  mvn -q -DskipTests package
```

## Expected sbmig outcome (asserted by `scripts/e2e.py`, see `expected.json` → `expect`)

Flag `framework-project` (journal `framework_project`, evidence `transitive`, scope **`full`**) and the ordinary
v2 migration: restructure into `module/comp-jar` + `module/boot-jar` under the configured parent, structure review
(replayed, `ok`), success, exit 0, 1 LLM call (the review).

Since v2.1z-aw the `framework_project` and `plan` events carry `name: Example` and `fix_loop: false` here too (the
values the configuration gives); the run is a full migration, so `report.md` titles the block `## Example framework`
and the fix loop is available as for any project.
