# Fixture projects

Small but real Maven projects used as the sbmig test corpus (unit tests read them; integration tests and
`scripts/e2e.py` build and migrate them). Each fixture has a `README.md` (exact as-is build command) and an
`expected.json`: what `sbmig analyze` should report **plus an `expect` block** — the asserted outcome of
`sbmig run` (success / blocked / flags / exit code / LLM calls / post-checks on the migrated sources).
Happy-path fixtures use only Maven Central; scenario fixtures may need the simulated in-house libraries in
`fixtures/libs/` (installed by `fixtures/libs/install.sh`).

## Matrix

Catalog IDs refer to `docs/ERROR-HANDLING.md`.

| fixture | case | catalog | as-is build JDK | expected outcome (`expect`) |
|---|---|---|---|---|
| `pure-java` | `pure_java` | — | 8 | success, exit 0, 0 LLM, commons-lang3 → 3.17.0, guava → 33.4.8-jre; JAXB (`jakarta.xml.bind-api`, `jaxb-runtime`) declared without `<version>` in `module/comp-jar/pom.xml` (round 9: managed by the parent) |
| `servlet-tomcat` | `servlet_tomcat` | — | 8 | success, exit 0, 0 LLM, commons-io → 2.19.0 |
| `spring5-xml` | `spring_legacy` | — | 8 | success, exit 0, ≤ 2 LLM calls (`WebMvcConfigurerAdapter` is an LLM/KB target), slf4j-api → 2.0.17 |
| `boot2` | `spring_boot` | — | 11 | success, exit 0, 0 LLM, commons-lang3 → 3.17.0, lombok → 1.18.38 |
| `boot3` | `spring_boot` | — | 21 | success, exit 0, 0 LLM, guava → 33.4.8-jre |
| `msa` | `msa` | — | 11 / 21 per service | success, exit 0, 0 LLM, lombok → 1.18.38; per-service properties (round 9): `order-service` reads `${order.tax-rate}` (external → `order.tax-rate=__SET_ME__` in `order-service/src/main/resources/application.properties`) and `${shop.region:kr}` (defaulted), `user-service` reads `${user.greeting}` defined in its own `application.yml` (nothing written, no `application.properties`) |
| `boot2-javax-lib` | `spring_boot` | X1 | 11 (+ `install.sh`) | **blocked** `external_javax_api` `com.company.legacy:legacy-web-lib`, restructure still runs (journal `restructure ok`), `success=false`, exit **2**, 0 LLM |
| `boot2-javax-lib-optional` | `spring_boot` | X1 | 11 (+ `install.sh`) | per-fixture `sbmig.e2e.yml` makes legacy-web-lib optional (2.0.0) → bumped `1.0.0→2.0.0`, nothing blocked, success, exit 0, 0 LLM |
| `euckr-legacy` | `pure_java` | E1 | 8 with `JAVA_TOOL_OPTIONS=-Dfile.encoding=MS949` | success, exit 0, 0 LLM, flag `source-encoding-set:(cp949\|ms949\|euc-kr\|euc_kr)`, migrated `Greeting.java` still decodes as cp949 with `안녕하세요` |
| `enforcer-java8-werror` | `pure_java` | E4, E5 | 8 | success, exit 0, 0 LLM, flags `enforcer-(relaxed\|skipped)` and `werror-removed` |
| `missing-artifact` | `pure_java` | X3 | 8 with `-Dmaven.repo.local.tail=<ghost repo>` | **blocked** `artifact_not_found` `com.company.nonexistent:ghost-lib`, `success=false`, exit **2**, 0 LLM, ≤ 240 s |
| `msa-shared-lib` | `msa` | S1 | 18 (whole reactor) | success, exit 0, 0 LLM; `common` + services on jakarta, `common/pom.xml` gets the parent but no mandatory deps |
| `boot2-method-rename` | `spring_boot` | X2 | 11 (+ `install.sh`) | `sbmig.e2e.yml`: legacy-web-lib optional at **3.0.0** (`clientIp` renamed to `remoteClientIp`), replayed LLM (`replay/`), own `kb.dir: out/e2e/kb-method-rename` → bumped `1.0.0→3.0.0`, `cannot find symbol method clientIp(HttpServletRequest)` in 2 files → 2 replayed LLM fixes (one per file cluster), success, exit 0, ≤ 2 LLM calls, nothing blocked; harvest compiles the KB candidate `com.company.kb.LegacyRequestHelperClientIpToRemoteClientIp` (`org.openrewrite.java.ChangeMethodName`, no LLM) — `expect.kb` |
| `boot2-cloud-legacy` | `spring_boot` | advisories (`docs/ADVISORIES.md`) | 11 | **reference run for library advisories + Handover**: success, exit 0, 0 LLM, nothing blocked; advisories `springfox` (recipe → replaced by springdoc), `sleuth` (Boot 4 umbrella → replaced by Micrometer Tracing), `spring-cloud-bom` (train upgraded → replaced), `hystrix` (guide → still-present, Resilience4j in "From here on" + manual follow-ups); ≥ 1 carried-over pin; springfox/sleuth gone from every pom, `springdoc-openapi-starter-webmvc-ui` in `module/comp-jar/pom.xml` |
| `boot2-hystrix-command` | `spring_boot` | advisory-guided LLM fix (`docs/ADVISORIES.md`) | 11 | programmatic `InventoryCommand extends HystrixCommand<Integer>` the project recipe cannot convert → after the starter swap 2 compile clusters (`InventoryCommand.java` with the `hystrix` advisory hint in its prompt, `StockService.java` without); `sbmig.e2e.yml`: replayed LLM (`replay/`), own `kb.dir: out/e2e/kb-hystrix-command` → response 1 ports the class to `CircuitBreaker.ofDefaults("inventory")` keeping `execute()`, response 2 no change; success, exit 0, ≤ 3 LLM calls (2 fix + the harvest recipe-candidate call), nothing blocked, journal `attempt` with `advisory_ids: [hystrix]`, `hystrix` replaced, Handover "code rewritten by the LLM for hystrix: …InventoryCommand.java" |
| `multi-module-merge` | `spring_boot` (multi-module, packaging `pom`) | layout `merge` (`docs/ARCHITECTURE.md` §8) | 11 (also builds on 21) | default `multi_module_policy: merge`: `core` + `web` folded into `module/comp-jar`, as-is `boot-jar` **kept** (moved to `module/boot-jar`, `shop-web` dependency → `shop-comp`, `ShopApplication` untouched, nothing generated); differing `messages.properties` → first wins (`hello from core`), `web` copy saved under `<run dir>/merge-conflicts/`, follow-up `merge conflicts: 1 file(s) …`; identical `META-INF/shared.txt` dropped; success, exit 0, 0 LLM, commons-lang3 3.9 → 3.17.0 |
| `legacy-role-parents` | `spring_boot` (multi-module on in-house role parents, old line) | company rule, Boot as-is in place (`docs/ARCHITECTURE.md` §8) | 11 | earlier in-house migration: `boot-jar` + service modules each inheriting an external role parent on the old line (stand-in `spring-boot-starter-parent` 2.7.18); `sbmig.e2e.yml` = company layout keys + role parents → directories **kept** (no `module/`), poms only: root → `parent`, still the aggregator of `[boot-jar, catalog-service, order-service]`, the 2 mandatory starters in the **root only**; `boot-jar` (a Boot app by its pom; not named `*-boot` → profile warning) → `boot_parent`; services → `comp_parent`; `<relativePath/>`, explicit groupId/version; lombok managed by the old parent → `lombok-version-floor`; success, exit 0, 0 LLM |
| `legacy-external-config` | `spring_legacy` | externalized config (`knowledge/notes/externalized-config.md`) | 8 | Spring 5 XML war from an external Tomcat: `@Value("${mail.host}")` only in `$CATALINA_BASE/conf/catalog-local.properties`, `<jee:jndi-lookup jndi-name="java:comp/env/jdbc/catalogDS">`, `System.getProperty("catalog.home")`, web.xml context-param `catalog.region=kr` → success, exit 0, 0 LLM; `expect.properties`: those 4 keys `external`, `catalog.title` / `catalog.page-size` not (packaged in `catalog.properties`); boot-jar `application.properties` holds `mail.host` / `spring.datasource.*` = `__SET_ME__` (`catalog.home` is `-D`-only: listed, not written) and `server.servlet.context-parameters.catalog.region=kr`; follow-up "… placeholder values …"; Handover "Properties to set before running" |
| `java6-legacy` | `pure_java` | S8 | 8 | Java 1.6 source level (compiler 2.3.2, surefire 2.4.3): `sbmig.e2e.yml` replayed LLM (`replay/`), own `kb.dir: out/e2e/kb-java6-legacy` → `UpgradeToJava21` sets release 21, JAXB → `jakarta.xml.bind-api` + `jaxb-runtime` (Dates on `jakarta.xml.bind`), `sun.misc.BASE64Encoder/Decoder` in `Codec.java` → 1 replayed LLM fix to `java.util.Base64` with the `jdk-removed-apis` advisory hint (+ 1 harvest call answered `NONE`); success, exit 0, ≤ 2 LLM calls, nothing blocked, advisory `jdk-removed-apis` replaced, commons-lang3 3.1 → 3.17.0; JAXB declared without `<version>` in `module/comp-jar/pom.xml` (round 9) |
| `multi-module-conflict` | `spring_boot` (multi-module, packaging `pom`) | layout `merge` conflict + fix-prompt merge notes (round 9) | 11 | `core` and `web` both define `com.example.shop.shared.Shared` (kept `core` copy lacks `farewell()`, which `web`'s `ShopController` calls) → restructure-phase compile error → 1 replayed LLM fix (`replay/`, own `kb.dir: out/e2e/kb-multi-module-conflict`) adds `farewell()` to the kept comp-jar `Shared.java` (prompt: "Module merge notes" + dropped copy as reference, journal `merge_conflict_hints`) + 1 harvest call (`NONE`); `META-INF/services/com.example.shop.spi.Plugin` merged as a union (both plugins), `messages.properties` first-wins (`shop (core)`); success, exit 0, ≤ 2 LLM calls, nothing blocked |
| `legacy-ibatis` | `spring_legacy` | advisory `ibatis2` (guide) | 8 | Spring 3.2.18 XML + iBATIS 2 (`ibatis-sqlmap` 2.3.4.726, `CatalogDao extends SqlMapClientDaoSupport`, `SqlMapClientFactoryBean`), Java 1.6 → Spring 7 removes `orm.ibatis` → spring-phase cluster, fake LLM (no replay) answers "no fix" twice → blocked `repeated_failure` (spring); restructure + final build still run and fail; the final verify loop releases it for 2 more LLM tries (`loop.verify_extra_llm_attempts`, 2026-10-22) and blocks it again (phase verify), ≤ 4 LLM calls; `success=false`, exit **2**, 2 LLM calls (both with the `ibatis2` hint), advisory `ibatis2` guide / still-present, MyBatis follow-up |
| `company-boot-single` | `spring_boot` (single jar) | company layout: wrap (round 9c) | 11 | `sbmig.e2e.yml` = company layout keys + role parents; single-module Boot 2.7.18 app (`com.example:shop`) wrapped into `shop-boot` under an aggregator root (packaging `pom`, parent 4.1.0, `<modules>` = `[shop-boot]`, the 2 mandatory starters in the root only); `shop-boot/pom.xml` parent `boot_parent` with `<relativePath/>`, explicit groupId/version, all sources + `application.properties` under `shop-boot/src/main/…`; no `shop-back`/`shop-front`; success, exit 0, 0 LLM |
| `company-spring5-web` | `spring_legacy` (war, static resources, no JSP) | company layout: boot/back/front (round 9c) | 11 | Spring 5.3 XML webapp (`web.xml` + `DispatcherServlet`, `@Controller` + `@Service`, `index.html`, `css/site.css`, `js/app.js`) → root aggregator `[shop-back, shop-boot, shop-front]` (any order), mandatory in the root only; `shop-boot` jar (generated Application only), `shop-back` (controller, service, XML configs), `shop-front` (`src/main/resources/static/…`, no Java); parents base/boot/comp/comp (stand-in 4.1.0); success, exit 0, 0 LLM |
| `company-servlet-jsp` | `servlet_tomcat` (war, JSP + JSTL) | company layout: JSP → WAR boot module (round 9c) | 8 | `HttpServlet` forwarding to `WEB-INF/jsp/hello.jsp` (`<c:out>`, JSTL `javax.servlet:jstl` 1.2), `css/site.css` → `shop-boot` packaging **war** (`spring-boot-starter-tomcat` provided, Application `extends SpringBootServletInitializer`, `shop-boot/target/*.war`), `shop-back` (servlet on `jakarta.servlet`), `shop-front` (`src/main/resources/META-INF/resources/WEB-INF/jsp/hello.jsp`, `static/css/site.css`); mandatory in the root only; success, exit 0, 0 LLM |
| `servlet-webxml-legacy` | `servlet_tomcat` (war, web.xml 2.3 DTD only) | W1 (2026-10-23) | 8 | every servlet / filter / listener / error page declared only in web.xml (no annotations): `front` (`/front/*` + `*.do`, load-on-startup 1), `report` (no mapping), jsp-file `help`, filters `encoding` (`/*`) + `audit` (`/admin/*`, REQUEST + FORWARD), `AppStartupListener`, session-timeout 30, error pages 404 / `java.lang.Exception`, welcome `index.jsp`, MIME `hwp` → company layout (`legacy-boot` WAR / `-back` / `-front`) + generated `legacy-boot/src/main/java/com/example/legacy/WebXmlRegistrations.java` (compiles against Boot 4.1), `server.servlet.session.timeout=30m`; success, exit 0, 0 LLM, flag `web-xml-registered`, journal `web_xml_registrations` (2 / 2 / 1), follow-ups for the jsp-file servlet, the welcome file and the MIME mapping |
| `servlet-nonstandard-dirs` | `servlet_tomcat` (war, non-standard top-level dirs) | E12, W1 (2026-10-23) | 8 | `<sourceDirectory>APP/src`, web root `STATIC/` (`maven-war-plugin` `<warSourceDirectory>${basedir}/STATIC`), `STATIC/resources` also a **filtered** `<resource>` (Excel XML template, `sample.xlsx`, `media/intro.mp4`), `DB/schema.sql` unreferenced, resources plugin 2.6 → company layout: `ndirs-back` keeps `APP/src` + `STATIC/resources` (classpath; `nonFilteredFileExtensions` keeps xlsx / mp4 byte-identical in `target/classes` — post-check `file_same_as`), `ndirs-front` gets the JSPs (`META-INF/resources/index.jsp`, `WEB-INF/jsp/report.jsp` on `jakarta.servlet.http.HttpSession`) + `static/css`, `static/img`, `ndirs-boot` WAR with the generated `WebXmlRegistrations` and no `warSourceDirectory`; nothing of `STATIC/` left at the root, `DB/` untouched; success, exit 0, 0 LLM, flags `web-xml-registered` + `jsp-javax-renamed` |
| `boot2-fix-failures` | `spring_boot` | X2, error cases (2026-10-23) | 11 (+ `install.sh`) | the sources of `boot2-javax-lib`, configured like `boot2-method-rename` (legacy-web-lib **3.0.0**, own `kb.dir: out/e2e/kb-fix-failures`) with a **deliberately imperfect** replayed LLM: `llm-001` SEARCH block not in `ClientIpFilter.java` (`apply_failed`), `llm-002` / `llm-004` edit `MemberController.java` but keep `clientIp(http)` (`no_change`), `llm-003` the real rename → `ClientIpFilter` error record `resolved` by `llm`, `text_replace` `clientIp → remoteClientIp`, `replace_rule_candidate`, history `apply_failed → improved`; `MemberController` `blocked` (`repeated_failure`) with its failed attempts and a `failure_reason`; success false, exit 2, ≤ 8 LLM calls; `error_cases.jsonl` / `fix_attempts.jsonl` / `error_cases.csv` in the run dir and the fixture KB — `expect.error_cases` |
| `ear-sibling-poms` | `servlet_tomcat` (no root pom, EAR) | aggregator, EAR (2026-10-23) | 11 (each module) | `APP/BATCH` (jar), `APP/WEB` (war, JSPs, web.xml), `APP/EAR` (ear: `portalWEB` war + batch, context root `/portal`, JEUS descriptor), `DB/`, `STATIC/` (JSP + downloads, referenced by no pom), no `<parent>` anywhere → intake generates the aggregator (`aggregator-generated`), the merge builds `portal-back` / `portal-front` / `portal-boot` WAR, the EAR is dropped (`ear-module-dropped`, descriptor in `asis-ear/`, context-root follow-up), no module dependency or `maven-ear-plugin` in a generated pom, `STATIC/` left in place (`web-content-left`); success, exit 0, 0 LLM |
| `servlet-war-overlay` | `servlet_tomcat` (war overlay) | 2026-10-23 | 11 (+ `install.sh`) | a `<type>war</type>` dependency on the in-house `legacy-base-web` WAR + `maven-war-plugin <overlays>` → not in `board-back` / `board-front`; `board-boot` WAR declares it with the overlays block and its built WAR holds the overlay's header JSP + css; flag `war-overlay-moved` + repository follow-up; success, exit 0, 0 LLM |
| `boot2-rewrite-excluded` | `spring_boot` | E13, E14 (2026-10-22) | 11 | copy of `boot2` whose `sbmig.e2e.yml` adds `**/vendor/**` and `**/repository/**` to `openrewrite.exclusions` (defaults repeated): `MemberRepository extends PagingAndSortingRepository<Member, Long>` (service calls `save` / `findAll()` / `deleteById`) and `vendor/LegacyQueryHelper` with fully-qualified `javax.persistence.EntityManager` / `Query` are never seen by the recipes → pre-emptive SPRING handlers `spring-data-crud-repository` (adds `ListCrudRepository<Member, Long>`) and `jakarta-namespace-sweep` (javax → jakarta); success, exit 0, **0 LLM**, flags `jakarta-leftovers-renamed` + `spring-data-crud-restored`, commons-lang3 → 3.17.0, `post_checks` on both files |
| `boot2-qlrm-javax` | `spring_boot` | X1b (2026-10-22) | 11 | copy of `boot2` (tests dropped) plus QLRM 1.7.1 under its old coordinates `ch.simas.qlrm:qlrm`: `query/MemberSummaryQuery` calls `JpaResultMapper.list(Query, Class)`, which takes `javax.persistence.Query` up to QLRM 3.0.4 → after the jakarta migration only a newer QLRM compiles: pre-emptive SPRING handler `jakarta-library-floor` relocates it to `org.qlrm:qlrm:4.2.1` (`knowledge/libraries/jakarta_libraries.yml`); success, exit 0, **0 LLM**, flag `jakarta-library-bumped`, `post_checks` on the pom and the query class |

Optional artifacts from `config/artifacts.local-e2e.yml` (exactly 8) used directly (→ "optional bump" path):

| artifact | fixtures |
|---|---|
| `org.apache.commons:commons-lang3` | pure-java, boot2, boot2-javax-lib*, boot2-method-rename, boot2-fix-failures, boot2-rewrite-excluded, boot2-qlrm-javax, euckr-legacy, enforcer-java8-werror, missing-artifact, multi-module-merge/core, java6-legacy |
| `com.google.guava:guava` | pure-java, boot3 |
| `org.projectlombok:lombok` | boot2, boot2-javax-lib*, boot2-method-rename, boot2-fix-failures, boot2-rewrite-excluded, boot2-qlrm-javax, msa/order-service, msa-shared-lib/order-service |
| `com.fasterxml.jackson.core:jackson-databind` | spring5-xml |
| `commons-io:commons-io` | servlet-tomcat |
| `org.slf4j:slf4j-api` | spring5-xml |
| `com.company.legacy:legacy-web-lib` 2.0.0 | boot2-javax-lib-optional only (its `sbmig.e2e.yml` swaps it in for slf4j-api, still 8 entries) |
| `com.company.legacy:legacy-web-lib` 3.0.0 | boot2-method-rename, boot2-fix-failures (same swap, version 3.0.0) |

## Simulated in-house libraries (`fixtures/libs/`)

* `legacy-web-lib/v1.0.0` (Java 8, `javax.servlet` API) and `legacy-web-lib/v2.0.0` (Java 17, `jakarta.servlet`):
  `com.company.legacy:legacy-web-lib`, `LegacyRequestHelper(HttpServletRequest)` + `static clientIp(HttpServletRequest)`.
  `legacy-web-lib/v3.0.0` is the 2.0.0 jakarta build with `clientIp` **renamed** to `remoteClientIp` (no alias).
  `fixtures/libs/install.sh [local-repo]` installs all three (idempotent; `~/.m2` by default, `-Dmaven.repo.local=$1`
  otherwise).
* `ghost-lib/` (`com.company.nonexistent:ghost-lib:1.2.3`) is **never installed**; `ghost-lib/make-repo.sh <dir>` lays it
  out in a throwaway repo only for the as-is build of `missing-artifact`. `scripts/e2e.py` refuses to run that fixture
  if the jar is in `~/.m2` (`requires_absent`).

## Running the e2e

```bash
scripts/e2e.sh                                    # recipes + advisories check, installs fixtures/libs, runs all fixtures
scripts/e2e.sh --only boot2-javax-lib,missing-artifact
scripts/e2e.sh --skip-libs --only euckr-legacy    # do not (re)install fixtures/libs
python scripts/e2e.py --expect-only               # print each fixture's expectations, run nothing
python scripts/e2e.py --install-libs --out out/e2e-x --llm-provider fake
```

For each fixture `scripts/e2e.py` runs `python -m sbmig.cli run fixtures/projects/<name> --config
config/artifacts.local-e2e.yml [--config fixtures/projects/<name>/sbmig.e2e.yml] --out out/e2e`, loads the run's
`report.json` + `journal.jsonl`, evaluates `expect` and prints a table with an `expect` PASS/FAIL column and the first
mismatch; `out/e2e/summary.md|json` hold the details (all mismatches). The exit code is non-zero if any fixture
mismatches (fixtures without `expect` must reach a successful final build). Missing report fields count as empty,
so an engine that does not yet produce e.g. `blocked` shows up as a FAIL with a readable reason, not a crash.

### `expect` keys

| key | meaning |
|---|---|
| `success` / `exit_code` | `RunReport.success` and the CLI exit code (0 ok, 2 blocked/unresolved, 3 aborted) |
| `max_llm_calls` | `llm_usage.calls` ≤ n (missing = 0) |
| `max_duration_s` | wall-clock of the `sbmig run` process ≤ n |
| `blocked` | the exact set of blocked items by match: every expected `{reason, artifact "g:a", owner_class?, phase?}` must match ≥ 1 `BlockedItem` (top-level `blocked` or `phases[].blocked`), and every actual item must match an expected one; `[]` = nothing may be blocked |
| `flags` / `forbidden_flags` | each entry must (not) be in `RunReport.flags`; `regex:<re>` = full match |
| `optional_bumped` | each entry (`"g:a old→new"` or `regex:`) must appear in the bumps the engine recorded: `report.optional_bumped`, journal events carrying `optional_bumped`, or phase notes `optional bumped: …` |
| `unresolved_max` | `len(unresolved)` ≤ n |
| `journal_events` | each dict must be a subset of some `journal.jsonl` event, e.g. `{"event": "restructure", "ok": true}` |
| `phases` | `{"restructure": "ok"}` — last result of that phase (`ok`/`FAIL`/`skipped`) |
| `advisories` | each `{id, policy?, outcome?, recipe_source?, status?, library?, replacement?}` must match a `report.advisories[]` entry by `advisory_id`, every given field equal; `advisories_exact: true` forbids unlisted findings; `advisories_absent: [id]` must not be reported |
| `handover` | on `report.handover`: `changes_min`, `carried_over_min`/`carried_over_max` (`len(carried_over)`), `parent_before`/`parent_after` (`g:a[:v]` prefix), `java_*`/`spring_boot_*` (exact), `from_here_contains` (entries of `from_here`), `removed_contains`/`added_contains` (`g:a[:v]`) |
| `followups_contains` | each entry must match some `manual_followups` line |
| `kb` | `{dir, candidates_min?, harvest_candidates_min?, recipes?}` — `dir` (relative to the repo, must be under `out/`; `scripts/e2e.py` deletes it before the run) is the fixture's own `kb.dir` from its `sbmig.e2e.yml`; `candidates_min` counts the recipe documents in `<dir>/recipes/candidates/*.yml`; `harvest_candidates_min` is on `len(report.harvest.candidates_created)`; each `recipes[]` `{recipe, options, name?}` must match a document in `recipes/{candidates,active}/*.yml` (`name` equal when given) with a `recipeList` entry `{<recipe>: {…}}` whose options are a superset of `options` |
| `properties` | on `report.properties` (else `profile.properties`), the externalized-config inventory: `external_min`, `external` (keys that must be `external` findings), `absent` (keys that must not be), `external_in` (`{service: [keys]}`, MSA: external findings whose `service` is that deployable), `written` (⊆ `placeholders_written`), `placeholder_file_contains` (substrings of `placeholder_files[0]`) |
| `post_checks` | checks on the migrated `project/` (paths may use `**` globs): `file_decodes_as`/`file_contains` `{path, encoding, contains, not_contains}` (strict decode), `pom_parent {path, group_id, artifact_id, version}`, `pom_dependency_absent`/`pom_dependency_present {path, artifacts: ["g:a"]}`, `pom_dependency_unversioned`/`pom_dependency_versioned {path, artifacts}` (each listed dependency declared without / with a `<version>` element — e.g. a version the parent manages was dropped; absent = fail); `pom_packaging {path, packaging}` (`jar`/`war`/`pom`, absent `<packaging>` = `jar`), `pom_module_list {path, modules, any_order?}` (the exact top-level `<modules>` list, in order unless `any_order: true`); `module/comp-jar/pom.xml` / `module/boot-jar/pom.xml` are the poms the pre-9c restructure creates for a single-jar Boot app (compile dependencies live in `comp-jar`); company-layout fixtures (round 9c) check `<name>-boot|back|front/…` instead |

`*_contains` entries: `regex:<re>` must fully match an entry (DOTALL), a plain string must be a substring of one.

Top-level helpers: `catalog` (IDs), `requires_libs` (`g:a:v` that must be in the local repo — run `install.sh`),
`requires_absent` (`g:a:v` that must not be there).

## Adding a scenario

1. Create `fixtures/projects/<name>/` (≤ 10 Java files, realistic usages), verify the as-is build with its JDK
   (`JAVA_HOME=… mvn -q -DskipTests package`, plus whatever the scenario needs) and record the command in its
   `README.md`.
2. Write `expected.json` with the analyze keys and an `expect` block; add the catalog ID to `catalog` and to the
   "Fixture" column of `docs/ERROR-HANDLING.md`. If the scenario needs a different artifact policy, add a
   `sbmig.e2e.yml` override (lists replace — keep exactly 8 optional artifacts).
   A scenario that needs an LLM fix uses a replayed LLM (`llm.provider: fake`, `llm.base_url: "replay:<fixture>/replay"`,
   `replay/llm-NNN.json` = `{"note", "response"}` in call order) and, when it exercises the KB, its own `kb.dir` under
   `out/` plus an `expect.kb` block.
3. Add the fixture to `NAMES`/`SCENARIOS` and `BUILD_MATRIX` in `tests/fixtures/test_fixture_projects.py`, to
   `DEFAULT_ORDER` in `scripts/e2e.py`, and to the matrix above.
4. `python -m pytest tests/fixtures` (schema) and `python -m pytest tests/fixtures -m integration -k <name>` (as-is build).

Integration check: `python -m pytest tests/fixtures -m integration` builds every fixture with its documented JDK
(scenario libraries go into a throwaway repo via `maven.repo.local.tail`, so `~/.m2` is not modified).
