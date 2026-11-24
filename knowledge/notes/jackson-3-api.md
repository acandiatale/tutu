---
title: "Jackson 3 API: what the recipe leaves in code that uses Jackson itself"
keywords:
  - "of type (ObjectMapper|JsonMapper)\\b"
  - "method (configure|enable|disable)\\((SerializationFeature|DeserializationFeature|MapperFeature)\\b"
  - "method (registerModules?|findAndRegisterModules|setSerializationInclusion|setDefaultPropertyInclusion|setPropertyNamingStrategy|getSerializerProvider|useDefaultPrettyPrinter)\\("
  - "location: class (SerializationFeature|MapperFeature)\\b"
  - "method visibility\\(PropertyAccessor"
  - "JavaTimeModule|Jdk8Module|ParameterNamesModule|tools\\.jackson\\.module\\.paramnames|jackson-datatype-(jsr310|jdk8)|jackson-module-parameter-names"
  - "defaultSerialize(Field|Value|Null)\\b|setNullValueSerializer"
  - "writeObjectProperty|writeObjectField"
  - "\\bObjectCodec\\b"
  - "\\bDefaultTyping\\b|LaissezFaireSubTypeValidator|(activate|enable)DefaultTyping"
  - "cannot override \\w+\\([^)]*(JsonGenerator|JsonParser|SerializationContext|DeserializationContext)"
  - "JacksonException cannot be converted to IOException"
  - "constructor (DatabindException|JsonMappingException)"
  - "BeanSerializerModifier|addDeserializationProblemHandler|setMixInAnnotations"
  - "PropertyNamingStrateg(y|ies) cannot be converted to PropertyNamingStrateg(y|ies)"
  - "tools\\.jackson\\.core\\.(JsonGenerator|JsonParser)\\b"
  - "tools\\.jackson\\.databind\\.(SerializationContext|DeserializationContext|ValueSerializer|ValueDeserializer|KeyDeserializer|JacksonModule|ser\\.|deser\\.|module\\.)"
cases: []
phases: [spring]
---
# Jackson 3 API after the recipe (Rule A: finish on `tools.jackson`)
For code on `tools.jackson`. An object typed `com.fasterxml.jackson…` (a Jackson 2 only library, jackson-2-3.md)
keeps Jackson 2's API and its checked exceptions.

## The mapper is immutable
- `cannot find symbol … method configure(DeserializationFeature,boolean)` (`enable`, `setDateFormat`, `registerModule` …)
  with `location: variable … of type ObjectMapper`: build it, `JsonMapper.builder()…build()`. A mapper the class
  owns or wants its own copy of: `this.mapper = injected.rebuild()…build();`, then use that. An injected Spring bean
  that was reconfigured in place (it changed the mapper everyone shares): a `JsonMapperBuilderCustomizer` bean
  (boot4-packages.md); a `rebuild()` copy there compiles but leaves the shared mapper unconfigured. Builder names:
  `configure` / `enable` / `disable` stay, `defaultDateFormat`, `defaultTimeZone`, `propertyNamingStrategy`,
  `addModule`, `findAndAddModules`; inclusion / visibility: last section.
- `extends ObjectMapper` configuring itself: `extends JsonMapper`, `super(builder)` with a `JsonMapper.Builder`.
- Default typing: `ObjectMapper.DefaultTyping` (`cannot find symbol … variable DefaultTyping … location: class
  ObjectMapper`) → `tools.jackson.databind.DefaultTyping`; `activateDefaultTyping(…)` on a mapper → the builder's
  `.activateDefaultTyping(…)` with the SAME arguments (a `JsonTypeInfo.As.PROPERTY` argument stays: without it the
  format becomes a wrapper array and stored JSON no longer reads); `enableDefaultTyping(t)` →
  `.activateDefaultTyping(ptv, t)`. `LaissezFaireSubTypeValidator is not public in
  tools.jackson.databind.jsontype.impl` → `BasicPolymorphicTypeValidator.builder().allowIfBaseType(Object.class)
  .build()` (`tools.jackson.databind.jsontype`): it allows every type and reads what Jackson 2 wrote.
- `getFactory()`: `mapper.createGenerator(out, JsonEncoding.UTF8)` / `mapper.createParser(json)`;
  `useDefaultPrettyPrinter()`: `mapper.writerWithDefaultPrettyPrinter().createGenerator(writer)`;
  `getSerializerProvider().setNullValueSerializer(s)`: `SimpleModule.setDefaultNullValueSerializer(s)`.

## Features
- `SerializationFeature.WRITE_BIGDECIMAL_AS_PLAIN` → `tools.jackson.core.StreamWriteFeature.WRITE_BIGDECIMAL_AS_PLAIN`.
- `SerializationFeature.WRITE_DATES_AS_TIMESTAMPS` → `tools.jackson.databind.cfg.DateTimeFeature.WRITE_DATES_AS_TIMESTAMPS`.
- `WRITE_NULL_MAP_VALUES` false → `changeDefaultPropertyInclusion(incl -> incl.withContentInclusion(Include.NON_NULL))`.
- `MapperFeature.AUTO_DETECT_GETTERS` (`_IS_GETTERS`, `_SETTERS`, `_FIELDS`) false →
  `changeDefaultVisibility(vc -> vc.withGetterVisibility(Visibility.NONE))` (`withIsGetterVisibility` …);
  `_CREATORS` false → `vc.withCreatorVisibility(Visibility.NONE).withScalarConstructorVisibility(Visibility.NONE)`.
- Another removed feature constant with no line here: do not invent a name; drop the call, `// MIGRATION-TODO`.

## Modules built into databind 3
- `class JavaTimeModule … location: package tools.jackson.databind.ext.javatime` (or `Jdk8Module`), `package
  tools.jackson.module.paramnames does not exist`: built in. Delete the import, `new …Module()` and its registration.
  Never add `jackson-datatype-jsr310`, `jackson-datatype-jdk8`, `jackson-module-parameter-names` (Jackson 2) to
  code on `tools.jackson`.
- A file still on `com.fasterxml.jackson` (Rule C or a Jackson 2 only library, jackson-2-3.md) keeps its module:
  there `cannot find symbol … class JavaTimeModule` / `package com.fasterxml.jackson.datatype.jsr310 does not exist`
  means the Jackson 2 artifact is missing: add `com.fasterxml.jackson.datatype:jackson-datatype-jsr310` (`-jdk8`,
  `com.fasterxml.jackson.module:jackson-module-parameter-names`), `version: null`; deleting it there compiles and
  fails at run time.
- Custom formats: a `SimpleModule` (typed `JacksonModule`) with
  `tools.jackson.databind.ext.javatime.ser.LocalDateTimeSerializer(fmt)` / `….deser.LocalDateTimeDeserializer(fmt)`.
- `variable module` not found at `return module;`: the recipe deleted `JavaTimeModule module = …` WITH its
  `addSerializer` lines; rebuild that `SimpleModule` with the as-is formats (unknown: `// MIGRATION-TODO`).

## Custom serializers / deserializers
- `cannot override serialize(T,JsonGenerator,SerializationContext)` + `overridden method does not throw IOException`
  (also `KeyDeserializer.deserializeKey`, `DeserializationProblemHandler.handleUnknownProperty`): remove `throws IOException`.
- `unreported exception IOException; must be caught or declared to be thrown` at a helper call inside `serialize` /
  `deserialize`: the helper must lose `throws IOException` too. Putting `throws` back on the override is the wrong
  direction (the message above). Real I/O: catch it, `throw tools.jackson.core.exc.JacksonIOException.construct(e)`.
- `SerializationContext`: `defaultSerializeField` → `defaultSerializeProperty`, `defaultSerializeValue(v, gen)` →
  `writeValue(gen, v)`, `defaultSerializeNull` → `defaultSerializeNullValue`.
- `isEmpty(T)`: `isEmpty(SerializationContext ctxt, T value)` (without `@Override` it is never called).
- `class ObjectCodec` not found: `ctxt.readTree(p)`, `ctxt.readTreeAsValue(node, X.class)`; never cast
  `p.objectReadContext()` to `ObjectMapper` (ClassCastException at run time).
- `constructor DatabindException … has protected access`: `DatabindException.from(p, msg)` (or `from(ctxt, msg)`).
- `JacksonException cannot be converted to IOException` (`new UncheckedIOException(e)`, an `IOException` parameter):
  keep the type callers catch, `new UncheckedIOException(new IOException(e))` / pass `new IOException(e)`.
- `e.getProcessor()` → `e.processor()`; `BeanPropertyWriter.getPropertyType()` → `getType().getRawClass()`.

## Recipe rewrites that do not exist in 3.1
- `writeObjectProperty(name, v)` → `writePOJOProperty(name, v)`. On a generator made from a bare `JsonFactory`
  that throws at run time: `writeStringProperty` / `writeNumberProperty`, or create it with `mapper.createGenerator(…)`.
- Builder `.visibility(PropertyAccessor.FIELD, Visibility.ANY)` →
  `.changeDefaultVisibility(vc -> vc.withVisibility(PropertyAccessor.FIELD, Visibility.ANY))`.
- `setDefaultPropertyInclusion(X)` (was `setSerializationInclusion(X)`) → builder `.changeDefaultPropertyInclusion(incl
  -> incl.withValueInclusion(X).withContentInclusion(X))`: both, value alone starts writing null map entries.
- `BeanSerializerModifier` → `tools.jackson.databind.ser.ValueSerializerModifier`, whose `changeProperties` takes
  `BeanDescription.Supplier`; keep `@Override`. `SetupContext`: `addBeanSerializerModifier` → `addSerializerModifier`,
  `addDeserializationProblemHandler` → `addHandler`, `setMixInAnnotations` → `setMixIn`.
- `PropertyNamingStrategies cannot be converted to PropertyNamingStrategy` (or reverse): declare it
  `tools.jackson.databind.PropertyNamingStrategy`.
