---
title: Jackson 3 counterparts in the Spring portfolio (Redis, Kafka, AMQP, messaging, Batch, ...)
keywords:
  - "org\\.springframework\\.kafka\\.support\\.serializer\\.(Jackson)?Json"
  - "JacksonJson(Serializer|Deserializer|Serde)\\b|\\bJsonSerde\\b"
  - "(kafka\\.support|amqp\\.support\\.converter)\\.JacksonUtils|JacksonMapperUtils|enhancedObjectMapper"
  - "(Default)?Jackson2?JavaTypeMapper"
  - "JsonKafkaHeaderMapper|DefaultKafkaHeaderMapper"
  - "cannot infer type arguments for Jackson2?Json\\w*<>"
  - "JacksonJsonMessageConverter|Jackson2(Json|Xml)MessageConverter|MappingJackson2MessageConverter"
  - "(Generic)?Jackson2?JsonRedisSerializer|Jackson2?HashMapper"
  - "JacksonJsonObject(Reader|Marshaller)|Jackson2?ExecutionContextStringSerializer"
  - "Jackson2?Json(ObjectMapper|MessageParser)\\b|Jackson2SockJsMessageCodec"
  - "(MappingJackson2|Jackson)(Xml|Smile|Cbor|Yaml)HttpMessageConverter"
  - "Jackson2HalModule|GraphQlJackson2Module|configureJacksonObjectMapper|RepositoryRestConfigurer"
cases: []
phases: [spring]
---
# Jackson 3 counterparts in the Spring portfolio
These Spring classes have a Jackson 3 counterpart: use it (Rule A, jackson-2-3.md). Unless a line says otherwise it
takes `tools.jackson.databind.json.JsonMapper` (not `ObjectMapper`) in the constructor and has no `setObjectMapper`.
Never move the file or the mapper back to Jackson 2 for them (one exception: a mapper that a Jackson 2 only library
hands out, jackson-2-3.md Rule B). `cannot find symbol … class <counterpart>` with
`location: package org.springframework…`: that Spring module is older than the target line (an as-is `<version>`, a
version property, an imported older BOM); remove the pin so the BOM manages it, never rename back.

## Spring Data Redis (`org.springframework.data.redis`)
- The recipe renames none of them. `serializer.Jackson2JsonRedisSerializer` → `JacksonJsonRedisSerializer<T>`:
  `(Class<T>)` or `(ObjectMapper, Class<T>)`; it has no `setObjectMapper` (`cannot find symbol … method
  setObjectMapper`): pass the mapper to the constructor. `serializer.GenericJackson2JsonRedisSerializer` →
  `GenericJacksonJsonRedisSerializer`: `(ObjectMapper)` or `GenericJacksonJsonRedisSerializer.builder()…build()`.
  `hash.Jackson2HashMapper` → `JacksonHashMapper` (`(ObjectMapper, boolean)`).
  These take `tools.jackson.databind.ObjectMapper` (a `JsonMapper` is one).
- `new GenericJackson2JsonRedisSerializer()` (no mapper) → `RedisSerializer.json()`: the same `@class` typing and
  cache null values; a bare `builder().build()` has no typing and reads stored entries as maps (`no suitable
  constructor found for GenericJacksonJsonRedisSerializer(no arguments)` = this). The old no-arg form compiles
  without Jackson 2 and fails at startup: replace it in a file you are fixing even when it is not the failing line.
- The builder: `enableDefaultTyping(PolymorphicTypeValidator)`, `enableUnsafeDefaultTyping()`,
  `enableSpringCacheNullValueSupport()`, `typePropertyName(String)`, `customize(b -> …)` on the `JsonMapper.Builder`.
- A mapper configured with default typing (`activateDefaultTyping(…)`): keep that configuration in its Jackson 3 form
  (jackson-3-api.md) and pass that mapper; the typed JSON is what the entries already in Redis hold.
- At the Jackson 2 classes `cannot access ObjectMapper` / `cannot access JavaType` (`class file for
  com.fasterxml.jackson.databind.… not found`), `tools.jackson.databind.ObjectMapper cannot be converted to
  com.fasterxml.jackson.databind.ObjectMapper` (at `setObjectMapper`), `no suitable constructor found for
  GenericJackson2JsonRedisSerializer(tools.jackson.databind.ObjectMapper)` and `cannot infer type arguments for
  Jackson2JsonRedisSerializer<>` all mean: use the counterpart, never add Jackson 2 for them (the exception of the
  opening apart).

## Spring Kafka (`org.springframework.kafka.support`)
- `UpgradeSpringKafka_4_0` renamed `serializer.JsonSerializer` / `JsonDeserializer` / `JsonSerde` →
  `JacksonJsonSerializer` / `JacksonJsonDeserializer` / `JacksonJsonSerde`, `DefaultKafkaHeaderMapper` →
  `JsonKafkaHeaderMapper`, `converter.*JsonMessageConverter` → `*JacksonJsonMessageConverter`.
- It leaves the mapper type: they take `JsonMapper` (`new JacksonJsonDeserializer<>(Foo.class, jsonMapper, false)`).
- It leaves `JacksonUtils.enhancedObjectMapper()` (Jackson 2) → `JacksonMapperUtils.enhancedJsonMapper()`.
- It leaves the type mapper interface: `mapping.Jackson2JavaTypeMapper` (and its `TypePrecedence`) →
  `mapping.JacksonJavaTypeMapper`, which `setTypeMapper` takes; it renames only `DefaultJackson2JavaTypeMapper` →
  `DefaultJacksonJavaTypeMapper` and `converter.ProjectingMessageConverter` → `JacksonProjectingMessageConverter`.
- `cannot infer type arguments for JacksonJsonSerializer<>` (or `JacksonJsonDeserializer<>`): no constructor fits, usually an
  `ObjectMapper` argument where `JsonMapper` is needed. Fix the argument, keep the class. When that variable also
  feeds a library that takes Jackson 2 types, leave it and give that library its own instance (jackson-2-3.md).
- `cannot access ObjectMapper … class file for com.fasterxml.jackson.databind.ObjectMapper not found` at
  `enhancedObjectMapper()`: the helper above. `no suitable constructor found for JsonKafkaHeaderMapper(ObjectMapper)`:
  pass a `JsonMapper`.
- `TRUSTED_PACKAGES` and the other `spring.json.*` constants keep their names on `JacksonJsonDeserializer`.
- The Jackson 2 classes still exist in spring-kafka 4.1 (deprecated) and take `com.fasterxml.jackson.databind.ObjectMapper`;
  `new JsonSerializer<>()` / `new JsonDeserializer<>(Foo.class)` fail without Jackson 2 (`cannot access
  TypeReference`): use the `JacksonJson…` class, never add Jackson 2 for them.

## Other modules
- AMQP (`amqp.support.converter`): `Jackson2JsonMessageConverter` → `JacksonJsonMessageConverter` (`()` or
  `(JsonMapper)`), `Jackson2XmlMessageConverter` → `JacksonXmlMessageConverter` (`XmlMapper`), `Jackson2JavaTypeMapper`
  → `JacksonJavaTypeMapper`; its `JacksonUtils.enhancedObjectMapper()` has no counterpart: `JsonMapper.builder()…build()`.
- spring-messaging / spring-jms: `MappingJackson2MessageConverter` → `JacksonJsonMessageConverter` (same package).
- spring-websocket: `Jackson2SockJsMessageCodec` → `JacksonJsonSockJsMessageCodec`.
- `MappingJackson2{Xml,Smile,Cbor,Yaml}HttpMessageConverter` → `Jackson{Xml,Smile,Cbor,Yaml}HttpMessageConverter`
  (same packages), taking `XmlMapper` / `SmileMapper` / `CBORMapper` / `YAMLMapper` (`tools.jackson.dataformat.*`).
- Spring Batch: `JacksonJsonObjectReader` / `JacksonJsonObjectMarshaller` keep their names but take `JsonMapper`
  (`cannot infer type arguments for JacksonJsonObjectMarshaller<>` = an `ObjectMapper` argument); Batch has no
  Jackson 2 variant, so going back to Jackson 2 cannot work. `Jackson2ExecutionContextStringSerializer` →
  `JacksonExecutionContextStringSerializer`.
- Spring Integration: `Jackson2JsonObjectMapper` → `JacksonJsonObjectMapper`.
- HATEOAS: `Jackson2HalModule` is gone → `HalJacksonModule`. GraphQL: `GraphQlJackson2Module` → `GraphQlJacksonModule`.
- Data REST: `configureJacksonObjectMapper` takes `tools.jackson.databind.cfg.MapperBuilder<…>`; `method does not
  override` on the old `(ObjectMapper)` override.

Elsewhere: Boot's own types in boot4-packages.md, Security's modules in spring-security-7.md, MVC converters and
WebFlux codecs in jackson-2-3.md.
