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
- `@TypeDef`/`@TypeDefs` removed → register via `@Type(MyUserType.class)` / `@JavaType` / `@JdbcType`.
- `UserType` is generic `UserType<T>` with new method signatures (`nullSafeGet(ResultSet, int, SharedSessionContractImplementor, Object)`).
- `org.hibernate.type.StringType.INSTANCE` etc. removed → `StandardBasicTypes.STRING`.
- Legacy `org.hibernate.Criteria` / `session.createCriteria(..)` / `Restrictions` removed → JPA `CriteriaBuilder`.
- Hibernate 7: `Session.save/saveOrUpdate/update/delete` removed → `persist` / `merge` / `remove`; `session.load` → `getReference`.
- `hibernate.dialect` is auto-detected; remove `MySQL5Dialect`/`MySQL57Dialect`/`MySQL8Dialect`/`PostgreSQL95Dialect`/
  `Oracle12cDialect` (removed classes) → delete the property or use `MySQLDialect`/`PostgreSQLDialect`/`OracleDialect`.
- `@GenericGenerator(strategy="uuid2")` → `@UuidGenerator`; `@org.hibernate.annotations.Where` → `@SQLRestriction`.
- `query.setResultTransformer(Transformers.aliasToBean(X.class))` deprecated → `TupleTransformer` / DTO constructor query.
- Positional JPQL parameters `?` without index are rejected → `?1` or named parameters.
