# hollow-reactor — a Maven pom that compiles nothing; the sources live beside it (v2.1z-c, 2026-11-02)

Modelled on a company project: `APP/WEB/pom.xml` was a packaging pom over a JDeveloper workspace — its `<build>` only
named `${DIR_xxxJAR}` / `${DIR_xxxWEB}` output directories, the java sat in `DEV/s10/src`, the web root in
`DEV/s10/public_html`, the static files in `STATIC/`. sbmig moved pom.xml and web.xml into `back` / `boot` / `front`, the
build passed (nothing to compile) and the report said **success** — with no source migrated.

The fixture (root WAR pom, Java 8, `javax.servlet-api` provided):

* `pom.xml` — packaging `war`, `src/main/webapp/WEB-INF/web.xml` (an empty 3.1 descriptor, so the WAR builds), **no
  `src/main/java`**; properties `DIR_HOLLOWJAR` / `DIR_HOLLOWWEB` point at `${env.HOLLOW_HOME}` like the company pom;
* `DEV/s10/src/com/example/hollow/HelloServlet.java` (javax.servlet), `DEV/s10/public_html/WEB-INF/web.xml` (welcome
  file only — a servlet registered there would reach the generated `WebXmlRegistrations` without its class being compiled;
  which web.xml is the application's is the tree-profile increment's call) and `index.jsp` — the IDE project nobody's pom reaches;
* `STATIC/css/app.css`, `DB/schema.sql` (start-up SQL, referenced by no pom).

Expected: the as-is build and the migrated build both pass; the structure review says ok (replay); **`hollow-reactor`**
(`sbmig/analyze/hollow.py`: inside java 0 / jsp 0 / web 0, outside java 1 / jsp 1 / web 1 — `DEV: java 1, jsp 1;
STATIC: web 1`), journal `hollow_reactor`, the Handover opens with `the Maven reactor compiles no sources: …`, `success:
false`, exit **2**, `SBMIG-RESULT … exit=2 build=FAIL … attn=hollow-reactor` (`build=` mirrors `success`). Nothing under `DEV/` or `STATIC/` is moved — mapping
those sources is the tree-profile increment.

## Build as-is

```bash
JAVA_HOME=$JAVA8_HOME mvn -q -DskipTests package
```
