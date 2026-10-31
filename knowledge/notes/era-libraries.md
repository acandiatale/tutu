---
title: 2013-2020 era libraries (Spring 3/4 DAOs, view layers, Jackson 1, Ehcache 2, POI 3, JJWT 0.9, ...) on Spring 7 / Boot 4
keywords:
  - "org\\.springframework\\.orm\\.hibernate[345]|HibernateTemplate|HibernateDaoSupport|HibernateCallback"
  - "org\\.apache\\.commons\\.fileupload|CommonsMultipartResolver|ServletFileUpload"
  - "org\\.codehaus\\.jackson"
  - "org\\.apache\\.tiles|TilesConfigurer|TilesViewResolver|view\\.tiles[23]"
  - "org\\.apache\\.velocity|VelocityEngineUtils|VelocityConfigurer"
  - "net\\.sf\\.ehcache|org\\.springframework\\.cache\\.ehcache|EhCacheCacheManager|EhCacheManagerFactoryBean"
  - "org\\.apache\\.commons\\.collections(?!4)"
  - "net\\.sf\\.json"
  - "io\\.jsonwebtoken|parseClaimsJws|parserBuilder"
  - "org\\.apache\\.poi|CELL_TYPE_|setBoldweight|HSSFColor"
  - "org\\.apache\\.commons\\.httpclient"
  - "org\\.apache\\.axis(?!2)|javax\\.xml\\.rpc"
  - "org\\.apache\\.commons\\.dbcp(?!2)"
  - "com\\.lowagie|openpdf"
  - "StaticLoggerBinder|jcl-over-slf4j|slf4j-log4j12"
  - "org\\.apache\\.commons\\.lang(?!3)\\b|commons-lang:commons-lang"
  - "org\\.apache\\.log4j\\b|log4j:log4j"
cases: [pure_java, spring_legacy, spring_boot, servlet_tomcat, msa]
phases: [spring, restructure]
---
# Era libraries on Spring 7 / Boot 4

Libraries of 2013–2020 Spring 3/4 / Boot 1–2 projects that Spring 7 / Boot 4.1 no longer carry. A `## fix-loop:
<library>` section is what the build-fix loop gets when that advisory fired (knowledge/libraries/advisories.yml).
Without a direct mapping: keep the public signatures, port only what the error names, mark the rest
`// MIGRATION-TODO <advisory id>`. A version-less declaration (`version: null`) gets the as-is version from sbmig.

## fix-loop: hibernate-template
`package org.springframework.orm.hibernate5 does not exist` / `cannot find symbol HibernateTemplate`: Spring 7 moved
the native support to `org.springframework.orm.jpa.hibernate` and dropped HibernateTemplate / HibernateCallback /
HibernateDaoSupport.
- `org.springframework.orm.hibernate5|4|3.LocalSessionFactoryBean`, `.HibernateTransactionManager`,
  `.SpringSessionContext` → `org.springframework.orm.jpa.hibernate.*` (same in Spring XML `class="…"`).
- `extends HibernateDaoSupport` / `HibernateTemplate` → an injected `SessionFactory` and
  `sessionFactory.getCurrentSession()` (Hibernate 7 Session / Query API — see jakarta.md, hibernate-7.md), or JPA
  `EntityManager`.
- Keep every DAO method signature; calls you cannot port mechanically → `// MIGRATION-TODO hibernate-template`.

## fix-loop: commons-fileupload
The pinned FileuploadToFileUpload2 recipe swapped the jar but left `org.apache.commons.fileupload.*` imports (its
package renames are misspelled), and Spring 6 removed `CommonsMultipartResolver`.
- Direct FileUpload code: `ServletFileUpload` → `org.apache.commons.fileupload2.jakarta.servlet6.JakartaServletFileUpload`,
  `org.apache.commons.fileupload.disk.DiskFileItemFactory` → `org.apache.commons.fileupload2.core.DiskFileItemFactory`
  (`DiskFileItemFactory.builder().get()`), `FileItem` → `org.apache.commons.fileupload2.core.FileItem` (artifact
  `org.apache.commons:commons-fileupload2-jakarta-servlet6`, version from the Dependency versions section) — or port
  to `MultipartFile` / `Part`.
- `CommonsMultipartResolver` bean → delete it; `maxUploadSize` → `spring.servlet.multipart.max-request-size` and
  `max-file-size`, `maxInMemorySize` → `file-size-threshold` (servlet-to-boot.md). Controllers taking
  `MultipartHttpServletRequest` / `MultipartFile` need no change.

## fix-loop: jackson1
CodehausToFasterXML already mapped ObjectMapper / JsonSerializer / @JsonSerialize and the annotate / map.ext /
map.ser packages; what is left is the unmapped part of `org.codehaus.jackson`.
- `org.codehaus.jackson.JsonNode` → `tools.jackson.databind.JsonNode`; `org.codehaus.jackson.type.TypeReference` →
  `tools.jackson.core.type.TypeReference` (Jackson 3, Boot 4 default; com.fasterxml.jackson 2 is still managed).
- JsonParser / JsonGenerator / JsonFactory / map.JsonMappingException / map.JsonDeserializer / node.*: move to the
  Jackson 3 (or 2) class of the same simple name; never re-add org.codehaus.jackson jars.

## fix-loop: tiles
Spring 6 removed `org.springframework.web.servlet.view.tiles3` (TilesConfigurer, TilesViewResolver, TilesView).
- Remove the TilesConfigurer / TilesViewResolver bean definitions and their imports so the module compiles; JSPs
  can keep `<jsp:include>` fragments.
- Do not re-create Tiles classes; mark the layout port `// MIGRATION-TODO tiles` (Thymeleaf layout / SiteMesh 3).

## fix-loop: velocity
Spring 5 removed VelocityConfigurer / VelocityViewResolver / `org.springframework.ui.velocity.VelocityEngineUtils`.
- Mail / text templates: `VelocityEngineUtils.mergeTemplateIntoString(engine, name, enc, model)` →
  `StringWriter w = new StringWriter(); engine.getTemplate(name, enc).merge(new VelocityContext(model), w);`
  (`org.apache.velocity:velocity-engine-core` 2.x line; version from the Dependency versions section).
- Web views: remove the Velocity view beans and mark `// MIGRATION-TODO velocity` (port .vm to Thymeleaf / FreeMarker).

## fix-loop: ehcache2
`package org.springframework.cache.ehcache does not exist`: Spring 6 removed EhCacheCacheManager /
EhCacheManagerFactoryBean.
- `EhCacheCacheManager` → `org.springframework.cache.jcache.JCacheCacheManager` over a `javax.cache.CacheManager`
  (Ehcache 3 `org.ehcache:ehcache` classifier jakarta + `javax.cache:cache-api`, both Boot-managed), or delete the
  bean and let Boot configure `spring.cache.type=jcache`.
- Direct `net.sf.ehcache.CacheManager` / `Cache` / `Element` use → `javax.cache` API; ehcache.xml needs the Ehcache 3
  schema (`// MIGRATION-TODO ehcache2` for the XML).

## fix-loop: commons-collections3
commons-collections 3.2.2 compiles on JDK 21 and sbmig runs no collections recipe.
- `package org.apache.commons.collections does not exist` → the jar lost its declaration (it often came
  transitively): declare `commons-collections:commons-collections` with `version: null`; keep the imports.
- The move to `org.apache.commons:commons-collections4` is a library replacement for the owners (Handover), not a
  build fix; only code already on collections4 gets `org.apache.commons:commons-collections4` (`version: null`).

## fix-loop: json-lib
json-lib 2.4 resolves only with `<classifier>jdk15</classifier>`; keep that while it is used.
- `package net.sf.json does not exist` → the dependency lost its classifier or version: restore
  `net.sf.json-lib:json-lib` with classifier `jdk15` (2.4 is the last release); do not port JSON code in the build fix.
- Code it pulled in transitively (`org.apache.commons.lang.*`, collections 3) needs those jars declared.

## fix-loop: jjwt
jjwt 0.9.1 needs `javax.xml.bind.DatatypeConverter` at runtime and its API changed in 0.12.
- Build fix: keep `io.jsonwebtoken:jjwt` as it is and add `javax.xml.bind:jaxb-api` (scope runtime, `version: null`);
  do not rename its javax import (it is inside the jar).
- Code already on 0.12+/0.13.0 (jjwt-api + jjwt-impl + jjwt-jackson): `Jwts.parserBuilder()` → `Jwts.parser()`,
  `setSigningKey(k).build().parseClaimsJws(t)` → `verifyWith(k).build().parseSignedClaims(t)`,
  `signWith(SignatureAlgorithm, secret)` → `signWith(Keys.hmacShaKeyFor(bytes))`.

## fix-loop: poi
POI 5 removed the int / short constants deprecated in 3.15–3.17.
- `Cell.CELL_TYPE_*` → `CellType.*`; `CellStyle.ALIGN_*` →
  `HorizontalAlignment.*`, `VERTICAL_*` → `VerticalAlignment.*`; `BORDER_*` → `BorderStyle.*`; fill patterns →
  `FillPatternType.*`; `Hyperlink.LINK_*` → `HyperlinkType.*`; `font.setBoldweight(BOLDWEIGHT_BOLD)` →
  `font.setBold(true)`; `setCellType(int)` → `setCellType(CellType)`.
- Artifacts (POI 5): poi-ooxml-schemas → poi-ooxml-lite, ooxml-schemas → poi-ooxml-full.
- `HSSFColor.<NAME>` inner classes → `HSSFColor.HSSFColorPredefined.<NAME>` / `IndexedColors.<NAME>`.

## fix-loop: commons-httpclient3
commons-httpclient 3.1 (the last release) still compiles on Java 21.
- `package org.apache.commons.httpclient does not exist` (the jar lost its version / declaration) → declare
  `commons-httpclient:commons-httpclient` (`version: null`); do not port GetMethod / PostMethod code in the build fix.
- New code: httpclient5 (`org.apache.hc.client5.http.*`) or RestClient; mark ports `// MIGRATION-TODO commons-httpclient3`.

## fix-loop: axis1
Axis 1 stubs are javax.xml.rpc / javax.xml.soap and have no Jakarta build.
- Keep `javax.xml.rpc.*` / `javax.xml.soap.*` imports in Axis stub and client code (never rename them to jakarta);
  declare `org.apache.axis:axis-jaxrpc` and `axis-saaj` (`version: null`) when they are missing.
- AxisServlet / server-side deployment cannot run on Tomcat 11: mark `// MIGRATION-TODO axis1` (JAX-WS / CXF).

## fix-loop: dbcp1
Runtime, not build: dbcp1 compiles. When the code was already moved to `org.apache.commons.dbcp2`:
- `setMaxActive` → `setMaxTotal`, `setMaxWait` → `setMaxWaitMillis`, `setRemoveAbandoned` →
  `setRemoveAbandonedOnBorrow` / `setRemoveAbandonedOnMaintenance`; the same renames apply to Spring XML
  `<property name="maxActive">` on `org.apache.commons.dbcp2.BasicDataSource`.

## fix-loop: itext
`com.lowagie:itext` 2.1.7 → `com.github.librepdf:openpdf` below 3.x keeps the `com.lowagie.text` packages (OpenPDF
3.x renamed them to org.openpdf.text: never jump to 3.x; version from the Dependency versions section). Missing in OpenPDF: `com.lowagie.tools.*`, some pdf.codec image
readers (Bmp / Gif / Png / Tiff / JBIG2), pdf.parser content-stream classes — mark `// MIGRATION-TODO itext`. OpenPDF
bundles the Korean CJK fonts (HYGoThic-Medium, HYSMyeongJo-Medium); drop itext-asian.

## fix-loop: slf4j-1-pin
No compile errors; logging goes silent (NOP) or duplicated. Remove explicit `org.slf4j:*` / logback versions (the
Boot BOM manages SLF4J 2), remove slf4j-log4j12 / slf4j-jdk14 / slf4j-simple and jcl-over-slf4j; keep
log4j-over-slf4j only for leftover Log4j 1 calls.

## fix-loop: commons-lang2
`package org.apache.commons.lang does not exist` (commons-lang 2.6 often arrived transitively — json-lib,
commons-configuration 1, an old in-house jar — and no longer does): declare `commons-lang:commons-lang` in that
module with `version: null`; keep the `org.apache.commons.lang.*` imports. The move to `commons-lang3` (and
`StringEscapeUtils` → commons-text) is the owners' replacement (Handover), not a build fix.

## fix-loop: log4j1
`package org.apache.log4j does not exist` (the jar lost its declaration or version): re-declare the as-is coordinate
`log4j:log4j` with `version: null`; keep the logging calls. `log4j.properties` / `log4j.xml` are not read by Boot
(Logback) — a startup matter; the move to SLF4J / Log4j 2 is the owners' (Handover). Spring's `Log4jConfigurer` /
`Log4jConfigListener` are gone: delete the call / registration (spring-framework-7.md).
