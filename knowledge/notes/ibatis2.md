---
title: iBATIS 2 (SqlMapClient) -> MyBatis 3 (Spring 4+ removed orm.ibatis)
keywords:
  - "org\\.springframework\\.orm\\.ibatis"
  - "com\\.ibatis"
  - "SqlMapClient(DaoSupport|Template|FactoryBean|Callback)?"
  - "ibatis-sqlmap|ibatis-core|sqlMapConfig|sql-map-2|sql-map-config-2"
cases: [spring_legacy, spring_boot, servlet_tomcat, msa]
phases: [spring]
---
# iBATIS 2 → MyBatis 3

Spring 3.2 was the last Spring line with `org.springframework.orm.ibatis`; Spring 4.0 removed it. The iBATIS 2
jars (`org.apache.ibatis:ibatis-sqlmap` 2.3.x, packages `com.ibatis.*`) still resolve from Maven Central, so the
pom is fine and the failure shows up only at compile time after the Spring upgrade. No OpenRewrite recipe exists.

## fix-loop: ibatis2
`package org.springframework.orm.ibatis does not exist` / `cannot find symbol SqlMapClientDaoSupport |
SqlMapClientTemplate | SqlMapClientFactoryBean`: port the DAO to MyBatis 3, never re-add iBATIS or an old Spring.
- Keep every public DAO signature (interface, method names, parameter and return types); callers stay untouched.
- `extends SqlMapClientDaoSupport` → inject `org.mybatis.spring.SqlSessionTemplate` (constructor or setter) and
  call it directly: `getSqlMapClientTemplate().queryForList(id, p)` → `sqlSession.selectList(id, p)`,
  `queryForObject` → `selectOne`, `insert` / `update` / `delete` keep their names (`insert` returns an int, not the key).
- Statement ids: MyBatis looks them up as `<namespace>.<id>`; keep the sqlmap namespace as the mapper namespace
  and pass the same `"ns.id"` strings.
- One mapper XML per sqlmap namespace (`<sqlMap namespace>` → `<mapper namespace>`, DOCTYPE
  `-//mybatis.org//DTD Mapper 3.0//EN`): `#name#` → `#{name}`, `$name$` → `${name}`, `parameterClass` →
  `parameterType`, `resultClass` → `resultType`, `<isNotEmpty property="x">` → `<if test="x != null and x != ''">`,
  `<iterate>` → `<foreach>`, `<dynamic prepend="WHERE">` → `<where>`.
- Spring XML: `SqlMapClientFactoryBean` → `org.mybatis.spring.SqlSessionFactoryBean` (`configLocation` →
  `mapperLocations`), plus a `SqlSessionTemplate` bean; `<sqlMapConfig>` → `<configuration>`.
- pom: add `org.mybatis:mybatis` (3.5.x line) + `org.mybatis:mybatis-spring` (3.0.x line or later), or
  `org.mybatis.spring.boot:mybatis-spring-boot-starter`, which manages both (the starter itself is not in the
  Spring Boot BOM: give it an explicit version); remove `ibatis-sqlmap` / `ibatis-core`.
- If a statement cannot be translated with confidence, keep the DAO compiling and add
  `// MIGRATION-TODO: iBATIS statement <id> not ported` — the Handover lists the files for the owning team.
