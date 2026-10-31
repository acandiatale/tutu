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
  - "HttpServletRequest|HttpServletResponse|HttpSession|ServletContext|\\b(Filter|FilterChain|FilterConfig)\\b"
  - "PagingAndSortingRepository|ListCrudRepository|no suitable method found for findAll"
  - "org\\.hibernate\\.(Query|SQLQuery|Criteria)|createSQLQuery|createCriteria|Restrictions|uniqueResult|setResultTransformer"
  - "org\\.hibernate\\.annotations\\.(Where|WhereJoinTable|LazyCollection|LazyToOne|IndexColumn|ForeignKey|Table|Loader|OrderBy|Sort|Proxy|Polymorphism|SelectBeforeUpdate|Persister|Tuplizer)\\b"
  - "org\\.hibernate\\.validator\\.constraints\\.(NotBlank|NotEmpty|Email|SafeHtml)"
  - "\\bfindOne\\(|method exists\\(|save\\(java\\.lang\\.Iterable|QueryDslPredicateExecutor|has protected access in org\\.springframework\\.data|new PageRequest|new Sort\\("
cases: [pure_java, spring_legacy, spring_boot, servlet_tomcat, msa]
phases: [spring, jdk21]
---
# javax → jakarta (Spring 6/7, Boot 3/4 = Jakarta EE 10/11)

## Package mapping (rename the import prefix `javax.X` → `jakarta.X`; artifacts take `version: null` — Boot-managed unless noted)
| javax package | jakarta package | Artifact |
|---|---|---|
| `javax.servlet[.http]` | `jakarta.servlet[.http]` | `jakarta.servlet:jakarta.servlet-api` (`provided`; embedded Tomcat 11 ships it) |
| `javax.servlet.jsp[.tagext]` | `jakarta.servlet.jsp[.tagext]` | `org.apache.tomcat.embed:tomcat-embed-jasper` (`provided`; `jakarta.servlet.jsp-api` is not Boot-managed); JSTL `jakarta.servlet.jsp.jstl:jakarta.servlet.jsp.jstl-api` + `org.glassfish.web:jakarta.servlet.jsp.jstl` |
| `javax.persistence` | `jakarta.persistence` | `jakarta.persistence:jakarta.persistence-api` |
| `javax.validation` | `jakarta.validation` | `jakarta.validation:jakarta.validation-api` + Hibernate Validator 9 (`spring-boot-starter-validation`) |
| `javax.annotation` (PostConstruct, PreDestroy, Resource, Priority, security.RolesAllowed) | `jakarta.annotation` | `jakarta.annotation:jakarta.annotation-api` |
| `javax.inject` | `jakarta.inject` | `jakarta.inject:jakarta.inject-api` |
| `javax.transaction` (Transactional, UserTransaction) | `jakarta.transaction` | `jakarta.transaction:jakarta.transaction-api` (`javax.transaction.xa` stays javax — it is JDK) |
| `javax.ws.rs` | `jakarta.ws.rs` | `jakarta.ws.rs:jakarta.ws.rs-api` (Jersey / RESTEasy jakarta lines) |
| `javax.xml.bind` | `jakarta.xml.bind` | `jakarta.xml.bind:jakarta.xml.bind-api` + `org.glassfish.jaxb:jaxb-runtime` |
| `javax.mail` | `jakarta.mail` | `jakarta.mail:jakarta.mail-api` + `org.eclipse.angus:angus-mail` (`spring-boot-starter-mail`) |
| `javax.jms` | `jakarta.jms` | `jakarta.jms:jakarta.jms-api` |
| `javax.websocket` | `jakarta.websocket` | `jakarta.websocket:jakarta.websocket-api` (+ `-client-api`) |
| `javax.el` | `jakarta.el` | `org.apache.tomcat.embed:tomcat-embed-el` (`jakarta.el:jakarta.el-api` is not Boot-managed) |
| `javax.faces` | `jakarta.faces` | `jakarta.faces:jakarta.faces-api` (not Boot-managed) |
| `javax.ejb`, `javax.enterprise` (CDI), `javax.json`, `javax.batch`, `javax.security.enterprise` | `jakarta.*` same suffix | matching `jakarta.*-api` |
Spring 7 ignores `javax.annotation` / `javax.inject` annotations: rename them, never add `javax.annotation-api` back.

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
  (Boot-managed through `querydsl-bom`); apt processor in `<annotationProcessorPaths>` also with classifier `jakarta`, plus
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
- `@GenericGenerator(strategy="uuid2")` → `@UuidGenerator` (`@GenericGenerator` itself still compiles, deprecated).
- Annotations removed in Hibernate 7.4 (`org.hibernate.annotations.*`): `@Where` → `@SQLRestriction`,
  `@WhereJoinTable` → `@SQLJoinTableRestriction`, `@Loader` → `@SQLSelect`, `@OrderBy` → `@SQLOrder` (or JPA
  `@jakarta.persistence.OrderBy`), `@IndexColumn` → `@jakarta.persistence.OrderColumn`, `@ForeignKey` →
  `@JoinColumn(foreignKey = @jakarta.persistence.ForeignKey(name = "…"))`, `@Table` → JPA `@Table` (+ `@Comment` /
  `@Check`), `@Sort` → `@SortNatural` / `@SortComparator`, `@LazyCollection` / `@LazyToOne` → the JPA `fetch` attribute;
  `@Proxy`, `@Polymorphism`, `@SelectBeforeUpdate`, `@Persister`, `@Tuplizer` → delete + `// MIGRATION-TODO`.
- Hibernate Validator 9.1 removed `org.hibernate.validator.constraints.NotBlank` / `NotEmpty` / `Email` →
  `jakarta.validation.constraints.*`; `SafeHtml` has no replacement (delete + MIGRATION-TODO).
- `@TypeDef` / `@TypeDefs` and the query-API removals: see "Hibernate 6+ removed query API" below.

## Hibernate 6+ removed query API (Hibernate 6.0 migration guide)
- Legacy Criteria API removed: `org.hibernate.Criteria`, `Session.createCriteria(..)`, `Restrictions`, `Projections`,
  `org.hibernate.criterion.*` → JPA `CriteriaBuilder` / `CriteriaQuery` (`session.getCriteriaBuilder()`). There is no
  mechanical mapping: port the query, keep the public method signatures, add `// MIGRATION-TODO` when the semantics
  are unclear (e.g. `Restrictions.sqlRestriction`, `DetachedCriteria` subqueries).
- `org.hibernate.Query` / `org.hibernate.SQLQuery` (deprecated since 5.2) are gone → `org.hibernate.query.Query` /
  `org.hibernate.query.NativeQuery`. Raw `session.createQuery(hql)` still exists in 7.4 (deprecated, returns
  `org.hibernate.query.Query`): when only the import fails, change the import and keep the call; add the result class
  (`createQuery(hql, Result.class)`) only where javac asks. `session.createSQLQuery(sql)` → `session.createNativeQuery(sql)`
  (`Object[]` rows) or `createNativeQuery(sql, Result.class)`.
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

## Spring Data 1.x → 2+ repositories (Spring 4 projects; no recipe covers 1.x → 2.0)
- `findOne(id)` → `findById(id).orElse(null)`; `exists(id)` → `existsById(id)`; `delete(id)` → `deleteById(id)`;
  `save(Iterable)` → `saveAll(..)`; `findAll(Iterable<ID>)` → `findAllById(..)`; `delete(Iterable)` → `deleteAll(..)`.
- `new PageRequest(page, size[, sort])` (protected now) → `PageRequest.of(..)`; `new Sort(..)` → `Sort.by(..)`;
  `QueryDslPredicateExecutor` → `org.springframework.data.querydsl.QuerydslPredicateExecutor`.
- Change the repository's callers only where javac reports them; keep the service signatures.
