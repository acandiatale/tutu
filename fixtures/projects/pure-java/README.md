# pure-java fixture (case 1: in-house pure Java library)

Plain Java 8 jar, no Spring, no servlet API.

* `maven.compiler.source/target` = 1.8, `maven-compiler-plugin` 3.8.1
* `commons-lang3` 3.9 and `guava` 28.0-jre (both **optional** artifacts in `config/artifacts.local-e2e.yml` → bump path)
* `junit:junit` 4.12 tests → JUnit4→5 migration target
* `XmlExporter` / `StockItem` use `javax.xml.bind` (JAXB). JAXB was removed from the JDK in 11, so after
  `UpgradeToJava21` the migration must add `jakarta.xml.bind-api` + runtime (OpenRewrite
  `AddJaxbDependencies` / jakarta migration) or the LLM must fix `package javax.xml.bind does not exist`.
* `DateUtil` uses `java.util.Date` / `SimpleDateFormat` (compile-neutral, runtime-owner concern).
* `InventoryCli` has a `public static void main` (psvm, not a Spring Boot main class).

## Build as-is

```bash
JAVA_HOME=/Users/acandiatale/Library/Java/JavaVirtualMachines/corretto-1.8.0_362/Contents/Home \
  mvn -q -DskipTests package
```

Expected migration hot spots: JAXB removal (`javax.xml.bind`), source/target 1.8 → release 21,
compiler plugin 3.8.1 → ≥ 3.13, JUnit 4 → 5, optional bumps (commons-lang3, guava), generated `Application`.

## Expected sbmig outcome (v2, asserted by `scripts/e2e.py`, see `expected.json` → `expect`)

Success, exit 0, **2 LLM calls** (review + one new fix script; `max_llm_calls` 3). The build fails on JAXB
(`package jakarta.xml.bind does not exist`); the root case (`compile|package_does_not_exist|jakarta.xml.bind`,
`7a52b1cea8`) gets a script that adds `jakarta.xml.bind-api` + `jaxb-runtime` to `module/comp-jar/pom.xml` without
versions (the parent manages them) → `build_ok`. The other cases of that round (`jakarta.xml.bind.annotation` in
`StockItem.java`, `9648fb67cb`, …) are deferred — their module's pom was just changed — and vanish with the rebuild.
commons-lang3 / guava are bumped. The run fills the shared KB `out/e2e/kb-jaxb` that `java6-legacy` reuses (reuse
scenario, fixtures/projects/README.md).
