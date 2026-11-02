# servlet-javax-grid — a vendor grid library on javax, its `-jakarta` build found and applied without the LLM (v2.1z-e, 2026-11-02)

A plain servlet 3.1 web app (`com.example:gridapp`, Java 8, WAR): `ExportServlet` hands the request / response to the
vendor grid component `com.example.grid8:grid8:1.0.6` (`GridExporter.export(HttpServletRequest, HttpServletResponse)`,
compiled against `javax.servlet`), `VersionServlet` prints the component's version, `index.jsp` links both.

Modelled on a company run (2026-11-02): a commercial UI library kept the build red after the recipes moved the app to
`jakarta.servlet` — `incompatible types: jakarta.servlet.http.HttpServletRequest cannot be converted to
javax.servlet.http.HttpServletRequest` —, the LLM declined with "no jakarta build exists", and the report told a person to
find one, although the vendor's Nexus held the same coordinates with versions `1.1.x` (javax) and **`2.0.5-jakarta`**
(jakarta). The run's own lookup had not seen it: its first repository URL did not answer and the lookup fell back to
`~/.m2` silently.

The stand-in library (`fixtures/libs/grid8`, installed by `fixtures/libs/install.sh`): `1.0.6` and `1.1.15` on
`javax.servlet`, `2.0.5-jakarta` on `jakarta.servlet` with a **silent pom** (no `<dependencies>`), so only the version
qualifier (and the jar scan of the class files' `jakarta/servlet/` references) proves it is a jakarta build.

## Build as-is

Install the stand-in library first (`fixtures/libs/install.sh`, idempotent — it builds `grid8` 1.0.6 / 1.1.15 on JDK 8 and
`2.0.5-jakarta` on JDK 21), then on JDK 8:

```bash
fixtures/libs/install.sh
JAVA_HOME=/Users/acandiatale/Library/Java/JavaVirtualMachines/corretto-1.8.0_362/Contents/Home \
  mvn -q -DskipTests package
```

## Migration

Migration (company layout; `sbmig.e2e.yml` as in `servlet-webxml-legacy`):

* one OpenRewrite run (`javax.servlet` → `jakarta.servlet` in the servlets), the restructure (`gridapp-boot` WAR,
  `gridapp-back` with the servlets and the grid dependency, `gridapp-front` with `index.jsp`, `WebXmlRegistrations`);
* the first build fails in `gridapp-back` with the `incompatible types` error of `ExportServlet` → case
  `external_javax_api`; the fix loop's **jakarta build discovery** (`sbmig/build/jakarta_discovery.py`) asks the repository
  set for `com.example.grid8:grid8`, sees `2.0.5-jakarta` as the higher-major candidate, verifies it by its `-jakarta`
  version qualifier, and the zero-LLM pre-try **`sbmig-jakarta`** rewrites `gridapp-back/pom.xml` (`1.0.6 → 2.0.5-jakarta`,
  commit `fix(sbmig-jakarta): …`) — a major change, so the Handover carries the ⚠ operator line with sbmig's synthesized
  guide; the rebuild passes;
* 1 LLM call in all (the structure review); no fix script stored (a pre-try is not a ladder step).

`expected.json`: `success: true`, the try `sbmig-jakarta` with result `build_ok`, `gridapp-back/pom.xml` declares
`com.example.grid8:grid8` at `2.0.5-jakarta`, the Handover names the major upgrade.
