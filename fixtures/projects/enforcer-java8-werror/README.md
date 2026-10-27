# enforcer-java8-werror fixture (scenarios E4 + E5: strict legacy build)

`pure-java`-like plain Java 8 jar (`maven.compiler.source/target 1.8`, commons-lang3 3.9, JUnit 4.12) with a build
that cannot pass on JDK 21 unchanged:

* `maven-enforcer-plugin` 3.4.1 `requireJavaVersion [1.8,9)` (+ a harmless `requireMavenVersion [3.6,)`)
  → on JDK 21: `Rule 0: …RequireJavaVersion failed with message: pricing-core must be built with JDK 8`
* `maven-compiler-plugin` 3.8.1 with `<compilerArgs>-Xlint:all -Werror</compilerArgs>` and sources that are
  warning-free on JDK 8 but not on JDK 21:
  * `PriceRule`: `new Integer(int)` — `[removal] Integer(int) … deprecated and marked for removal`
    (OpenRewrite may rewrite this one to `Integer.valueOf`)
  * `PriceCalculator.finalize()` override + `super.finalize()` — `[removal] finalize() in java.lang.Object …`
    (no recipe touches it, so `-Werror` stays fatal: `warnings found and -Werror specified`)

## Build as-is

```bash
JAVA_HOME=/Users/acandiatale/Library/Java/JavaVirtualMachines/corretto-1.8.0_362/Contents/Home \
  mvn -q -DskipTests package
```

(Tests pass on JDK 8 with `mvn -q test`.)

## Expected sbmig outcome (v2, asserted by `scripts/e2e.py`, see `expected.json` → `expect`)

Success, exit 0, **3 LLM calls** (review + 2 fix scripts; v1 had the handlers `enforcer-relax` / `remove-werror`, gone in
v2). Round 1: the file-less enforcer failure (case `630b997bc5`) → the script finds the rule in the poms and sets
`requireJavaVersion` to `[21,)`. Round 2: `warnings found and -Werror specified` (refined case key
`compile|build|warnings found and -werror specified`, `b2b6bc54b7`) → the script drops `-Werror`, keeps `-Xlint:all`
and leaves a `MIGRATION-TODO: -Werror removed` (post-checks on `module/comp-jar/pom.xml`).
