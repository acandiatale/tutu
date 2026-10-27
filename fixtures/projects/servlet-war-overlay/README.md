# servlet-war-overlay fixture (a WAR overlay dependency, 2026-10-23)

A plain servlet + JSP project (`board`, Java 8, war) whose pom declares the company's common-screens WAR
`com.company.legacy:legacy-base-web:1.0.0` as a `<type>war</type>` dependency and overlays it with
`maven-war-plugin <overlays>` (header JSP + base css). Before 2026-10-23 the restructure copied that WAR dependency
into the back **jar** module, where it is useless and — when the WAR is not reachable from the configured
repositories — breaks dependency resolution. `fixtures/libs/legacy-base-web/v1.0.0` (installed by
`fixtures/libs/install.sh`) stands in for the in-house WAR. Company layout (`sbmig.e2e.yml`).

## Build as-is

```bash
fixtures/libs/install.sh
JAVA_HOME=/Users/acandiatale/Library/Java/JavaVirtualMachines/azul-11.0.18/Contents/Home \
  mvn -q -DskipTests package
```

## Expected sbmig outcome (v2, asserted by `scripts/e2e.py`, see `expected.json` → `expect`)

* `board-back` (BoardServlet on `jakarta.servlet`) and `board-front` (`list.jsp`) carry no `legacy-base-web`
  dependency; `board-boot` is the WAR for the external Tomcat and declares `legacy-base-web` `<type>war</type>` with
  the `maven-war-plugin` `<overlays>` block: the built WAR contains the overlay's `WEB-INF/jsp/common/header.jsp`
  and `css/base.css`
* flag `war-overlay-moved` and a follow-up that the overlay WAR must be resolvable from the company repositories;
  `web-xml-registered`; BUILD SUCCESS, exit 0, **1 LLM call** (the structure review, verdict `ok`), no fix script
