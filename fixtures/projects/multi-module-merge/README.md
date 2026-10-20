# multi-module-merge fixture (case 2b: multi-module Spring Boot 2.7, `layout.multi_module_policy: merge`)

* root aggregator `com.example:shop` (packaging `pom`, `spring-boot-starter-parent` **2.7.18**, `java.version` 11),
  modules in this order: `core`, `web`, `boot-jar`
* `core` (`shop-core`, jar): `GreetingService` (`@Service`), `spring-context`, **commons-lang3 3.9** (optional artifact
  → bumped to 3.17.0), `src/main/resources/messages.properties` = `greeting=hello from core`
* `web` (`shop-web`, jar, depends on `shop-core`): `GreetingController` (`@RestController`), its **own**
  `messages.properties` = `greeting=hello from web` (→ a **differing merge conflict**)
* both `core` and `web` ship the same `src/main/resources/META-INF/shared.txt` (→ an **identical duplicate**, dropped
  silently)
* `boot-jar` (`shop-app`, jar, depends on `shop-web`): `ShopApplication` (`@SpringBootApplication`),
  `application.properties`, `spring-boot-maven-plugin`, one `@SpringBootTest`

## Build as-is

```bash
JAVA_HOME=/Users/acandiatale/Library/Java/JavaVirtualMachines/azul-11.0.18/Contents/Home \
  mvn -q -DskipTests package
```

`mvn -q clean package` (with tests) also passes on JDK 11 and on JDK 21 (Boot 2.7 compiles with `--release 11`
semantics on javac 21; no lombok here).

## Expected migration (default `merge` policy)

* `core` + `web` are folded into `module/comp-jar` (`shop-comp`); `boot-jar` is the **kept boot module**: moved to
  `module/boot-jar`, its dependency on `shop-web` replaced by `${project.groupId}:shop-comp:${project.version}`,
  `ShopApplication` / `application.properties` / the test untouched, no Application class generated.
* root modules exactly `module/comp-jar`, `module/boot-jar`.
* `messages.properties`: first module in `<modules>` order wins → comp-jar keeps `hello from core`; the `web` copy is
  saved under `<run dir>/merge-conflicts/web/src/main/resources/messages.properties` and reported (Handover "Merge
  conflicts", manual follow-up `merge conflicts: 1 file(s) …`). `META-INF/shared.txt` is identical → dropped with a note.
