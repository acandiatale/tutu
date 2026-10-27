# missing-artifact fixture (scenario X3: dependency not in any repository — blocked)

`pure-java`-like plain Java 8 jar (commons-lang3 3.9, JUnit 4.12) that declares
**`com.company.nonexistent:ghost-lib:1.2.3`** — a library that only ever existed in a decommissioned company
repository. It is not on Maven Central and is never installed into `~/.m2`, so during migration Maven reports
`Could not find artifact com.company.nonexistent:ghost-lib:jar:1.2.3 in central (…)` (or, after a first failed
lookup, `… was not found in https://repo.maven.apache.org/maven2 during a previous attempt. This failure was cached …`).

## Build as-is

The as-is environment had the old repository. Simulate it with a throwaway repository that is chained *behind* the
local repo (`maven.repo.local.tail`, Maven ≥ 3.9) — it never touches `~/.m2`:

```bash
fixtures/libs/ghost-lib/make-repo.sh /tmp/ghost-repo
JAVA_HOME=/Users/acandiatale/Library/Java/JavaVirtualMachines/corretto-1.8.0_362/Contents/Home \
  mvn -q -DskipTests -Dmaven.repo.local.tail=/tmp/ghost-repo package
```

i.e. the standard `mvn -q -DskipTests package` plus `-Dmaven.repo.local.tail=…`. Without it the build fails in
dependency resolution — which is exactly what sbmig sees. **Never** `mvn install` `fixtures/libs/ghost-lib`.

## Expected sbmig outcome (v2, asserted by `scripts/e2e.py`, see `expected.json` → `expect`)

**Blocked** `artifact_not_found` `com.company.nonexistent:ghost-lib` (case `dependency_resolution|com.company.nonexistent:ghost-lib`)
after the blocked policy's 2 fix-script requests, which the replayed LLM answers without a script (a script cannot make
the artifact exist, and the LLM must not invent a version); `success = false`, exit code **2**, 3 LLM calls (review +
2), and the run stays short: `max_duration_s` 240 in `expected.json`.
