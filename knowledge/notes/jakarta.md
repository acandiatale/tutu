---
title: javax -> jakarta namespace (Jakarta EE 9+ / EE 11)
keywords:
  - "javax\\.(servlet|persistence|validation|annotation|inject|transaction|ws\\.rs|xml\\.bind|mail|jms|websocket|el|faces|ejb|enterprise|batch|json|security\\.enterprise)"
  - "jakarta\\."
  - "package javax\\.\\S+ does not exist"
  - "hibernate|@Type|TypeDef|Criteria|Dialect|createCriteria|UserType"
  - "querydsl|JPAQueryFactory|com\\.querydsl"
  - "EntityManager|@Entity|@Column|@Table|@Id"
  - "ConstraintValidator|@NotNull|@NotBlank|@Valid"
  - "HttpServletRequest|HttpServletResponse|HttpSession|ServletContext|Filter"
  - "PagingAndSortingRepository|ListCrudRepository|no suitable method found for findAll"
  - "org\\.hibernate\\.(Query|SQLQuery|Criteria)|createSQLQuery|createCriteria|Restrictions|uniqueResult|setResultTransformer"
cases: [pure_java, spring_legacy, spring_boot, servlet_tomcat, msa]
phases: [spring, jdk21]
---
# javax → jakarta (Spring 6/7, Boot 3/4 = Jakarta EE 10/11)

## Package mapping (rename the import prefix `javax.X` → `jakarta.X`)
| javax package | jakarta package | Artifact (Boot 4 / EE 11 version) |
|---|---|---|
| `javax.servlet[.http]` | `jakarta.servlet[.http]` | `jakarta.servlet:jakarta.servlet-api:6.1.0` (provided; embedded Tomcat 11 ships it) |
| `javax.servlet.jsp[.jstl]` | `jakarta.servlet.jsp[.jstl]` | `jakarta.servlet.jsp:jakarta.servlet.jsp-api:4.0`, JSTL `jakarta.servlet.jsp.jstl:jakarta.servlet.jsp.jstl-api:3.0` + `org.glassfish.web:jakarta.servlet.jsp.jstl` |
| `javax.persistence` | `jakarta.persistence` | `jakarta.persistence:jakarta.persistence-api:3.2.0` |
| `javax.validation` | `jakarta.validation` | `jakarta.validation:jakarta.validation-api:3.1.x` + Hibernate Validator 9 (`spring-boot-starter-validation`) |
| `javax.annotation` (PostConstruct, PreDestroy, Resource, Priority, security.RolesAllowed) | `jakarta.annotation` | `jakarta.annotation:jakarta.annotation-api:3.0.0` |
| `javax.inject` | `jakarta.inject` | `jakarta.inject:jakarta.inject-api:2.0.1` |
| `javax.transaction` (Transactional, UserTransaction) | `jakarta.transaction` | `jakarta.transaction:jakarta.transaction-api:2.0.1` (note: `javax.transaction.xa` stays javax — it is JDK) |
| `javax.ws.rs` | `jakarta.ws.rs` | `jakarta.ws.rs:jakarta.ws.rs-api:4.0.0` (Jersey 4 / RESTEasy 7) |
| `javax.xml.bind` | `jakarta.xml.bind` | `jakarta.xml.bind:jakarta.xml.bind-api:4.0.x` + `org.glassfish.jaxb:jaxb-runtime:4.0.x` |
| `javax.mail` | `jakarta.mail` | `jakarta.mail:jakarta.mail-api:2.1.x` + `org.eclipse.angus:angus-mail` (`spring-boot-starter-mail`) |
| `javax.jms` | `jakarta.jms` | `jakarta.jms:jakarta.jms-api:3.1.0` |
| `javax.websocket` | `jakarta.websocket` | `jakarta.websocket:jakarta.websocket-api:2.2.0` (+ `-client-api`) |
| `javax.el` | `jakarta.el` | `jakarta.el:jakarta.el-api:6.0` |
| `javax.faces` | `jakarta.faces` | `jakarta.faces:jakarta.faces-api:4.1` |
| `javax.ejb`, `javax.enterprise` (CDI), `javax.json`, `javax.batch`, `javax.security.enterprise` | `jakarta.*` same suffix | matching `jakarta.*-api` |

JSP / tag files are not compiled by the build (Tomcat compiles them at runtime) and OpenRewrite skips them: the
same packages inside `<%@ page|tag import="…" %>`, scriptlets (`<% %>`, `<%= %>`, `<%! %>`) and
`<jsp:useBean class|type>` still say `javax.*` — rename them where a fix touches them. Never rename taglib URIs
(`http://java.sun.com/jsp/jstl/core` is a URI, not a package — JSTL 3 accepts it) or EL / HTML text.

## Stays `javax.*` (part of the JDK — never rename)
`javax.sql`, `javax.crypto`, `javax.naming`, `javax.net`, `javax.net.ssl`, `javax.swing`, `javax.imageio`,
`javax.security.auth`, `javax.security.cert`, `javax.management`, `javax.xml.parsers`, `javax.xml.transform`,
`javax.xml.xpath`, `javax.xml.stream`, `javax.xml.namespace`, `javax.xml.crypto`, `javax.script`, `javax.tools`,
`javax.annotation.processing`, `javax.lang.model`, `javax.print`, `javax.sound`, `javax.transaction.xa`, `javax.cache` (JCache, JSR-107).

## Artifact swaps (old → new; remove the old one, Boot manages versions where noted)
- `javax.servlet:javax.servlet-api` / `javax.servlet:servlet-api` → `jakarta.servlet:jakarta.servlet-api` (scope provided)
- `javax.persistence:javax.persistence-api` / `org.hibernate.javax.persistence:hibernate-jpa-2.1-api` → `jakarta.persistence:jakarta.persistence-api`
- `javax.validation:validation-api` → `jakarta.validation:jakarta.validation-api`; `org.hibernate:hibernate-validator` → `org.hibernate.validator:hibernate-validator`
- `org.hibernate:hibernate-core` / `hibernate-entitymanager` → `org.hibernate.orm:hibernate-core` (entitymanager merged into core)
- `org.hibernate:hibernate-jpamodelgen` → `org.hibernate.orm:hibernate-processor` (Boot 4)
- `javax.annotation:javax.annotation-api` → `jakarta.annotation:jakarta.annotation-api`
- `javax.inject:javax.inject` → `jakarta.inject:jakarta.inject-api`
- `javax.xml.bind:jaxb-api` + `com.sun.xml.bind:jaxb-impl` → `jakarta.xml.bind:jakarta.xml.bind-api` + `org.glassfish.jaxb:jaxb-runtime`
- `com.sun.mail:javax.mail` → `org.eclipse.angus:angus-mail`
- `org.ehcache:ehcache` 3 → add `<classifier>jakarta</classifier>`; Ehcache 2 (`net.sf.ehcache`) unsupported.
- QueryDSL: `com.querydsl:querydsl-jpa` and `com.querydsl:querydsl-apt` need `<classifier>jakarta</classifier>`
  (5.1.0); apt processor in `<annotationProcessorPaths>` also with classifier `jakarta`, plus
  `jakarta.persistence:jakarta.persistence-api`. Generated `Q*` classes import `jakarta.annotation.Generated`.
  Maintained fork: `io.github.openfeign.querydsl:querydsl-jpa` (6.x/7.x, jakarta by default).

## Hibernate 6.x / 7.x (Boot 3 = 6.x, Boot 4.1 = Hibernate 7.4)
- `@Type(type = "org.hibernate.type.TextType")` → remove or `@JdbcTypeCode(SqlTypes.LONGVARCHAR)`;
  `@Type(type="json")` (hibernate-types) → `@JdbcTypeCode(SqlTypes.JSON)`; custom → `@Type(MyUserType.class)`.
- `UserType` is generic `UserType<T>` with new method signatures (`nullSafeGet(ResultSet, int, SharedSessionContractImplementor, Object)`).
- Hibernate 7: `Session.save/saveOrUpdate/update/delete` removed → `persist` / `merge` / `remove`; `session.load` → `getReference`.
- Hibernate 7 dropped `hibernate-commons-annotations`: `org.hibernate.annotations.common.*` (`LoggerFactory`, `XClass`)
  no longer resolves → see `hibernate-7.md` (jboss-logging `Logger.getLogger`, plain reflection).
- `hibernate.dialect` is auto-detected; remove `MySQL5Dialect`/`MySQL57Dialect`/`MySQL8Dialect`/`PostgreSQL95Dialect`/
  `Oracle12cDialect` (removed classes) → delete the property or use `MySQLDialect`/`PostgreSQLDialect`/`OracleDialect`.
- `@GenericGenerator(strategy="uuid2")` → `@UuidGenerator`; `@org.hibernate.annotations.Where` → `@SQLRestriction`.
- `@TypeDef` / `@TypeDefs` and the query-API removals: see "Hibernate 6+ removed query API" below.

## Hibernate 6+ removed query API (Hibernate 6.0 migration guide)
- Legacy Criteria API removed: `org.hibernate.Criteria`, `Session.createCriteria(..)`, `Restrictions`, `Projections`,
  `org.hibernate.criterion.*` → JPA `CriteriaBuilder` / `CriteriaQuery` (`session.getCriteriaBuilder()`). There is no
  mechanical mapping: port the query, keep the public method signatures, add `// MIGRATION-TODO` when the semantics
  are unclear (e.g. `Restrictions.sqlRestriction`, `DetachedCriteria` subqueries).
- `org.hibernate.Query` / `org.hibernate.SQLQuery` (deprecated since 5.2) are gone → `org.hibernate.query.Query<R>` /
  `org.hibernate.query.NativeQuery<R>`; `session.createQuery(hql)` → `session.createQuery(hql, Result.class)`;
  `session.createSQLQuery(sql)` → `session.createNativeQuery(sql, Result.class)` (or `createNativeQuery(sql)` for
  `Object[]` rows).
- Typed setters `setString` / `setInteger` / `setLong` / `setDate` / `setTimestamp` / `setBoolean` … removed →
  `setParameter(name, value)` (`setParameter(name, value, StandardBasicTypes.X)` when the type must be explicit).
- `Query#iterate()` removed → `stream()` / `getResultStream()` (close the stream) or `list()`.
- Still present (compile fine): `list()`, `uniqueResult()`, `setParameterList(..)`, `setFirstResult` / `setMaxResults`.
- `query.setResultTransformer(Transformers.aliasToBean(X.class))` deprecated → `setTupleTransformer` / DTO
  constructor query (`select new com.x.Dto(..)`).
- Ordinal parameters are 1-based `?1`, `?2`; a bare `?` in HQL/JPQL is rejected → `?1` or named parameters.
- `@TypeDef` / `@TypeDefs` removed → register via `@Type(MyUserType.class)` / `@JavaType` / `@JdbcType`.
- `BasicType` singletons removed (`org.hibernate.type.StringType.INSTANCE`, `LongType.INSTANCE` …) →
  `StandardBasicTypes.STRING` / `StandardBasicTypes.LONG`, on mappings `@JdbcTypeCode(SqlTypes.X)`.

## Spring Data 3+ repositories (Boot 3 / 4)
- Spring Data 3.0: `PagingAndSortingRepository` no longer extends `CrudRepository`. Symptoms on a repository that
  extends only the paging interface: `no suitable method found for findAll(no arguments)`, `cannot find symbol:
  method save(…)` / `findById(…)` / `deleteById(…)` / `count()`.
  Fix: add `ListCrudRepository<T, ID>` (or `CrudRepository<T, ID>`) next to the paging interface with the same type
  arguments, or switch to `JpaRepository<T, ID>` — on the repository interface (named in `location:`), not at
  every call site.
- `getOne(id)` / `getById(id)` deprecated → `getReferenceById(id)`; `deleteInBatch(..)` → `deleteAllInBatch(..)`
  (both still present in Spring Data JPA 4.1, so the build compiles — change them only when you touch the file).
- `PageRequest.of(..)`, `Sort.by(..)`, `Pageable`, derived query methods: unchanged.
