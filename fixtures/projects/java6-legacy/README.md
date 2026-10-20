# java6-legacy fixture (scenario S8: Java source level ≤ 1.7, removed JDK APIs)

A small pure-Java jar (case `pure_java`) as a 2010-era team wrote it: `<source>1.6</source><target>1.6</target>`.
JDK 21 cannot compile level 1.6 (minimum 8). sbmig never builds the as-is project. It only builds the migrated
tree on JDK 21.

* `maven-compiler-plugin` 2.3.2 (`source/target 1.6`, UTF-8), `maven-surefire-plugin` 2.4.3, `junit:junit` 4.11.
* `org.apache.commons:commons-lang3` 3.1, which is one of the 8 **optional** artifacts of
  `config/artifacts.local-e2e.yml`, so it takes the optional-bump path (→ 3.17.0).
* `Codec` uses `sun.misc.BASE64Encoder` / `BASE64Decoder`. These are JDK internals and `javac --release 21` does not
  see them. OpenRewrite's `UseJavaUtilBase64` (inside `UpgradeToJava21`) cannot resolve the types on JDK 21 and
  leaves the class alone. The `jdk-removed-apis` advisory adds the porting hint to the prompt, and the replayed LLM
  answer (`replay/llm-001.json`) ports the class to `java.util.Base64`.
* `Dates` uses `javax.xml.bind.DatatypeConverter.printDateTime/parseDateTime`. JAXB was removed from the JDK in 11.
  The pre-emptive handler `jaxb-runtime-add` adds `jaxb-api` + `jaxb-runtime` 2.3. `UpgradeToJava21`
  (`AddJaxbAPIDependencies`) turns the API into `jakarta.xml.bind:jakarta.xml.bind-api` 2.3.3. The spring-phase
  `JavaxMigrationToJakarta` moves the import to `jakarta.xml.bind` (API 3.0.1, `jaxb-runtime` 3.0.2).
* `LedgerLines` is written in pre-Java-7 style: no diamond, no try-with-resources, an anonymous `Comparator`.
  It compiles unchanged on 21.
* `LedgerUtilsTest` is a single JUnit 4 test (→ JUnit 5 by `JUnit4to5Migration`).

`sbmig.e2e.yml` replays the LLM (`llm.provider: fake`, `replay/`) and uses its own KB (`out/e2e/kb-java6-legacy`).
`llm-002.json` answers the KB harvest's recipe-candidate call with `NONE`.

## Build as-is

```bash
JAVA_HOME=/Users/acandiatale/Library/Java/JavaVirtualMachines/corretto-1.8.0_362/Contents/Home \
  mvn -q -DskipTests package
```

JDK 8 accepts `-source 1.6` with an "obsolete" warning. `mvn -q clean package` (without `-DskipTests`) also passes
the 3 tests.

Expected migration result: release 21, compiler plugin 3.16.0, surefire 3.5.3, commons-lang3 3.17.0, JAXB on
jakarta, `Codec` on `java.util.Base64` (1 LLM fix + 1 harvest call), and the `jdk-removed-apis` advisory `replaced`,
with the Handover line "code rewritten by the LLM for jdk-removed-apis: …Codec.java".
