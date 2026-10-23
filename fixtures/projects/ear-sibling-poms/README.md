# ear-sibling-poms fixture (no aggregator pom: APP/BATCH + APP/WEB + APP/EAR, DB, STATIC — 2026-10-23)

Modelled on a company project: the upload root holds `APP/`, `DB/` and `STATIC/` and **no pom.xml**. `APP/BATCH`
(jar, `portalBATCH`), `APP/WEB` (war, `portalWEB`, depends on the batch jar; JSPs under
`src/main/webapp/WEB-INF/jsp`, servlets mapped in `web.xml`) and `APP/EAR` (packaging `ear`, `portal`: packages
`portalWEB` as a `<type>war</type>` dependency with context root `/portal` and `portalBATCH`; `commons-io` for the
EAR's `lib/`) each have their own pom and **none has a `<parent>`** — they were built one after the other
(`install`) and the EAR was deployed to an application server (`src/main/application/META-INF/jeus-application-dd.xml`).
`STATIC/` (one JSP + `resources/` with downloads) and `DB/` (SQL) are referenced by no pom.

Before 2026-10-23 sbmig could not restructure this tree (no root pom → `restructure failed: root pom not found`), and
a run on `APP/EAR` alone copied the EAR's `portalWEB:war` dependency into the back module (Maven then looked for it
in central). Company layout (`sbmig.e2e.yml`, like `company-servlet-jsp`).

## Build as-is

```bash
for m in APP/BATCH APP/WEB APP/EAR; do
  (cd "$m" && JAVA_HOME=/Users/acandiatale/Library/Java/JavaVirtualMachines/azul-11.0.18/Contents/Home \
    mvn -q -DskipTests install)
done
# a single module: cd APP/BATCH && mvn -q -DskipTests package
```

## Expected sbmig outcome (asserted by `scripts/e2e.py`, see `expected.json` → `expect`)

* intake writes a root aggregator `pom.xml` (`portal-aggregator`, modules `APP/BATCH`, `APP/WEB`, `APP/EAR` in
  dependency order, property `sbmig.project-name = portal`): flag `aggregator-generated`, journal
  `aggregator_generated`, a follow-up that says so
* the three modules build in one reactor through jdk21 / spring; the restructure merges them (company layout,
  `{name}` = `portal`, the root renamed `portal` once the EAR module is gone): `portal-back` (NoticeServlet on
  `jakarta.servlet`, NoticeCleaner, `commons-io` from the EAR's lib), `portal-front` (`list.jsp` under
  `META-INF/resources/WEB-INF/jsp/notice/`, `static/css/portal.css`), `portal-boot` WAR for external Tomcat with the
  generated `WebXmlRegistrations` (`/notice/list.do`)
* no dependency on `portalWEB` / `portalBATCH` / `portal` and no `maven-ear-plugin` in any generated pom; the EAR
  descriptor kept under `portal-boot/src/main/resources/asis-ear/…`; flag `ear-module-dropped` and a follow-up with
  the context root `/portal`; a follow-up for `NoticeCleaner`'s `main` (a standalone batch merged into the back module)
* `STATIC/` and `DB/` untouched: flag `web-content-left` + a follow-up naming `STATIC/`
* BUILD SUCCESS, exit 0, 0 LLM calls
