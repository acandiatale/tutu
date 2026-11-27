# fw-convert-basic — framework project, the conversion way (`framework.mode: convert`)

A small project on the in-house framework (stand-in name `Example`, core artifact `com.example.fw:example-fw-core`
from `fixtures/libs/example-fw`), handled the second way: intake converts it into the generated Spring Boot project of
`docs/FW-CONVERT.md` and the run goes on like any multi-module Spring Boot project.

As-is tree:

* `pom.xml`: `com.example.shop:shop-app`, packaging war, Java 8, declares `example-fw-core` 1.0.0 and
  `javax.servlet-api` provided. The generated project takes its names from it: project `shopApp`, package
  `com.example.shop.shopApp`, modules `shopApp-boot` / `-domain` / `-service` / `-store`.
* `src/main/resources/service/order-service.xml`: a service with a router (`find` / `save` / `remove` →
  `/api/order/{transition}`).
* `src/main/resources/service/item-service.xml`: a service without a router; `total` calls `order.countAll`, a
  statement of the order SQL file (a service of another name); `search` is the project's own activity class
  `ItemSearchActivity extends ExampleMyBatisSearch`; `stock` is `StockCheckActivity`, Java code only (no database).
* `src/main/resources/sql/order.example_sql`, `item.example_sql`: the framework's SQL files (named and positional
  parameters).
* `src/main/resources/mybatis/notice-mapper.xml`: a MyBatis mapper, copied as it is.
* `src/main/resources/applicationContext.xml` + `jdbc.properties`: an H2 datasource bean with placeholders and a
  password (the password is not carried over: hand-over item `[DATASOURCE_PASSWORD]`).
* `WebContent/WEB-INF/jsp/shop/order-list.jsp`: includes `common/header.jsp`, links `item-list.mvc` and the camelCase
  page `orderDetail.mvc`, an inline `commonAjax.json` call; `WebContent/js/order.js` has one more such call,
  `WebContent/js/item.js` calls the REST addresses of the item and notice controllers.

The as-is tree does not build here: the stand-in framework has no activity classes (`ExampleMyBatisSearch`), and the
conversion never builds the as-is tree. The command a framework owner would use:

```bash
fixtures/libs/install.sh            # once: example-fw-core into the local repository
JAVA_HOME=/Users/acandiatale/Library/Java/JavaVirtualMachines/corretto-1.8.0_362/Contents/Home mvn -q -DskipTests package
```

## Expected sbmig outcome (asserted by `scripts/e2e.py`, see `expected.json` → `expect`)

`sbmig.e2e.yml` sets `framework.artifacts: ["example-fw-core"]`, `framework.mode: convert`, the company layout with
role parents (every in-house parent stood in by `spring-boot-starter-parent` 4.1.0, `layout.boot_as_is: in-place`)
and leaves `convert.versions` at the default: the MyBatis starter's version is the newest release the repository set
has. Intake commits `convert: generated Spring Boot project` as the second commit (flag `framework-converted`, journal
`framework_converted`); the one analysis sees a multi-module Spring Boot project (no `framework-project` flag), the
plan is `transform.rewrite` → `restructure.boot_pom` → `restructure.review` → `verify.build`. The restructure gives
the root pom the company parent and the mandatory starters, `shopApp-boot` the boot parent, the three other modules
the comp parent. Success, exit 0, one LLM call (the replayed structure review, verdict `ok`), no fix script; the boot
module's WAR `shopApp-boot/target/shopApp.war` exists and carries the JSPs, the mappers and the three module jars.
