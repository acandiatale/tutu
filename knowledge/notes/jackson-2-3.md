---
title: Jackson 2 → 3 under the target Boot BOM (decision rules A-D)
keywords:
  - "com\\.fasterxml\\.jackson\\.(databind|core|datatype|module|dataformat)"
  - "tools\\.jackson\\."
  - "cannot access ObjectMapper|class file for com\\.fasterxml\\.jackson"
  - "MappingJackson2\\w*|Jackson2ObjectMapper\\w*|MappingJackson\\w*View"
  - "JsonProcessingException|JsonMappingException|JacksonException"
  - "ObjectMapper|JsonMapper"
  - "spring-boot-jackson2|jackson-databind"
cases: []
phases: [spring]
---
# Jackson 2 vs Jackson 3 (Spring Framework 7, Boot 4.1)

## Facts
- The Boot 4.1 BOM manages both lines: Jackson 3.1.x (`tools.jackson.*`, groupId `tools.jackson.core`) and Jackson 2.21.x
  (`com.fasterxml.jackson.*`, through `jackson-2-bom`). A dependency on either line takes `version: null`.
- `spring-boot-starter-webmvc` → `spring-boot-starter-jackson` brings only `tools.jackson.core:jackson-databind` (3);
  no Jackson 2 databind arrives transitively.
- Annotations stay `com.fasterxml.jackson.annotation.*` on both lines (`@JsonProperty`, `@JsonIgnore`, `@JsonFormat` …,
  artifact `com.fasterxml.jackson.core:jackson-annotations`) — never rename them.
- The recipes already moved code and poms to Jackson 3 wherever the types resolved (`UpgradeSpringFramework_7_0` ends
  with `UpgradeJackson_2_3`; Boot projects run it inside `UpgradeSpringBoot_4_0`). What reaches you is what they could not resolve.
- Spring 7 keeps its Jackson 2 classes (deprecated, they compile): `MappingJackson2HttpMessageConverter`,
  `MappingJackson2JsonView`, `Jackson2ObjectMapperBuilder`, `Jackson2ObjectMapperFactoryBean`. They take
  `com.fasterxml.jackson.databind.ObjectMapper` and need Jackson 2 databind on the classpath.
- Typical errors: `cannot access ObjectMapper … class file for com.fasterxml.jackson.databind.ObjectMapper not found`
  (a Jackson 2 class used without Jackson 2 databind); `package com.fasterxml.jackson.databind does not exist` (a file
  left on Jackson 2); `tools.jackson.databind.ObjectMapper cannot be converted to com.fasterxml.jackson.databind.ObjectMapper`.

## Rule A — the module is on Jackson 3: finish the move in the failing file
The module's pom or its other files use `tools.jackson`: change the failing file's imports to `tools.jackson.*` and
- `JsonProcessingException` / `JsonMappingException` → `tools.jackson.core.JacksonException` (unchecked: drop a
  `throws` / `catch` that only existed for it, keep public signatures otherwise);
- `MappingJackson2HttpMessageConverter` → `org.springframework.http.converter.json.JacksonJsonHttpMessageConverter`,
  `MappingJackson2JsonView` → `org.springframework.web.servlet.view.json.JacksonJsonView` (XML twins:
  `JacksonXmlHttpMessageConverter`, `JacksonXmlView`). The twins take `tools.jackson.databind.json.JsonMapper` (or its
  `Builder`), not `ObjectMapper`; they have no `setObjectMapper` — configure the mapper first and pass it to the
  constructor; `getMapper()` replaces `getObjectMapper()`;
- `Jackson2ObjectMapperBuilder` / `Jackson2ObjectMapperFactoryBean` → `JsonMapper.builder()…build()` (Spring has no
  Jackson 3 version of its builder);
- a mapper bean handed to the twins must be declared as `JsonMapper` (`(JsonMapper) mapper` only when it is one).
This finishes the recipe's work; it is not a library swap. Never revert the module to Jackson 2.

## Rule B — a library needs Jackson 2 types
The code exchanges Jackson 2 types with a library compiled against Jackson 2 (an in-house jar, `jjwt-jackson`, a
signature naming `com.fasterxml.jackson.databind.*`): keep THAT file on `com.fasterxml.jackson.*` and add
`com.fasterxml.jackson.core:jackson-databind` (`version: null`) to that module. Both lines coexist on the classpath.

## Rule C — nothing in the module is on Jackson 3
`package com.fasterxml.jackson.databind does not exist` and no file / pom of the module uses `tools.jackson`: the
smallest fix is `com.fasterxml.jackson.core:jackson-databind` with `version: null`; the imports stay.

## Rule D — converted projects (`spring_legacy`, `servlet_tomcat`, `pure_java`)
Never add `org.springframework.boot:spring-boot-jackson2`: it brings Boot's Jackson 2 auto-configuration, which the
compile does not need (rules B / C add only `jackson-databind`). In a Boot project it is only for
`org.springframework.boot.jackson2.*` types or an injected Jackson 2 `ObjectMapper` bean.

## Not a build fix
- XML beans of class `MappingJackson2JsonView` (eGovFrame "jsonView"), `MappingJackson2HttpMessageConverter`,
  `Jackson2ObjectMapperFactoryBean` or `com.fasterxml…ObjectMapper` in `legacy-config/` compile, but fail at startup
  without Jackson 2 databind: a startup matter for the owners (Handover) — never edit the XML for the build.
- Jackson 3 defaults differ (e.g. dates written as ISO strings): add `// MIGRATION-TODO` where a fix changes serialization.
