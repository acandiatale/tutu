# boot2-rewrite-excluded fixture (E13 + E14: files the recipes did not see)

Copy of `boot2` (Spring Boot **2.7.18**, Java 11, jar) that reproduces the 2026-10-22 company run, where the
OpenRewrite recipes left whole files untouched (unparseable files, `openrewrite.exclusions`, fully-qualified names,
missing type attribution) and the build then failed with jakarta / Spring Data errors.

* `MemberRepository extends PagingAndSortingRepository<Member, Long>` (+ `findByEmail` / `existsByEmail`).
  `MemberService` calls `save`, `findAll()` and `deleteById` — fine on Spring Data 2.7 (the paging interface extends
  `CrudRepository`), but Spring Data 3+ removed that, so after the migration `no suitable method found for
  findAll(no arguments)` / `cannot find symbol: method save(…)` — **E14**.
* `vendor/LegacyQueryHelper` ("vendored" helper) uses fully-qualified `javax.persistence.EntityManager` /
  `javax.persistence.Query` (no imports); `MemberService.countMembers()` passes its `@PersistenceContext`
  `EntityManager` (javax import, migrated by the recipes) to it → `jakarta.persistence.EntityManager cannot be
  converted to javax.persistence.EntityManager` — **E13**.
* `sbmig.e2e.yml`: `openrewrite.exclusions` = the defaults (`**/*.jsp`, `**/*.jspx`, `**/*.html`, `**/*.htm`) plus
  `**/vendor/**` and `**/repository/**` (lists replace on merge, so the defaults are repeated). The recipes
  (`JavaxMigrationToJakarta`, `MigratePagingAndSortingRepository`) therefore never see those two directories.
* Everything else is `boot2` (lombok 1.18.24, Security 5 DSL, `@MockBean`, commons-lang3 optional artifact).

## Build as-is

```bash
JAVA_HOME=/Users/acandiatale/Library/Java/JavaVirtualMachines/azul-11.0.18/Contents/Home \
  mvn -q -DskipTests package
```

Tests also pass on JDK 11 (`mvn -q test`).

## Expected sbmig outcome (v2, asserted by `scripts/e2e.py`, see `expected.json` → `expect`)

Success, exit 0, **3 LLM calls**: v2 has no pre-emptive handlers, so the build reports what the recipes did not see and
two new fix scripts repair it in two rounds — first the fully-qualified `javax.persistence` names in
`vendor/LegacyQueryHelper` (case `compile|package_does_not_exist|javax.persistence`, `e62cd43b29`), then
`ListCrudRepository<Member, Long>` added to `MemberRepository` (one answer for the `save` / `findAll()` / `deleteById`
cases). commons-lang3 is bumped to 3.17.0 like in `boot2`.
