# java6-legacy fixture (scenario S8: Java source level ≤ 1.7, removed JDK APIs)

A small pure-Java jar (case `pure_java`) as a 2010-era team wrote it: `<source>1.6</source><target>1.6</target>`.
JDK 21 cannot compile level 1.6 (minimum 8). sbmig never builds the as-is project. It only builds the migrated
tree on JDK 21.

* `maven-compiler-plugin` 2.3.2 (`source/target 1.6`, UTF-8), `maven-surefire-plugin` 2.4.3, `junit:junit` 4.11.
* `org.apache.commons:commons-lang3` 3.1, which is one of the 8 **optional** artifacts of
  `config/artifacts.local-e2e.yml`, so it takes the optional-bump path (→ 3.17.0).
* `Codec` uses `sun.misc.BASE64Encoder` / `BASE64Decoder`. These are JDK internals and `javac --release 21` does not
  see them. OpenRewrite's `UseJavaUtilBase64` (inside `UpgradeToJava21`) cannot resolve the types on JDK 21 and
  leaves the class alone, so the build reports `cannot find symbol BASE64Encoder / BASE64Decoder` (the advisory
  `jdk-removed-apis` gives the fix script its hint).
* `Dates` uses `javax.xml.bind.DatatypeConverter.printDateTime/parseDateTime`. JAXB was removed from the JDK in 11.
  The one OpenRewrite run moves the import to `jakarta.xml.bind`; the build then misses the JAXB API — the same
  case as `pure-java` (`compile|package_does_not_exist|jakarta.xml.bind`, `7a52b1cea8`).
* `LedgerLines` is written in pre-Java-7 style: no diamond, no try-with-resources, an anonymous `Comparator`.
  It compiles unchanged on 21.
* `LedgerUtilsTest` is a single JUnit 4 test (not compiled by the migration build: `-Dmaven.test.skip=true`).

`sbmig.e2e.yml` replays the LLM (`llm.provider: fake`, `replay/`: the review, the bootclasspath script, the Base64
script — **no answer for the JAXB case**) and shares the KB `out/e2e/kb-jaxb` with `pure-java` (the reuse scenario,
`expect.kb.after: pure-java`: run `--only pure-java,java6-legacy` to run the pair alone).

## Build as-is

```bash
JAVA_HOME=/Users/acandiatale/Library/Java/JavaVirtualMachines/corretto-1.8.0_362/Contents/Home \
  mvn -q -DskipTests package
```

JDK 8 accepts `-source 1.6` with an "obsolete" warning. `mvn -q clean package` (without `-DskipTests`) also passes
the 3 tests.

## Expected sbmig outcome (v2, asserted by `scripts/e2e.py`, see `expected.json` → `expect`)

Success, exit 0, **3 LLM calls** (review + 2 new scripts) and **one reuse**. Round 1: javac 21 rejects the JDK 6
compiler `<bootclasspath>` together with `--release 21` (file-less, case `c5d089fead`) → a new script drops it
(`improved`). Round 2: the JAXB case of `Dates.java` **reuses pure-java's stored v1 without an LLM call** (`reuse`;
the meta ends with 2 successes in 2 projects, error record `fix_method: script_reuse`); the Base64 cases of that round
wait, because the reused script changed their module's pom. Round 3: a new script ports `Codec` from
`sun.misc.BASE64*` to `java.util.Base64` → `build_ok`. Result: release 21, commons-lang3 3.17.0, JAXB on jakarta
with `jakarta.xml.bind-api` + `jaxb-runtime` unversioned in `module/comp-jar/pom.xml`, no `<bootclasspath>` /
`<source>1.6` left in any pom, advisory `jdk-removed-apis` = `guide` (hint + Handover).

The pom's JDK 6-era compiler setup (`<fork>true</fork>` + `<compilerArguments><bootclasspath>${java.home}/lib/rt.jar</bootclasspath>`)
is what round 1 fixes (v1 stripped it with the pre-emptive handler `compiler-legacy-config`).
