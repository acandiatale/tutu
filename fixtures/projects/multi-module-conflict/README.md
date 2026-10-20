# multi-module-conflict fixture (multi-module Spring Boot 2.7, first-wins merge conflict the build-fix loop repairs)

Same shape as `multi-module-merge` (root aggregator `com.example:shop`, packaging `pom`, `spring-boot-starter-parent`
**2.7.18**, `java.version` 11, modules in this order: `core`, `web`, `boot-jar`), but the modules collide:

* `com.example.shop.shared.Shared` exists in **both** `core` (`greet(String)` only, first in `<modules>` → kept) and
  `web` (a copy-pasted version with an extra `farewell(String)`). As-is `web` compiles its own copy (sources win over
  the `shop-core` jar on the classpath), so the project builds.
* `web`'s `ShopController` calls `Shared.farewell(name)`. After the first-wins merge comp-jar holds the `core` copy →
  `cannot find symbol: method farewell(java.lang.String) location: class com.example.shop.shared.Shared` in the
  restructure-phase build.
* `META-INF/services/com.example.shop.spi.Plugin` in both modules with different lines (`com.example.shop.core.CorePlugin`
  / `com.example.shop.web.WebPlugin`) — a line-list registry: merged as a **union** (first copy's lines, then the new
  lines of the next copies), not first-wins.
* `messages.properties` differs (`shop.title=shop (core)` / `shop (web)`) → plain first-wins, `web` copy saved under
  `<run dir>/merge-conflicts/`.
* `boot-jar` (`shop-app`, depends on `shop-web`): `ShopApplication`, `application.properties`, `spring-boot-maven-plugin`
  (kept as boot-jar, dependency → `${project.groupId}:shop-comp`).

`sbmig.e2e.yml` replays the LLM (`llm.provider: fake`, `replay/`) and uses its own KB (`out/e2e/kb-multi-module-conflict`).
`replay/llm-001.json` is the fix for the restructure cluster: its prompt carries the "Module merge notes" section and
the dropped `web` copy of `Shared.java` as a read-only reference; the answer is one SEARCH/REPLACE on the kept
`module/comp-jar/src/main/java/com/example/shop/shared/Shared.java` (SEARCH = the `greet` method of the `core` copy,
moved verbatim by the merge) that adds `farewell(String)`. `llm-002.json` answers the KB harvest with `NONE`.

## Build as-is

```bash
JAVA_HOME=/Users/acandiatale/Library/Java/JavaVirtualMachines/azul-11.0.18/Contents/Home \
  mvn -q -DskipTests package
```

## Expected migration (default `merge` policy)

`core` + `web` folded into `module/comp-jar`, `boot-jar` kept as `module/boot-jar`; 1 replayed LLM fix (restructure
phase) + 1 harvest call; success, exit 0, nothing blocked; comp-jar `Shared` has `greet` and `farewell`; the services
registry lists both plugins; `messages.properties` keeps `shop (core)`; follow-up `merge conflicts: N file(s) …`
naming `Shared.java`.
