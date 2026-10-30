# servlet-nonstandard-dirs — pom with non-standard top-level directories (`APP` / `STATIC` / `DB`) → company layout

Plain servlet webapp on Java 8, packaging `war` (`com.example:ndirs`), no Spring, laid out the way some company
projects are (2026-10-23):

* `APP/src` — the Java sources (`<sourceDirectory>APP/src</sourceDirectory>`): `ReportServlet` and `UploadFilter`
  (no annotations, mapped only in web.xml) and `ExcelTemplate`, which reads `templates/report-template.xml` from the
  classpath;
* `STATIC/` — the web root (`maven-war-plugin` `<warSourceDirectory>${basedir}/STATIC</warSourceDirectory>`):
  `index.jsp`, `WEB-INF/web.xml` (2.5 XSD: filter `upload` + servlet `report` on `/report/*`),
  `WEB-INF/jsp/report.jsp` (`<%@ page import="javax.servlet.http.HttpSession" %>`), `css/app.css`, `img/logo.png`;
* `STATIC/resources/` — also a **filtered** classpath resource dir (`<resource><directory>STATIC/resources</directory>
  <filtering>true</filtering>`): the Excel XML template with `${rows}` placeholders, `sample.xlsx` (a real workbook),
  `media/intro.mp4` (a few KB standing in for a video; it contains `${project.version}` bytes a filter would rewrite);
* `DB/schema.sql` — SQL scripts, not referenced by the pom.

`maven-resources-plugin` is pinned to 2.6 (the Maven 3.0-era default, which filtered binaries without failing); with
the 3.2+ plugin of the Boot 4 parents the filtered xlsx / mp4 fail the build with `MalformedInputException` (E12).

Migration (company layout; `sbmig.e2e.yml` as in `company-servlet-jsp`):

* `ndirs-back` — `APP/src/**` (on `jakarta.servlet`) and `STATIC/resources/**` at the same relative paths, the copied
  `<sourceDirectory>APP/src` / `<resource><directory>STATIC/resources` entries; the structure review's script adds
  `<nonFilteredFileExtensions>` so `sample.xlsx` / `intro.mp4` reach `target/classes` byte-identical;
* `ndirs-front` — the JSPs under `src/main/resources/META-INF/resources/` (`index.jsp`, `WEB-INF/jsp/report.jsp`
  keeps `javax.servlet.http.HttpSession`: v2 renames nothing inside JSPs), `static/css/app.css`, `static/img/logo.png`;
* `ndirs-boot` — WAR (JSPs), generated `WebXmlRegistrations` for the filter + servlet, `web.xml` stashed under
  `asis-webinf/`; no `warSourceDirectory` in its pom;
* nothing of `STATIC/` is left at the root; `DB/` stays where it is.

Expected (v2, `expected.json` → `expect`): BUILD SUCCESS, exit 0, **1 LLM call**, no fix script, flag
`web-xml-registered`. The structure review's input lists the binary files under the filtered `STATIC/resources`
as a fact; the replayed review reports it as **critical** and its script adds `nonFilteredFileExtensions` xlsx / mp4
to `ndirs-back/pom.xml` (committed; the resources plugin 2.6 pin is a warning). The JSPs keep `javax.servlet` (not
compiled by the build; Tomcat 10+ needs them on jakarta — an open point, fixtures/projects/README.md).

As-is build (JDK 8):

```bash
JAVA_HOME=$JAVA8_HOME mvn -q -DskipTests package
```

v2.1m (2026-10-30): the restructure writes maven-resources-plugin `nonFilteredFileExtensions` `mp4, xlsx` into
`ndirs-back/pom.xml` before the first build, so the structure review no longer reports the filtered binaries as critical and runs no
script (one warning: the pinned plugin version).
