# ant-webapp — Ant web application without pom.xml → pom.xml synthesized at intake (v2.1j) → company layout WAR

A classic Eclipse "Dynamic Web Project" built by **Ant** — `build.xml`, **no `pom.xml`** (the business team's request
2026-10-29: "Ant 프로젝트도 Maven 으로 변경 후 마이그레이션"). `build.xml` (`<project name="ant-webapp">`, properties
`src.dir=src`, `web.dir=WebContent`, `build.dir=build`, `app.version=1.2.0`): `<javac srcdir="src" source="1.6"
target="1.6" encoding="UTF-8" classpathref="cp">` against `<path id="cp">` = every jar of `WebContent/WEB-INF/lib`, a
`<copy>` of the non-Java files of `src/` into the classes, `<war destfile="build/ant-webapp.war"
webxml="WebContent/WEB-INF/web.xml">` and a `deploy` target that runs `<exec executable="scp">` (not translatable).

* `src/com/example/antweb/HelloServlet.java` — `extends javax.servlet.http.HttpServlet`, registered in
  `WebContent/WEB-INF/web.xml` (Servlet 2.5, no annotation) at `/hello`; uses `org.apache.commons.lang.StringUtils`,
  `com.example.util.Greeter` (from the in-house jar below) and `messages.properties` next to it (a resource under the
  Ant source root).
* `WebContent/index.jsp` — `<%@ page import="javax.servlet.http.HttpSession" %>` (renamed to jakarta by the
  restructure, `layout.jsp_jakarta_rename`); `WebContent/css/site.css`.
* `WebContent/WEB-INF/lib` — one jar per rung of the coordinates ladder (`sbmig/analyze/jar_coords.py`); the three
  stubs and the real jar are written by **`make-stubs.py`** (idempotent, fixed timestamps; `--util` recompiles
  `example-util-1.0.jar` from `lib-src/` with `javac --release 8`):

  | jar | how sbmig resolves it | in the synthesized pom |
  |---|---|---|
  | `commons-lang-2.6.jar` (stub with `META-INF/maven/commons-lang/commons-lang/pom.properties`) | ① pom.properties | `commons-lang:commons-lang:2.6` (the real jar comes from Central) |
  | `servlet-api-2.5.jar` (stub, `javax/servlet/Servlet.class`) | ② `jar_names.yml` stem `servlet-api` | `javax.servlet:servlet-api:2.5` `provided` |
  | `example-util-1.0.jar` (real `com/example/util/Greeter.class`, no pom.properties) | ③ sha1 lookup on Central: not found → unknown, **used** | `com.company.migrated:example-util:1.0` **`system`**, jar copied to `lib/` |
  | `old-report.jar` (stub, `com/example/report/Old.class`) | unknown, no source uses `com.example.report` | **dropped** (listed in the Handover) |

* `lib-src/com/example/util/Greeter.java` — the source of `example-util-1.0.jar`, for reference only (outside the
  `javac srcdir`: neither Ant nor the synthesized pom compiles it).

Migration (v2.1j + the round 9c company layout; `sbmig.e2e.yml` sets the company layout keys back and the role
parents, every one stood in by `spring-boot-starter-parent` 4.1.0):

1. **intake**: `pom.xml` synthesized from build.xml and committed (`intake: pom.xml synthesized from build.xml`):
   `com.company.migrated:ant-webapp:1.2.0` (`intake.synth_group_id`, project name, `app.version`), packaging `war`,
   `maven.compiler.source/target` 1.6 → **1.8** (`intake.min_java_source`), `<sourceDirectory>src</sourceDirectory>`,
   a `<resource>` for `src` without `**/*.java`, `maven-war-plugin` `warSourceDirectory` `WebContent`, `finalName`
   `ant-webapp`; the system-scope jar is referenced as `${maven.multiModuleProjectDirectory}/lib/example-util-1.0.jar`.
   Flags `pom-synthesized`, `jar-coordinates-unknown`; journal `pom_synthesized` (4 jars, 1 unknown, 1 dropped).
2. OpenRewrite: Java 21, `jakarta.servlet` (`javax.servlet:servlet-api` → `jakarta.servlet:jakarta.servlet-api`).
3. **restructure** (company layout, non-Boot → split): the synthesized war pom becomes the root **aggregator**
   (`<packaging>pom</packaging>`, the synthesis comment and `ant-webapp:1.2.0` kept, parent `artifacts.parent`, the two
   mandatory starters); `ant-webapp-back` keeps `src/` as its `sourceDirectory` (HelloServlet on jakarta,
   `messages.properties`) with commons-lang, the servlet API and `example-util` — the restructure rewrites the system
   path to `${project.basedir}/lib/example-util-1.0.jar` and copies the jar into `ant-webapp-back/lib/`;
   `ant-webapp-front` gets `index.jsp` (`META-INF/resources`, `jakarta.servlet.http.HttpSession`) and
   `static/css/site.css`; `ant-webapp-boot` is a **WAR** (JSP → external Tomcat, `finalName` `ant-webapp`) with the
   generated `WebXmlRegistrations` (`HelloServlet` at `/hello`, flag `web-xml-registered`).
4. structure review (replayed): one warning — the system-scope jar. No build error, no fix script.

Expected (v2, `expected.json` → `expect`): BUILD SUCCESS, exit 0, **1 LLM call** (the structure review); report.md
Handover block "Ant project: pom.xml synthesized" with `mvn install:install-file
-Dfile=WebContent/WEB-INF/lib/example-util-1.0.jar -DgroupId=com.company.migrated -DartifactId=example-util
-Dversion=1.0 -Dpackaging=jar`, `old-report.jar` left out, the `deploy` `<exec>` not translated.

As-is build (Ant; not run by the test suite — the three stubs hold no real classes, so the as-is build needs the
real commons-lang 2.6 / servlet-api 2.5 jars in `WebContent/WEB-INF/lib`):

```bash
JAVA_HOME=$JAVA8_HOME ant war
```

The synthesized pom alone (the intake commit `intake: pom.xml synthesized from build.xml` of a run's `project/`, before
OpenRewrite) builds with Maven on JDK 8 — commons-lang comes from Central, `example-util` from `lib/`:

```bash
JAVA_HOME=$JAVA8_HOME mvn -q -DskipTests package
```
