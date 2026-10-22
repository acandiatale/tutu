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

## Expected outcome

Success, exit 0, **0 LLM calls**: the pre-emptive SPRING handlers (selected from the as-is scan: javax Jakarta EE
imports → `jakarta-namespace-sweep`, `jpa` library → `spring-data-crud-repository`) run after the spring recipes
and before the first spring build. `MemberRepository` then extends
`PagingAndSortingRepository<Member, Long>, ListCrudRepository<Member, Long>` and `LegacyQueryHelper` names
`jakarta.persistence.*` only. Flags `jakarta-leftovers-renamed` and `spring-data-crud-restored`; commons-lang3 is
bumped to 3.17.0 like in `boot2`.
