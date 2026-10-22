---
title: Hibernate ORM 7 (Boot 4) dropped hibernate-commons-annotations (org.hibernate.annotations.common.*)
keywords:
  - "org\\.hibernate\\.annotations\\.common"
  - "hibernate-commons-annotations"
  - "XClass|XProperty|XMethod|ReflectionManager|JavaReflectionManager|JavaXMember"
cases: [pure_java, spring_legacy, spring_boot, servlet_tomcat, msa]
phases: [spring, restructure]
---
# Hibernate Commons Annotations (HCANN) is gone in Hibernate ORM 7

`org.hibernate.common:hibernate-commons-annotations` was a transitive dependency of `hibernate-core` 3.x–6.x
(6.6.x still pulls 7.0.1.Final at runtime scope), so project code could import `org.hibernate.annotations.common.*`
without declaring the jar. Hibernate ORM 7.0 (every Spring Boot 4.x line) dropped it — `hibernate-models` replaced
it internally — and `hibernate-core` 7.x does not reference it at all. Symptom after the Boot 4 parent swap:
`package org.hibernate.annotations.common.util.impl does not exist` / `cannot find symbol class LoggerFactory`.
Last HCANN release: 7.0.3.Final (Maven Central; not managed by the Boot 4 BOM).

## fix-loop: hibernate-commons-annotations
`package org.hibernate.annotations.common.* does not exist` / `cannot find symbol … LoggerFactory | Log | XClass |
ReflectionManager`: remove the HCANN usage; never downgrade Hibernate or Boot to get the jar back.
- `org.hibernate.annotations.common.util.impl.LoggerFactory` / `Log` (usually an IDE auto-import that meant SLF4J):
  `LoggerFactory.make(Foo.class)` / `.make(Foo.class, "category")` → `org.jboss.logging.Logger.getLogger(Foo.class)`
  with the field typed `org.jboss.logging.Logger` (`Log extends BasicLogger`, so `info / debug / warn / error /
  infof / debugf / tracev` calls keep compiling; jboss-logging stays on the classpath through hibernate-core 7).
  Switch to `org.slf4j.LoggerFactory.getLogger(Foo.class)` only when every call is `info/debug/warn/error/trace(String …)`
  (SLF4J has no `infof` / `debugv`).
- `org.hibernate.annotations.common.AssertionFailure` → `org.hibernate.AssertionFailure`.
- `org.hibernate.annotations.common.reflection.*` (`XClass`, `XProperty`, `XMethod`, `ReflectionManager`,
  `JavaReflectionManager`): no public replacement (Hibernate 7 uses `hibernate-models` internally). Port to
  `java.lang.reflect` (`Class`, `Field`, `Method`, `getAnnotation`) keeping the public signatures of the project
  class; if a usage cannot be ported with confidence, keep the class compiling and add
  `// MIGRATION-TODO: hibernate-commons-annotations reflection not ported` so the Handover lists the file.
- Fallback only when the source cannot be changed (generated / vendor code): declare
  `org.hibernate.common:hibernate-commons-annotations:7.0.3.Final` explicitly in that module's pom (compile scope;
  it still resolves and compiles against jboss-logging) and say so in the analysis — it is EOL and the owners
  must plan its removal.
- Never add `hibernate-core` 5.x / 6.x back and never pin `hibernate.version` to an older line.
