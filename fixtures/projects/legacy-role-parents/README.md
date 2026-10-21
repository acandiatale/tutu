# legacy-role-parents — a project migrated once by the earlier in-house tool (role parents on the old line)

Spring Boot 2.7.18 / Java 11, `root{boot-jar, catalog-service, order-service}`: the earlier migration direction put the
boot module next to the service modules and made **every module inherit an in-house role parent directly** instead of
the root — `boot-jar` the `boot-jar` parent, each service the `comp-jar` parent, all on the old 1.0.0 line (stood in
here by `spring-boot-starter-parent` 2.7.18 with `<relativePath/>`; the root inherits the same stand-in for
`base-parent`). The module poms therefore carry explicit `groupId` / `version`. `catalog-service` uses lombok
without a version (managed by the old role parent: 1.18.24, too old for JDK 21 javac → handler `lombok-version-floor`
pins it for the jdk21 phase; the Boot 4.1 role parent then manages it above the optional floor, so no optional bump is reported).

Migration (`sbmig.e2e.yml` sets `artifacts.boot_parent` / `comp_parent` = `spring-boot-starter-parent` 4.1.0, the
local stand-in for the company's `boot-jar` / `comp-jar` 4.0.0): the services are folded into `module/comp-jar`,
the as-is boot module is kept as `module/boot-jar`, and the three parents are moved to the configured line —
root → `artifacts.parent`, `module/boot-jar` → `boot_parent`, `module/comp-jar` → `comp_parent` (external,
`<relativePath/>`, explicit groupId/version, the two mandatory starters in both module poms). Expected: BUILD SUCCESS,
exit 0, 0 LLM calls.

As-is build (JDK 11):

```bash
JAVA_HOME=$JAVA11_HOME mvn -q -DskipTests package
```
