# company-boot-single — single-module Spring Boot app → company layout (wrapped into `{name}-boot`)

Spring Boot 2.7.18 / Java 11, one jar module (`com.example:shop`): `ShopApplication`, one `@RestController`
(`ProductController`), one `@Service` (`ProductService`, `@Value("${shop.currency:KRW}")`), `application.properties`.

Migration (round 9c, the company rule; `sbmig.e2e.yml` sets the company layout keys back — the local e2e config pins
the pre-9c layout for the older fixtures — and the role parents, every one stood in by `spring-boot-starter-parent`
4.1.0). A Spring Boot as-is project keeps its sources together (`boot_as_is: in-place`); a single-module Boot app is
**wrapped** into `{name}-boot` = `shop-boot` under an aggregator root:

* root `pom.xml`: packaging `pom`, parent `artifacts.parent` (base-parent), `<modules>` = `[shop-boot]`, the two
  mandatory starters declared here only (`artifacts.mandatory_target: root`);
* `shop-boot/pom.xml`: parent `artifacts.boot_parent` (external, `<relativePath/>`), explicit `groupId` / `version`,
  packaging jar, every source under `shop-boot/src/main/java/…`, `application.properties` under
  `shop-boot/src/main/resources`; no `shop-back` / `shop-front` (nothing is split), no `module/`, no root `src/`.

Expected (v2, `expected.json` → `expect`): BUILD SUCCESS, exit 0, **1 LLM call** (the structure review, verdict `ok`),
no fix script.

As-is build (JDK 11):

```bash
JAVA_HOME=$JAVA11_HOME mvn -q -DskipTests package
```
