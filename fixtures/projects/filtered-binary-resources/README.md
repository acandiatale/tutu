# filtered-binary-resources — binary files under `<filtering>true</filtering>` (v2.1m)

A Spring Framework 6.2.12 jar app whose `src/main/resources` is filtered (so `application.properties` gets
`@project.version@`) and holds two binary files next to it: `fonts/app.woff2` (a known binary extension) and
`data/items.dmdb` (an extension nobody knows — the company shape — whose bytes contain NUL and are not valid text).

The company failure behind it (2026-10-30): maven-resources-plugin 3.3 — what the Boot 4.1 parents manage — reads a filtered
binary as text and fails the copy with `MalformedInputException: Input length = 1`; with an empty KB the LLM wrote three
different scripts for the same error and the case hit the cap. v1's handler E12 (`resources-binary-filtering`) had written
`nonFilteredFileExtensions`; v2 step 4 deleted it.

Expected: `spring_legacy`, layout `binres-back` / `binres-boot`; the restructure writes maven-resources-plugin
`<nonFilteredFileExtensions>dmdb, woff2</nonFilteredFileExtensions>` into `binres-back/pom.xml` (the plugin's own
jpg/jpeg/gif/bmp/png defaults are not repeated, `README.txt` / `data/catalog.properties` stay filtered — `application.properties` itself moves to the boot module, where the
boot parent filters it), BUILD SUCCESS with ONE LLM call (the structure review), both binaries byte-identical in
`target/classes`, `catalog.version` still filtered in.

As-is build:

    JAVA_HOME=/Users/acandiatale/Library/Java/JavaVirtualMachines/corretto-21.0.1/Contents/Home mvn -q -DskipTests package
