# fw-pom-ms949 fixture (a pom saved as MS949 under a UTF-8 declaration; v2.1z-ay)

* `packaging` jar, Java 8, declares the framework core stand-in `com.example.fw:example-fw-core:1.0.0`
  (`fixtures/libs/example-fw`, installed by `fixtures/libs/install.sh`)
* `pom.xml` starts with `<?xml version="1.0" encoding="UTF-8"?>` but its Korean comment, `<name>` and `<description>`
  are saved as **MS949** (all characters inside EUC-KR). Maven builds it without a warning (the name prints
  garbled); OpenRewrite stops with `Failed to parse pom: Invalid UTF-8 middle byte …` and converts nothing.

> `pom.xml` is intentionally **not UTF-8**. Do not open-and-save it with a UTF-8 editor;
> `tests/fixtures/test_fixture_projects.py` asserts it fails to decode as UTF-8 and decodes as cp949.

* `src/main/java/com/example/greet/Greeting.java` uses `new Integer(5)`, which the Java 21 recipes replace
* `sbmig.e2e.yml`: `framework.artifacts: ["example-fw-core"]`, `report.language: ko`

## Expected sbmig outcome (asserted by `scripts/e2e.py`, see `expected.json` → `expect`)

Before the first OpenRewrite run sbmig writes the declaration `EUC-KR` (only the declaration's bytes change): commit
`transform: pom encodings corrected`, journal `pom_encodings`, flag `pom-encoding-corrected`, one hand-over line. Then
`rewrite OK` with changed files (`Integer.valueOf(5)`, `maven.compiler.source/target` 21), one passing build, exit 0,
0 LLM calls. The delivered pom decodes as EUC-KR and its Korean text is unchanged.

## Build as-is

```bash
fixtures/libs/install.sh            # once: example-fw-core into the local repository
JAVA_HOME=<a JDK 8 home> mvn -q -DskipTests package
```
