---
title: Fix policy for the build-fix loop
keywords: []
cases: []
phases: []
---
# Fix policy (always included)

Context: you are one step inside an automated loop. The orchestrator runs your fix (a Python fix script), commits it, and
re-runs Maven (`compile` / `test-compile` / `package`). A phase only ends with `BUILD SUCCESS`; regressions are
reverted automatically. Your job is to make **this cluster of errors** disappear with the smallest correct change.

## Allowed
- Mechanical API migrations: import/package changes (javax → jakarta, relocated Boot 4 classes), renamed
  methods/classes, signature changes (`HttpStatus` → `HttpStatusCode`), replacing removed APIs with their
  documented successor.
- pom.xml edits: add/replace/remove a dependency, change a version, add `<classifier>jakarta</classifier>`,
  plugin version bumps, compiler `<release>21</release>`, annotation processor paths.
- New small files only when required (e.g. a `SecurityFilterChain` config replacing a removed base class
  inside the same class is preferred over a new file).
- Fixing tests so they compile (`@MockBean` → `@MockitoBean`, JUnit 4 → 5 APIs). Tests are never deleted.

## Forbidden
- Deleting or emptying business logic, methods, classes or tests to "make it compile".
- Commenting code out, `@Disabled`/`@Ignore` on tests, `-Dmaven.test.skip`, `<skip>true</skip>` on compiler,
  `<failOnError>false</failOnError>`, `@SuppressWarnings` as a fix for an error.
- Adding new third-party libraries unless the error is a missing artifact/package and a note names the
  replacement (e.g. `jakarta.xml.bind-api` + `jaxb-runtime`). Prefer Spring Boot managed versions (omit
  `<version>` when the parent/BOM manages it).
- Downgrading Java, Spring Boot, or the in-house parent; changing `<parent>` of the root pom (restructure owns it).
- Reformatting, reordering imports, renaming unrelated code, "drive-by" refactors. Keep the diff minimal.
- Guessing file content you have not seen: ask with `<need_files>` / `<need_grep>` instead.

## Runtime concerns
Runtime behaviour is out of scope for the loop, but must not be hidden. When a compile fix changes semantics
or leaves something to verify at runtime (web.xml mappings, security rules, Jackson 3 serialization defaults,
trailing-slash matching, removed auto-configuration), add a one-line comment at the change:
`// MIGRATION-TODO: <what to verify and why>` (XML: `<!-- MIGRATION-TODO: … -->`, properties: `# MIGRATION-TODO: …`).
These are collected into the report for the as-is owners.

## Strategy
- Fix the root cause first: one missing dependency often explains dozens of `cannot find symbol` /
  `package … does not exist` errors — fix the pom, not every import.
- `cannot find symbol` on a class that exists in a newer package → change the import.
- If a previous attempt on this cluster is listed, it did NOT work: do something different.
- Prefer one file per answer when possible; several files are allowed if they belong to the same root cause.
- Confidence: 0.9+ only for well-known mechanical renames; ≤ 0.5 when guessing.
