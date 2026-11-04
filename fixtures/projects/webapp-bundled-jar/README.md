# webapp-bundled-jar — a vendor jar in `WEB-INF/lib` that no pom declares (v2.1z-i, company run 2026-11-04)

A copy of `company-spring5-web` renamed to `com.example:vault` (package `com.example.vault`): Spring Framework 5.3.31 /
Java 11, packaging `war`, no Spring Boot; `WEB-INF/web.xml` with a `ContextLoaderListener` (`applicationContext.xml`,
scans `service`) and a `DispatcherServlet` (`dispatcher-servlet.xml`, scans `web`); one `@Controller`
(`HomeController`: `/home`, `/api/greeting`, `/api/token`), one `@Service` (`GreetingService`), static
`index.html`, `css/site.css`, `js/app.js`; no JSP.

The scenario: `src/main/webapp/WEB-INF/lib/acme-seed-1.0.jar` is a vendor crypto jar the IDE build saw through the
web-app libraries container — **no pom declares it, it has no `pom.properties`**. `util/CryptoUtil` imports
`com.acme.seed.Seed` and `HomeController#token` calls it. Maven never put the jar on the compile classpath, so the
as-is Maven build fails on purpose (`package com.acme.seed does not exist`); that is the company's shape.

Migration (the company layout, as `company-spring5-web`): root aggregator with `vault-back`, `vault-boot`,
`vault-front`; the sources go to `vault-back`. sbmig's analyze scan lists the jar (flag `bundled-jars`, journal
`bundled_jars`, `logs/bundled-jars.json`); the build after the restructure fails on `package com.acme.seed does not
exist`; the fix loop's zero-LLM pre-try `sbmig-bundled` copies the jar to `vault-back/lib/acme-seed-1.0.jar`, declares
it `system` scope at `${project.basedir}/lib/acme-seed-1.0.jar` (placeholder coordinates — never guessed) and sets
`<includeSystemScope>true</includeSystemScope>` on the boot module's spring-boot-maven-plugin; Handover line
"bundled jar `acme-seed-1.0.jar` …" (publish it to Nexus and replace the entry).

Expected (v2, `expected.json` → `expect`): BUILD SUCCESS, exit 0, **1 LLM call** (the structure review, verdict ok),
one fix attempt with source `sbmig-bundled` (0 LLM calls), no LLM fix script.

The jar (749 bytes, committed as-is; `jar tf` = `META-INF/`, `META-INF/MANIFEST.MF`, `com/acme/seed/Seed.class`,
class file major 52). Rebuild it with JDK 21:

```java
// com/acme/seed/Seed.java
package com.acme.seed;

public final class Seed {
    public static String encrypt(String s) {
        return new StringBuilder(s).reverse().toString();
    }
}
```

```bash
$JAVA21_HOME/bin/javac --release 8 -d classes com/acme/seed/Seed.java
$JAVA21_HOME/bin/jar --create --file acme-seed-1.0.jar --date=2026-11-04T00:00:00Z -C classes com/acme/seed/Seed.class
```

As-is build (JDK 11) — **fails by design** (the jar is undeclared):

```bash
JAVA_HOME=$JAVA11_HOME mvn -q -DskipTests package
```
