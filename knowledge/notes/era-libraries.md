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
cases: [pure_java, spring_legacy, spring_boot, servlet_tomcat, msa]
phases: [spring, restructure]
---
# Era libraries on Spring 7 / Boot 4

Libraries that 2013–2020 Spring 3/4 / Boot 1–2 projects use and that Spring 7 / Boot 4.1 no longer carry. Each
`## fix-loop: <library>` section is the guidance the build-fix loop gets when the matching advisory fired
(knowledge/libraries/advisories.yml, docs/ADVISORIES.md "2026-10-22 era libraries"). Where no direct mapping is
known: keep the public signatures, port only what the error names, mark the rest `// MIGRATION-TODO <advisory id>`.

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
  commons-fileupload2-jakarta-servlet6 2.0.0-M5) — or port to `MultipartFile` / `Part`.
- `CommonsMultipartResolver` bean → delete it (Boot multipart / `StandardServletMultipartResolver`); limits go to
  `spring.servlet.multipart.max-file-size` / `max-request-size` / `file-size-threshold`. Controllers taking
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
  with org.apache.velocity:velocity-engine-core 2.4.1.
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
After UpgradeApacheCommonsCollections_3_4 the imports are `org.apache.commons.collections4.*`.
- `package org.apache.commons.collections4 does not exist` → add org.apache.commons:commons-collections4 4.5.0.
- Generics errors left by the recipe → add the type arguments the collections4 signatures ask for; keep behaviour.
- Remaining `org.apache.commons.collections.*` imports → the collections4 class of the same name.

## fix-loop: json-lib
json-lib 2.4 resolves only with `<classifier>jdk15</classifier>`; keep that while it is used.
- `package net.sf.json does not exist` → the dependency lost its classifier or version: restore
  net.sf.json-lib:json-lib 2.4 classifier jdk15; do not port JSON code inside the build fix.
- Code it pulled in transitively (`org.apache.commons.lang.*`, collections 3) needs those jars declared.

## fix-loop: jjwt
jjwt 0.9.1 needs `javax.xml.bind.DatatypeConverter` at runtime and its API changed in 0.12.
- Build fix: keep `io.jsonwebtoken:jjwt` 0.9.1 and add `javax.xml.bind:jaxb-api` 2.3.1 (scope runtime); do not
  rename its javax import (it is inside the jar).
- Code already on 0.12+/0.13.0 (jjwt-api + jjwt-impl + jjwt-jackson): `Jwts.parserBuilder()` → `Jwts.parser()`,
  `setSigningKey(k).build().parseClaimsJws(t)` → `verifyWith(k).build().parseSignedClaims(t)`,
  `signWith(SignatureAlgorithm, secret)` → `signWith(Keys.hmacShaKeyFor(bytes))`.

## fix-loop: poi
POI 5 removed the int / short constants deprecated in 3.15–3.17.
- `Cell.CELL_TYPE_*` → `CellType.*`; `CellStyle.ALIGN_*` →
  `HorizontalAlignment.*`, `VERTICAL_*` → `VerticalAlignment.*`; `BORDER_*` → `BorderStyle.*`; fill patterns →
  `FillPatternType.*`; `Hyperlink.LINK_*` → `HyperlinkType.*`; `font.setBoldweight(BOLDWEIGHT_BOLD)` →
  `font.setBold(true)`; `setCellType(int)` → `setCellType(CellType)`.
- Artifacts: poi-ooxml-schemas → poi-ooxml-lite, ooxml-schemas → poi-ooxml-full (5.5.1).
- `HSSFColor.<NAME>` inner classes → `HSSFColor.HSSFColorPredefined.<NAME>` / `IndexedColors.<NAME>`.

## fix-loop: commons-httpclient3
commons-httpclient 3.1 (the last release) still compiles on Java 21.
- `package org.apache.commons.httpclient does not exist` (the jar lost its version / declaration) → declare
  commons-httpclient:commons-httpclient 3.1 explicitly; do not port GetMethod / PostMethod code inside the build fix.
- New code: httpclient5 (`org.apache.hc.client5.http.*`) or RestClient; mark ports `// MIGRATION-TODO commons-httpclient3`.

## fix-loop: axis1
Axis 1 stubs are javax.xml.rpc / javax.xml.soap and have no Jakarta build.
- Keep `javax.xml.rpc.*` / `javax.xml.soap.*` imports in Axis stub and client code (never rename them to jakarta);
  declare org.apache.axis:axis-jaxrpc and axis-saaj 1.4 explicitly when they are missing.
- AxisServlet / server-side deployment cannot run on Tomcat 11: mark `// MIGRATION-TODO axis1` (JAX-WS / CXF).

## fix-loop: dbcp1
Runtime, not build: dbcp1 compiles. When the code was already moved to `org.apache.commons.dbcp2`:
- `setMaxActive` → `setMaxTotal`, `setMaxWait` → `setMaxWaitMillis`, `setRemoveAbandoned` →
  `setRemoveAbandonedOnBorrow` / `setRemoveAbandonedOnMaintenance`; the same renames apply to Spring XML
  `<property name="maxActive">` on `org.apache.commons.dbcp2.BasicDataSource`.

## fix-loop: itext
`com.lowagie:itext` 2.1.7 → `com.github.librepdf:openpdf` 2.4.0 keeps the `com.lowagie.text` packages (never jump to
OpenPDF 3.x: it renamed them to org.openpdf.text). Missing in OpenPDF: `com.lowagie.tools.*`, some pdf.codec image
readers (Bmp / Gif / Png / Tiff / JBIG2), pdf.parser content-stream classes — mark `// MIGRATION-TODO itext`. OpenPDF
bundles the Korean CJK fonts (HYGoThic-Medium, HYSMyeongJo-Medium); drop itext-asian.

## fix-loop: slf4j-1-pin
No compile errors; logging goes silent (NOP) or duplicated. Remove explicit `org.slf4j:*` / logback versions (Boot
4.1 manages SLF4J 2.0.18), remove slf4j-log4j12 / slf4j-jdk14 / slf4j-simple and jcl-over-slf4j; keep
log4j-over-slf4j only for leftover Log4j 1 calls.
