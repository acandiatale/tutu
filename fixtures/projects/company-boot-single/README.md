# company-boot-single — single-module Spring Boot app → company layout (split into boot / back / front)

Spring Boot 2.7.18 / Java 11, one jar module (`com.example:shop`): `ShopApplication`, one `@RestController`
(`ProductController`), one `@Controller` rendering a Thymeleaf view (`HomeController` → `templates/home.html`), one
`@Service` (`ProductService`, `@Value("${shop.currency:KRW}")`), `application.properties`, a static file
(`static/css/shop.css`), a `@SpringBootTest` (`ShopApplicationTests`) and a plain unit test (`ProductServiceTest`).

Migration (v2.1z-bf, the company rule; `sbmig.e2e.yml` sets the company layout keys back — the local e2e config pins
the pre-9c layout for the older fixtures — and the role parents, every one stood in by `spring-boot-starter-parent`
4.1.0). A Spring Boot as-is project keeps its directories (`boot_as_is: in-place`), but a root pom that is the
application itself is split like a non-Boot project (`layout.boot_single_module: split`, the default; the plan step is
`restructure.apply`, journal `restructure` mode `layout`):

* root `pom.xml`: packaging `pom`, parent `artifacts.parent` (base-parent), `<modules>` = `[shop-back, shop-front,
  shop-boot]`, the two mandatory starters declared here only (`artifacts.mandatory_target: root`);
* `shop-boot/pom.xml`: parent `artifacts.boot_parent` (external, `<relativePath/>`), explicit `groupId` / `version`,
  packaging jar, the Boot plugin; depends on `shop-comp` (the back module's artifactId: the local e2e config keeps the
  `-comp` suffix) and `shop-front`; holds `ShopApplication.java`, `application.properties` and `ShopApplicationTests`;
* `shop-back/pom.xml`: parent `artifacts.comp_parent`; the project's dependencies (`spring-boot-starter-webmvc` after
  the Boot 4 recipe, `spring-boot-starter-thymeleaf`); holds both controllers, the service and `ProductServiceTest`;
* `shop-front/pom.xml`: parent `artifacts.comp_parent`; `src/main/resources/static/css/shop.css` and
  `src/main/resources/templates/home.html` (the classpath places Boot serves / resolves them from: `/css/shop.css` and
  the view name `home` stay what they were);
* no `module/`, no root `src/`.

The final `mvn clean package` builds the three modules; the boot jar holds `BOOT-INF/classes/…/ShopApplication.class`,
`BOOT-INF/classes/application.properties`, `BOOT-INF/lib/shop-comp-1.0.0-SNAPSHOT.jar` and
`BOOT-INF/lib/shop-front-1.0.0-SNAPSHOT.jar` (checked by `archive_entries`). `java -jar` on it answers `/` (the template
from the front jar), `/css/shop.css` and `/api/products`.

The earlier treatment (everything wrapped into `shop-boot`) is the fixture `company-boot-wrap`.

Expected (v2, `expected.json` → `expect`): BUILD SUCCESS, exit 0, **1 LLM call** (the structure review, verdict `ok`),
no fix script.

As-is build (JDK 11):

```bash
JAVA_HOME=$JAVA11_HOME mvn -q -DskipTests package
```
