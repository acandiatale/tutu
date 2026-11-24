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
  - "Jackson2(Json|Smile|Cbor)(Decoder|Encoder)"
  - "jackson2(Json|Smile)(Decoder|Encoder)|JacksonJson(Decoder|Encoder)|CodecConfigurer|configureHttpMessageCodecs|ExchangeStrategies"
  - "exception (JacksonException|DatabindException|StreamReadException|StreamWriteException) has already been caught"
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
  (a class compiled against Jackson 2 is called without Jackson 2 databind; the same jar may hold a Jackson 3 class:
  choose the rule below first, Jackson 2 databind is added only under Rule B / C); `package
  com.fasterxml.jackson.databind does not exist` (a file left on Jackson 2, or Jackson 2 types written under Rule B
  before the dependency is there); `package tools.jackson.… does not
  exist` / `class file for tools.jackson.… not found` (Jackson 3 is missing from that module: add
  `tools.jackson.core:jackson-databind`, `version: null`; never move the imports back);
  `tools.jackson.databind.ObjectMapper cannot be converted to com.fasterxml.jackson.databind.ObjectMapper`
  (a Jackson 3 mapper passed to a class that takes Jackson 2: an MVC converter / view, a WebFlux codec, a Redis
  serializer or a Kafka class is Rule A; for another class, and for the reverse message, the choice below decides);
  `exception JacksonException has already been caught` and `exception IOException is never thrown in body of
  corresponding try statement` (catch clauses after the move to the unchecked `JacksonException`, Rule A).

## Which rule applies
Decide by the class the failing call talks to, never by the message alone. Two things hold under every rule: code on
`tools.jackson` is never moved back to `com.fasterxml.jackson`, and a missing line is added as a dependency, never
avoided by moving code. A type written fully qualified as `com.fasterxml.jackson.…` in a file on `tools.jackson`
marks the boundary to a Jackson 2 only library (Rule B): it stays.
0. no file and no pom of the module uses `tools.jackson` (`<need_grep>` when unsure): Rule C, whatever the class;
1. a Spring Framework / Spring Boot / Spring portfolio class (`org.springframework.*`): Rule A; the modules these
   notes name each have a Jackson 3 counterpart on the target line (MVC converters, WebFlux codecs, Redis, Kafka:
   below; the other modules: `jackson-3-spring.md`; Boot's own types: `boot4-packages.md`). A Spring class no note
   names (Spring Cloud …): choice 3;
2. Jackson 3's own API (a call on a `tools.jackson` object: mapper methods, modules, serializers, exceptions):
   Rule A (`jackson-3-api.md`); an argument typed `com.fasterxml.jackson.…` stays so and crosses as text (Rule B);
3. another library: Rule A when a class of it that takes Jackson 3 types is on the classpath (a note, the error's
   candidate list or a `Jackson3…` name shows it: Hibernate's `Jackson3JsonFormatMapper(JsonMapper)`,
   elasticsearch-java's `Jackson3JsonpMapper(JsonMapper)`); otherwise Rule B (`jjwt-jackson`, swagger-core 2.2's
   `ModelResolver`, an in-house jar compiled against Jackson 2). Never swap the library's artifact for another to get
   a Jackson 3 class.

## Rule A — the module is on Jackson 3: finish the move in the failing file
Chosen by 1-3 above; the module's pom or its other files use `tools.jackson`: change the failing file's imports to
`tools.jackson.*` and
- `JsonProcessingException` / `JsonMappingException` → `tools.jackson.core.JacksonException` (unchecked, extends
  `RuntimeException`; `DatabindException` is its subclass): a `throws` clause that only existed for the checked
  Jackson 2 exception may go, keep public signatures otherwise; a `catch` with its own handling is kept, never dropped:
  - `exception JacksonException has already been caught` (or another Jackson exception): move that catch block,
    unchanged, above the broader `catch (RuntimeException …)` / `catch (Exception …)` of the same try; delete neither
    block; never a multi-catch with `RuntimeException` / `Exception` (does not compile);
  - `exception IOException is never thrown in body of corresponding try statement` where the try's only I/O source
    was a Jackson call: change that catch's type to `JacksonException`, keep its body; when the same try already
    catches `JacksonException`, remove the dead `IOException` block;
  - `unreported exception IOException` / `overridden method does not throw IOException` in a custom (de)serializer
    whose base type is `tools.jackson`: `jackson-3-api.md` (remove `throws IOException` from the helper too; never
    put `throws` back on the override);
- `MappingJackson2HttpMessageConverter` → `org.springframework.http.converter.json.JacksonJsonHttpMessageConverter`,
  `MappingJackson2JsonView` → `org.springframework.web.servlet.view.json.JacksonJsonView` (XML twins:
  `JacksonXmlHttpMessageConverter`, `JacksonXmlView`). The twins take `tools.jackson.databind.json.JsonMapper` (or its
  `Builder`), not `ObjectMapper`; they have no `setObjectMapper` — configure the mapper first and pass it to the
  constructor; `getMapper()` replaces `getObjectMapper()`;
- WebFlux codecs: `org.springframework.http.codec.json.Jackson2JsonDecoder` → `JacksonJsonDecoder`,
  `Jackson2JsonEncoder` → `JacksonJsonEncoder` (same package; Smile twins
  `org.springframework.http.codec.smile.JacksonSmileDecoder` / `JacksonSmileEncoder`, CBOR twins
  `org.springframework.http.codec.cbor.JacksonCborDecoder` / `JacksonCborEncoder`). The JSON codecs take
  `tools.jackson.databind.json.JsonMapper` or `JsonMapper.Builder` (optionally `MimeType...`), not `ObjectMapper`:
  inject the mapper as `JsonMapper` (Boot 4.1 auto-configures that bean) or build it with `JsonMapper.builder()…build()`.
  Register with `defaultCodecs().jacksonJsonDecoder(…)` / `jacksonJsonEncoder(…)` (the deprecated
  `jackson2JsonDecoder` / `jackson2JsonEncoder` still compile and delegate; renaming them is optional).
  `tools.jackson.databind.ObjectMapper cannot be converted to com.fasterxml.jackson.databind.ObjectMapper` at a codec
  constructor is this case: never move the file, the mapper or the module back to Jackson 2 for it, and never remove
  a Jackson 3 dependency from a pom to make it compile. `cannot find symbol … class JacksonJsonDecoder` (or
  `JacksonJsonEncoder`) with `location: package org.springframework.http.codec.json` means the module's spring-web is
  older than 7.0: an as-is pin (explicit `<version>` on a Spring artifact, a Spring version property, an imported older
  Spring / Boot BOM) overrides the target Boot BOM there. Remove the pin so the BOM manages Spring (versions left
  `null`); never go back to `Jackson2JsonDecoder` or Jackson 2 for it;
- Spring Data Redis (`org.springframework.data.redis.serializer`): `Jackson2JsonRedisSerializer` →
  `JacksonJsonRedisSerializer<T>` (`(Class<T>)` or `(ObjectMapper, Class<T>)`; no `setObjectMapper`: the mapper goes
  into the constructor), `GenericJackson2JsonRedisSerializer` → `GenericJacksonJsonRedisSerializer` (`(ObjectMapper)`;
  no no-arg constructor: `new GenericJackson2JsonRedisSerializer()` → `RedisSerializer.json()`, the same typing, never
  a bare `builder().build()`), `hash.Jackson2HashMapper` → `JacksonHashMapper`. They take
  `tools.jackson.databind.ObjectMapper` (a `JsonMapper` is one). At these classes
  `cannot access ObjectMapper` / `cannot access JavaType` (`class file for com.fasterxml.jackson.databind.… not
  found`), `tools.jackson.databind.ObjectMapper cannot be converted to com.fasterxml.jackson.databind.ObjectMapper`,
  `no suitable constructor found for GenericJackson2JsonRedisSerializer(…)` and `cannot infer type arguments for
  Jackson2JsonRedisSerializer<>` are all this case: never move the file or the mapper back to Jackson 2 and never add
  Jackson 2 databind for it (one exception: a mapper that a Rule B library hands out, see Rule B). Builder, default
  typing: `jackson-3-spring.md`, `jackson-3-api.md`;
- Spring Kafka (`org.springframework.kafka.support`): `serializer.JsonSerializer` / `JsonDeserializer` / `JsonSerde` →
  `JacksonJsonSerializer` / `JacksonJsonDeserializer` / `JacksonJsonSerde` (they take `JsonMapper`; `cannot access
  TypeReference … class file for com.fasterxml.jackson.core.type.TypeReference not found` at `new JsonSerializer<>()`
  is this case), `JacksonUtils.enhancedObjectMapper()` → `JacksonMapperUtils.enhancedJsonMapper()` (without Jackson
  2 on the classpath it gives the `cannot access ObjectMapper` message); the rest in `jackson-3-spring.md`;
- `Jackson2ObjectMapperBuilder` / `Jackson2ObjectMapperFactoryBean` → `JsonMapper.builder()…build()` (Spring has no
  Jackson 3 version of its builder);
- a mapper bean handed to the twins or the codecs must be declared as `JsonMapper` (`(JsonMapper) mapper` only when it
  is one).
This finishes the recipe's work; it is not a library swap. Never revert the module to Jackson 2.

## Rule B — a library with no Jackson 3 API needs Jackson 2 types
Only by choice 3 above: the call exchanges Jackson 2 types with a library that has no class taking Jackson 3 types
(never a Spring class the notes name, never a library whose Jackson 3 class is on the classpath). Add
`com.fasterxml.jackson.core:jackson-databind` (`version: null`) to that module (and the Jackson 2 module artifacts a
file kept on Jackson 2 imports); both lines coexist on the classpath and often that alone compiles.
- A file the recipe left on `com.fasterxml.jackson.*` that uses Jackson only with that library stays as it is.
- A file on `tools.jackson` keeps its imports: write the Jackson 2 types that library takes, returns or declares
  (an interface or base-class signature) fully qualified (`com.fasterxml.jackson.databind.ObjectMapper`,
  `…JsonNode`). A call on such a Jackson 2 object follows Jackson 2: its checked
  `com.fasterxml.jackson.core.JsonProcessingException` / `IOException` is caught or declared; Rule A does not apply
  to it.
- Never retype a variable of the file that a Jackson 3 call also uses and never cast between the lines: give the
  library its own Jackson 2 object (`new com.fasterxml.jackson.databind.ObjectMapper()` or the library's factory;
  `// MIGRATION-TODO`: it has none of the Jackson 3 mapper's settings) and pass trees across as text
  (`mapper.readValue(node.toString(), X.class)`), also from a parameter the library's signature fixes to Jackson 2.
- A mapper the library hands out (a company factory) has no Jackson 3 form: the Spring class it feeds (a Redis
  serializer, a converter) must be the Jackson 2 one. Keep that class, or put it back where an earlier fix renamed
  it, with the library's mapper declared fully qualified (`// MIGRATION-TODO`); never replace the library's mapper
  with a new `JsonMapper`.

## Rule C — nothing in the module is on Jackson 3
`package com.fasterxml.jackson.databind does not exist` and no file / pom of the module uses `tools.jackson`: the
smallest fix is `com.fasterxml.jackson.core:jackson-databind` with `version: null` (plus the Jackson 2 module
artifacts the files import, e.g. `com.fasterxml.jackson.datatype:jackson-datatype-jsr310`); the imports stay.

## Rule D — converted projects (`spring_legacy`, `servlet_tomcat`, `pure_java`)
Never add `org.springframework.boot:spring-boot-jackson2`: it brings Boot's Jackson 2 auto-configuration, which the
compile does not need (rules B / C add only `jackson-databind`). In a Boot project it is only for
`org.springframework.boot.jackson2.*` types or an injected Jackson 2 `ObjectMapper` bean.

## Not a build fix
- XML beans of class `MappingJackson2JsonView` (eGovFrame "jsonView"), `MappingJackson2HttpMessageConverter`,
  `Jackson2ObjectMapperFactoryBean` or `com.fasterxml…ObjectMapper` in `legacy-config/` compile, but fail at startup
  without Jackson 2 databind: a startup matter for the owners (Handover) — never edit the XML for the build.
- Jackson 3 defaults differ (e.g. dates written as ISO strings): add `// MIGRATION-TODO` where a fix changes serialization.
