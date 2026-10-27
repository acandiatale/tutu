# legacy-role-parents — a project migrated once by the earlier in-house tool (role parents on the old line)

Spring Boot 2.7.18 / Java 11, `root{boot-jar, catalog-service, order-service}`: the earlier migration direction put the
boot module next to the service modules and made **every module inherit an in-house role parent directly** instead of
the root — `boot-jar` the `boot-jar` parent, each service the `comp-jar` parent, all on the old 1.0.0 line (stood in
here by `spring-boot-starter-parent` 2.7.18 with `<relativePath/>`; the root inherits the same stand-in for
`base-parent`). The module poms therefore carry explicit `groupId` / `version`. `catalog-service` uses lombok
without a version (managed by the old role parent: 1.18.24, too old for JDK 21 javac; v2 builds only after the
restructure, where the Boot 4.1 role parent manages it above the optional floor, so no optional bump is reported).

Migration (round 9c, the company rule): `sbmig.e2e.yml` sets the company layout keys back (`module_dir: ""`,
`{name}-boot` / `{name}-back` / `{name}-front`, `boot_as_is: in-place` — the local e2e config pins the pre-9c layout
for the older fixtures) and `artifacts.boot_parent` / `comp_parent` = `spring-boot-starter-parent` 4.1.0 (the local
stand-in for the company's `boot-jar` / `comp-jar` 4.0.0). A Spring Boot as-is project keeps its directories — only
the poms change:

* root → `artifacts.parent` (base-parent), still the aggregator of `[boot-jar, catalog-service, order-service]`, and
  the **only** pom declaring the two mandatory starters (`artifacts.mandatory_target: root`);
* `boot-jar/pom.xml` is a Boot app by its pom (`spring-boot-maven-plugin`, `ShopApplication`) → `boot_parent`; its
  directory does not match `*-boot`, which is a profile warning, not a blocker;
* `catalog-service` / `order-service` → `comp_parent`;
* every module parent external with `<relativePath/>`, explicit groupId/version kept; no `module/`, no `shop-boot`.

Expected (v2, `expected.json` → `expect`): BUILD SUCCESS, exit 0, **1 LLM call** (the structure review: one warning that
`boot-jar` does not match `*-boot`), no fix script.

As-is build (JDK 11):

```bash
JAVA_HOME=$JAVA11_HOME mvn -q -DskipTests package
```
