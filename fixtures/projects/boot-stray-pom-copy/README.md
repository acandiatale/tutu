# boot-stray-pom-copy — a Boot module with a stray copy of itself in `shop-boot/bin/` (v2.1z-h, 2026-11-03)

Spring Boot 2.7.18 / Java 11, a root aggregator (`com.example:shop`, packaging `pom`, `<modules>` = `[shop-boot]`) and
one Boot module `shop-boot` (`ShopApplication`, `ProductController`, `ProductService`, `application.properties`) —
and, listed by nothing, **`shop-boot/bin/`**: a whole copy of the module (`pom.xml` with the module's coordinates,
`.project`, `src/`), the IDE output / backup folder the company's input zips carry (company run 2026-11-03: the
pom scan read it as a project pom, the structure review registered it as an "unlisted module" and Maven refused the
reactor — `Project 'xxx-boot:1.0.0-SNAPSHOT' is duplicated in the reactor`).

Migration (Boot as-is, `boot_as_is: in-place`, pom-only): the intake finds the stray copy (`stray_pom_copies`: unlisted,
same coordinates as `shop-boot/pom.xml`, inside its directory), puts `shop-boot/bin` into `ws.ignored_dirs` (every scan
prunes it like `target/`), flags `stray-pom-ignored`, journals `stray_pom_ignored` and writes one Handover line
(`pom copy ignored: …`). The copy is left where it is — not built, not migrated, never shown to the structure review.
The root keeps `[shop-boot]` (root → base-parent, `shop-boot` → boot-jar stand-ins), build ok, **1 LLM call** (the
review, verdict `ok`), no fix script.

As-is build (JDK 11):

```bash
JAVA_HOME=$JAVA11_HOME mvn -q -DskipTests package
```
