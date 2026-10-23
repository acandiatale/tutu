# fixtures/webapps — Tomcat webapp trees without a pom (`sbmig inventory`)

Inputs for the read-only inventory (`sbmig inventory <dir|zip>`, `sbmig/analyze/webapp_inventory.py`). These trees
are never built and are not part of `scripts/e2e.sh`; `tests/analyze/test_webapp_inventory.py`,
`tests/report/test_inventory_md.py` and `tests/core/test_cli_inventory.py` pin the expected inventory.

## legacy-exploded

A company-style "no framework" deployment copied off a Tomcat server: two exploded webapps under one root, a stray
Ant script nobody runs, sources inside the webapp directories, jars dropped into `WEB-INF/lib` by hand.

```text
legacy-exploded/
├── build.xml                                   stray Ant script (build file, no pom → no warning)
├── portal/                                     plain servlets, Servlet 2.3, MS949 sources
│   ├── index.jsp  login.jsp  common/header.jspf   3 JSPs; index.jsp has <%@ page import="javax.servlet.http.HttpSession" %>
│   ├── css/portal.css  js/portal.js             2 static files
│   ├── META-INF/context.xml                     <Resource name="jdbc/PortalDS" …>
│   └── WEB-INF/
│       ├── web.xml                              web-app_2_3.dtd, 1 servlet, 1 filter, 1 taglib, resource-ref jdbc/PortalDS
│       ├── src/com/acme/portal/LoginServlet.java, DbUtil.java   cp949 bytes with Korean string literals
│       ├── src/com/acme/common/Util.java        also in admin/src → 1 duplicate class
│       ├── classes/com/acme/portal/Legacy.class compiled by JDK 8 javac -source 1.6 -target 1.6 (major 50), no source
│       ├── lib/commons-lang-2.6.jar, ojdbc6.jar, json-simple-1.1.1.jar   known stems
│       ├── lib/acme-util.jar                    unknown coordinates (company jar)
│       ├── lib/commons-io-2.4.jar.old           backup → ignored
│       └── lib/README.txt                       not a jar → ignored file
└── admin/                                      Spring MVC, Servlet 2.5, UTF-8 sources
    ├── src/com/acme/admin/AdminController.java  imports RequestMapping + javax.servlet.http.HttpServletRequest
    ├── src/com/acme/common/Util.java
    └── WEB-INF/
        ├── web.xml                              version="2.5", DispatcherServlet + ContextLoaderListener
        ├── spring/admin-servlet.xml             <beans …>
        ├── jsp/dashboard.jsp, users.jsp         2 JSPs
        └── lib/spring-webmvc-4.3.30.RELEASE.jar, servlet-api-2.5.jar
```

The jars are synthetic (a zip holding only `META-INF/MANIFEST.MF`); only their names, sizes and sha1 matter.

### Expected inventory

| Fact | portal | admin |
|---|---|---|
| servlet spec | 2.3 (DTD) | 2.5 |
| frameworks | plain-servlet | spring-mvc |
| JNDI | `jdbc/PortalDS` | - |
| JSPs / static | 3 / 2 | 2 / 0 |
| jars known / unknown / ignored files | 3 / 1 (`acme-util.jar`) / 2 (`commons-io-2.4.jar.old`, `README.txt`) | 2 / 0 / 0 |
| jar guesses | `commons-lang:commons-lang` 2.6, `com.oracle.database.jdbc:ojdbc6` (no version), `com.googlecode.json-simple:json-simple` 1.1.1 | `org.springframework:spring-webmvc` 4.3.30.RELEASE, `javax.servlet:servlet-api` 2.5 |
| class files | 1, Java 1.6, 1 without source (`com.acme.portal.Legacy`) | 0 |
| source root | `portal/WEB-INF/src` (3 files, cp949, jdbc-only) | `admin/src` (2 files, utf-8, spring) |
| proposal | `portal` war, Java 1.6 | `admin` war, Java 1.8 (no class files → default) |

Plus: 1 duplicate class (`com.acme.common.Util`), build files `build.xml`, no warnings.

Console summary (`sbmig inventory fixtures/webapps/legacy-exploded`):

```text
inventory: …/fixtures/webapps/legacy-exploded
  webapp roots 2: admin (2 jsp, servlet 2.5, spring-mvc), portal (3 jsp, servlet 2.3, plain-servlet)
  source roots 2: admin/src (2 java, utf-8), portal/WEB-INF/src (3 java, cp949)
  java from classes: 1.6
  jars: 5 known / 1 unknown / 2 ignored (unknown: acme-util.jar)
  classes without source: 1   duplicate classes: 1
  proposals: admin (war, java 1.8), portal (war, java 1.6)
  build files: build.xml
  md: …/out/inventory/inventory.md
  json: …/out/inventory/inventory.json
```

Regenerating: the tree was written once by a throw-away Python script (`zipfile` for the jars, `javac` 1.8 with
`-source 1.6 -target 1.6 -g:none` for `Legacy.class`, 137 bytes); edit the files directly from now on and keep
`LoginServlet.java` / `DbUtil.java` in cp949.
