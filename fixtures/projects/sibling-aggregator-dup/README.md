# sibling-aggregator-dup — no root pom, `APP/pom.xml` aggregates its sibling `DEV/` (v2.1k, 2026-10-29)

Modelled on a company project: the upload root holds `APP/`, `DEV/`, `STATIC/` and `DB/` and **no pom.xml**.
`APP/pom.xml` (packaging `pom`, `com.example.dup:dup-app`) is the aggregator the owners built from:
`<modules>../DEV</module>`, `./EAR`, `./BATCH`. `DEV/` sits **beside** APP, not below it:

* `DEV/pom.xml` — the WAR (`com.example.dup:dup`), `<parent>` = APP (`<relativePath>../APP/pom.xml</relativePath>`),
  `HelloServlet` on `javax.servlet` (mapped in web.xml), `maven-war-plugin` `<warSourceDirectory>../STATIC</…>`;
* `STATIC/` — the web root: `WEB-INF/web.xml` (3.1: servlet `hello` on `/hello`) and `index.jsp`;
* `APP/EAR` (`dup-ear`, packaging `ear`: packages the `dup` WAR at context root `/dup` and the batch jar),
  `APP/BATCH` (`dup-batch`, jar: `Job`);
* `DB/schema.sql` — SQL, referenced by no pom.

What went wrong before v2.1k: the intake's aggregator planner saw a single top pom (APP — DEV is reached through
`../DEV`) and generated **no** root pom. The analyzer then took `APP/pom.xml` as the root, every Maven call
(effective pom, OpenRewrite, compile, build) ran in the project root without a pom ("there is no POM in this
directory") and the restructure built the layout under `APP/`; with a DEV that does not inherit APP the analyzer
found no root at all and the restructure failed with `root pom not found: pom.xml`. An older variant of the same
tree (`../DEV` invisible to the planner) generated a root listing `DEV` *and* `APP`, and Maven refused the reactor:
`Project 'com.example.dup:dup:1.0.0-SNAPSHOT' is duplicated in the reactor` (the intake's `dedupe_reactor` now
removes such listings; unit tests cover it).

Migration (company layout; `sbmig.e2e.yml` as in `ear-sibling-poms`):

* intake writes a root aggregator `pom.xml` listing **only `APP`** (`dup-aggregator`, property
  `sbmig.project-name = dup` — the EAR's artifactId without its `-ear` suffix): flag `aggregator-generated`;
* one OpenRewrite run from the project root (`HelloServlet` → `jakarta.servlet`);
* the restructure merges APP's reactor into the company layout at the root (`{name}` = `dup`, the root renamed `dup`
  once the EAR is gone): `dup-back` (HelloServlet, Job), `dup-front` (`index.jsp` under `META-INF/resources`),
  `dup-boot` WAR with the generated `WebXmlRegistrations` (`/hello`) and web.xml stashed under `asis-webinf/`;
  `APP/`, `DEV/` and `STATIC/` are gone, `DB/` stays; flag `ear-module-dropped` + the context-root follow-up.

Expected (v2, `expected.json` → `expect`): BUILD SUCCESS, exit 0, **1 LLM call** (the structure review, verdict ok),
no fix script.

As-is build (JDK 8, from the aggregator the owners used):

```bash
cd APP && JAVA_HOME=$JAVA8_HOME mvn -q -DskipTests package
```
