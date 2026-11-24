"""v2.1z-bw (docs/INTERFACES.md "v2.1z-bw"): the note `jackson-3-spring.md` gives the Jackson 3 counterparts of the
Spring portfolio (Spring Kafka in full, one line per other module), so the fix LLM does not go back and forth between
Spring's Jackson 2 classes and their Jackson 3 counterparts. The error texts below are the javac results the survey
stored (`out/scratch/jackson-libs/spring-twins/snippets/results/`); the note must reach the prompt for them, first or
directly after `jackson-2-3.md`, and stay out of the prompts of the earlier Jackson rounds and of unrelated Kafka errors."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from sbmig.core.interfaces import FixContext
from sbmig.core.models import BuildError, ErrorCluster, ErrorKind, Phase
from sbmig.fix import ContextPacker, load_notes_index
from tests.fix import test_v21zbu_jackson_codecs as codecs
from tests.fix import test_v21zbv_jackson_catch as catch

NOTES = Path(__file__).resolve().parents[2] / "knowledge" / "notes"
NOTE = "jackson-3-spring.md"
BASE_NOTE = "jackson-2-3.md"

KAFKA_FILE = "app/src/main/java/com/example/app/KafkaConfig.java"
# the source as UpgradeSpringKafka_4_0 + UpgradeJackson_2_3 leave it (survey snippet s05_kafka_after_recipe)
KAFKA_SOURCE = """package com.example.app;

import tools.jackson.databind.ObjectMapper;
import org.apache.kafka.common.serialization.Deserializer;
import org.apache.kafka.common.serialization.Serializer;
import org.springframework.kafka.support.JacksonUtils;
import org.springframework.kafka.support.JsonKafkaHeaderMapper;
import org.springframework.kafka.support.serializer.JacksonJsonDeserializer;
import org.springframework.kafka.support.serializer.JacksonJsonSerializer;

public class KafkaConfig {
    public static class Foo { public String name; }

    Serializer<Foo> valueSerializer(ObjectMapper objectMapper) {
        return new JacksonJsonSerializer<>(objectMapper);
    }

    Deserializer<Foo> valueDeserializer() {
        JacksonJsonDeserializer<Foo> d = new JacksonJsonDeserializer<>(Foo.class, JacksonUtils.enhancedObjectMapper(), false);
        d.addTrustedPackages("*");
        return d;
    }

    JsonKafkaHeaderMapper headerMapper(ObjectMapper objectMapper) {
        return new JsonKafkaHeaderMapper(objectMapper);
    }
}
"""
KAFKA_DIAMOND = "cannot infer type arguments for JacksonJsonSerializer<>"
KAFKA_DESER_DIAMOND = "cannot infer type arguments for JacksonJsonDeserializer<>"
KAFKA_HEADER = "no suitable constructor found for JsonKafkaHeaderMapper(ObjectMapper)"
KAFKA_HEADER_DETAIL = """    constructor JsonKafkaHeaderMapper.JsonKafkaHeaderMapper(JsonMapper) is not applicable
      (argument mismatch; ObjectMapper cannot be converted to JsonMapper)
    constructor JsonKafkaHeaderMapper.JsonKafkaHeaderMapper(String...) is not applicable
      (varargs mismatch; ObjectMapper cannot be converted to String)"""

REDIS_FILE = "app/src/main/java/com/example/app/RedisConfig.java"
REDIS_SOURCE = """package com.example.app;

import tools.jackson.databind.ObjectMapper;
import org.springframework.data.redis.serializer.GenericJackson2JsonRedisSerializer;
import org.springframework.data.redis.serializer.RedisSerializer;

public class RedisConfig {
    RedisSerializer<Object> generic(ObjectMapper objectMapper) {
        return new GenericJackson2JsonRedisSerializer(objectMapper);
    }
}
"""
REDIS_CTOR = "no suitable constructor found for GenericJackson2JsonRedisSerializer(tools.jackson.databind.ObjectMapper)"
REDIS_DETAIL = """    constructor GenericJackson2JsonRedisSerializer.GenericJackson2JsonRedisSerializer(String) is not applicable
      (argument mismatch; tools.jackson.databind.ObjectMapper cannot be converted to String)
    constructor GenericJackson2JsonRedisSerializer.GenericJackson2JsonRedisSerializer(com.fasterxml.jackson.databind.ObjectMapper) is not applicable
      (argument mismatch; tools.jackson.databind.ObjectMapper cannot be converted to com.fasterxml.jackson.databind.ObjectMapper)"""

BATCH_FILE = "batch/src/main/java/com/example/batch/BatchConfig.java"
BATCH_SOURCE = """package com.example.batch;

import tools.jackson.databind.ObjectMapper;
import org.springframework.batch.infrastructure.item.json.JacksonJsonObjectMarshaller;

public class BatchConfig {
    public static class Foo { public String name; }

    JacksonJsonObjectMarshaller<Foo> marshaller(ObjectMapper objectMapper) {
        return new JacksonJsonObjectMarshaller<>(objectMapper);
    }
}
"""
BATCH_DIAMOND = "cannot infer type arguments for JacksonJsonObjectMarshaller<>"

# a Kafka error that has nothing to do with JSON
TEMPLATE_FILE = "app/src/main/java/com/example/app/OrderPublisher.java"
TEMPLATE_SOURCE = """package com.example.app;

import org.springframework.kafka.core.KafkaTemplate;

public class OrderPublisher {
    private final KafkaTemplate<String, String> template;

    public OrderPublisher(KafkaTemplate<String, String> template) {
        this.template = template;
    }
}
"""

# (file, source, message, line, symbol, location, detail lines after the message)
POSITIVE = {
    "kafka-diamond": (KAFKA_FILE, KAFKA_SOURCE, KAFKA_DIAMOND, 15, None, "class com.example.app.KafkaConfig", ""),
    "kafka-deserializer-diamond": (KAFKA_FILE, KAFKA_SOURCE, KAFKA_DESER_DIAMOND, 19, None,
                                   "class com.example.app.KafkaConfig", ""),
    "kafka-header-mapper": (KAFKA_FILE, KAFKA_SOURCE, KAFKA_HEADER, 25, None, "class com.example.app.KafkaConfig",
                            KAFKA_HEADER_DETAIL),
    "redis-ctor": (REDIS_FILE, REDIS_SOURCE, REDIS_CTOR, 9, None, "class com.example.app.RedisConfig", REDIS_DETAIL),
    "batch-marshaller": (BATCH_FILE, BATCH_SOURCE, BATCH_DIAMOND, 10, None, "class com.example.batch.BatchConfig", ""),
}


def _entries():
    return {e.file: e for e in load_notes_index(NOTES)}


def _note():
    return _entries()[NOTE]


def _ctx() -> FixContext:
    return FixContext(phase=Phase.VERIFY, iteration=1, script_mode="new", case_id="k1")


def _cluster(file: str, message: str, line: int, symbol: str | None, location: str | None,
             detail: str = "") -> ErrorCluster:
    raw = f"[ERROR] /p/{file}:[{line},16] {message}" + (f"\n{detail}" if detail else "")
    err = BuildError(kind=ErrorKind.COMPILE, message=message, file=file, line=line, column=16, symbol=symbol,
                     location=location, signature="s1", raw=raw)
    return ErrorCluster(id="case:k1", kind=ErrorKind.COMPILE, errors=[err], case_id="k1", signatures=["s1"])


def _pack(cfg, ws, put, file, source, cluster, case_type="spring_boot"):
    put(file, source)
    return ContextPacker(cfg).pack_script(ws, cluster, _ctx(), f"case={case_type}", case_type=case_type)


@pytest.mark.parametrize("name", sorted(POSITIVE))
def test_the_spring_twin_errors_select_the_note_first_or_after_the_base_note(cfg, ws, put, name):
    file, source, message, line, symbol, location, detail = POSITIVE[name]
    entries = _entries()
    title, base = entries[NOTE].title, entries[BASE_NOTE].title
    for case_type in ("spring_boot", "msa", "spring_legacy"):
        p = _pack(cfg, ws, put, file, source, _cluster(file, message, line, symbol, location, detail), case_type)
        got = p.notes_included
        assert title in got, (name, case_type, got)
        i = got.index(title)
        assert i == 0 or (i == 1 and got[0] == base), (name, case_type, got)
        assert "JsonMapper" in dict(p.sections)[f"Migration note: {title}"]


def test_the_note_hits_these_errors_in_the_error_text_itself():
    """The selector takes only notes that hit the error text when any note does: the note must hit it, not only the
    imports."""
    note = _note()
    for name, (_f, _s, message, _l, _sym, _loc, detail) in POSITIVE.items():
        err, _ctx_hits = note.score_parts(f"{message}\n{detail}")
        assert err > 0, name


def test_the_note_stays_out_of_the_webflux_codec_case(cfg, ws, put):
    p = _pack(cfg, ws, put, codecs.CODEC_FILE, codecs.CODEC_SOURCE,
              _cluster(codecs.CODEC_FILE, codecs.CONVERT_ERROR, 20, None, "class com.example.web.CodecConfig"))
    assert _note().title not in p.notes_included, p.notes_included
    assert p.notes_included[0] == _entries()[BASE_NOTE].title


def test_the_note_stays_out_of_the_catch_case(cfg, ws, put):
    p = _pack(cfg, ws, put, catch.SRC_FILE, catch.JACKSON_SOURCE,
              _cluster(catch.SRC_FILE, catch.ALREADY_CAUGHT, catch.CAUGHT_LINE, None,
                       "class com.example.app.PayloadReader"))
    assert _note().title not in p.notes_included, p.notes_included


def test_the_note_stays_out_of_an_unrelated_kafka_error(cfg, ws, put):
    """A Kafka error that is not about JSON: neither the error text nor the imports select the note."""
    cl = _cluster(TEMPLATE_FILE, "cannot find symbol", 3, "class KafkaTemplate", "package org.springframework.kafka.core")
    p = _pack(cfg, ws, put, TEMPLATE_FILE, TEMPLATE_SOURCE, cl)
    assert _note().title not in p.notes_included, p.notes_included
    note = _note()
    for text in (TEMPLATE_SOURCE, "import org.springframework.kafka.annotation.KafkaListener;",
                 "import com.fasterxml.jackson.databind.JsonSerializer;", "import tools.jackson.databind.ObjectMapper;",
                 codecs.CONVERT_ERROR, catch.ALREADY_CAUGHT, catch.NEVER_THROWN):
        assert note.score(text) == 0, (text, [rx.pattern for rx in note.compiled() if rx.search(text)])


def test_the_note_stays_out_of_another_librarys_diamond_message():
    """`cannot infer type arguments for JacksonSerializer<>` is jjwt-jackson (a Jackson 2 only library, Rule B of the
    base note): the diamond keyword names Spring's `…Json…` classes only."""
    note = _note()
    for text in ("cannot infer type arguments for JacksonSerializer<>",
                 "cannot infer type arguments for JacksonDeserializer<>"):
        assert note.score(text) == 0, (text, [rx.pattern for rx in note.compiled() if rx.search(text)])
    for text in (KAFKA_DIAMOND, KAFKA_DESER_DIAMOND, BATCH_DIAMOND,
                 "cannot infer type arguments for Jackson2JsonRedisSerializer<>"):
        assert note.score(text) >= 2, text


# the five messages javac gives for a Redis configuration after the recipe (spring-data-redis 4.1.0; the first, third
# and fourth name no Redis class, so only the base note reaches their prompt: its Rule A carries the Redis answer)
REDIS_FULL_SOURCE = """package com.example.app;

import tools.jackson.databind.ObjectMapper;
import org.springframework.data.redis.serializer.GenericJackson2JsonRedisSerializer;
import org.springframework.data.redis.serializer.Jackson2JsonRedisSerializer;
import org.springframework.data.redis.serializer.RedisSerializer;

public class RedisConfig {
    RedisSerializer<Object> typed(ObjectMapper objectMapper) {
        return new Jackson2JsonRedisSerializer<>(objectMapper, Object.class);
    }

    RedisSerializer<Object> generic(ObjectMapper objectMapper) {
        return new GenericJackson2JsonRedisSerializer(objectMapper);
    }

    RedisSerializer<Object> legacySetter(ObjectMapper objectMapper) {
        Jackson2JsonRedisSerializer<Object> s = new Jackson2JsonRedisSerializer<>(Object.class);
        s.setObjectMapper(objectMapper);
        return s;
    }
}
"""
REDIS_MESSAGES = {
    "cannot-access-mapper": ("cannot access ObjectMapper", 10,
                             "  class file for com.fasterxml.jackson.databind.ObjectMapper not found"),
    "ctor-mismatch": (REDIS_CTOR, 14, REDIS_DETAIL),
    "cannot-access-javatype": ("cannot access JavaType", 18,
                               "  class file for com.fasterxml.jackson.databind.JavaType not found"),
    "setter-mismatch": ("incompatible types: tools.jackson.databind.ObjectMapper cannot be converted to "
                        "com.fasterxml.jackson.databind.ObjectMapper", 19, ""),
    "diamond": ("cannot infer type arguments for Jackson2JsonRedisSerializer<>", 10, ""),
}


@pytest.mark.parametrize("name", sorted(REDIS_MESSAGES))
def test_every_redis_message_reaches_a_note_that_names_the_counterpart(cfg, ws, put, name):
    """Redis is used in the company projects and oscillated there: whichever of the five messages a case carries,
    its prompt has a note that names `JacksonJsonRedisSerializer` / `GenericJacksonJsonRedisSerializer` and forbids
    going back to Jackson 2."""
    message, line, detail = REDIS_MESSAGES[name]
    p = _pack(cfg, ws, put, REDIS_FILE, REDIS_FULL_SOURCE, _cluster(REDIS_FILE, message, line, None, None, detail))
    notes = {t.removeprefix("Migration note: "): re.sub(r"\s+", " ", b) for t, b in p.sections
             if t.startswith("Migration note: ")}
    assert notes, name
    with_answer = [t for t, b in notes.items()
                   if "`JacksonJsonRedisSerializer<T>`" in b and "`GenericJacksonJsonRedisSerializer`" in b]
    assert with_answer, (name, sorted(notes))
    body = notes[with_answer[0]]
    assert "never add Jackson 2" in body, name
    assert "no `setObjectMapper`" in body, name


def test_the_redis_setter_leftover_selects_the_note(cfg, ws, put):
    """After the class is renamed the old setter call is left: the message names the counterpart."""
    cl = _cluster(REDIS_FILE, "cannot find symbol", 19, "method setObjectMapper(ObjectMapper)",
                  "variable s of type JacksonJsonRedisSerializer<Object>")
    p = _pack(cfg, ws, put, REDIS_FILE, REDIS_FULL_SOURCE.replace("Jackson2JsonRedisSerializer", "JacksonJsonRedisSerializer"), cl)
    assert _note().title in p.notes_included, p.notes_included


def test_the_helper_keyword_names_the_spring_packages_only():
    """A project's own `JacksonUtils` (a common name) must not bring the Kafka advice into its prompt; the type mapper
    messages of spring-kafka 4.1 do select the note."""
    note = _note()
    for text in ("location: class com.example.util.JacksonUtils", "import com.example.util.JacksonUtils;"):
        assert note.score(text) == 0, (text, [rx.pattern for rx in note.compiled() if rx.search(text)])
    for text in ("import org.springframework.kafka.support.JacksonUtils;",
                 "import org.springframework.amqp.support.converter.JacksonUtils;",
                 "incompatible types: DefaultJacksonJavaTypeMapper cannot be converted to Jackson2JavaTypeMapper",
                 "incompatible types: Jackson2JavaTypeMapper cannot be converted to JacksonJavaTypeMapper"):
        assert note.score(text) >= 1, text


def test_the_note_keeps_its_load_bearing_statements():
    body = re.sub(r"\s+", " ", _note().body())
    # the opening: the rule, the mapper type, no setter, never back to Jackson 2
    assert "Rule A, jackson-2-3.md" in body
    assert "takes `tools.jackson.databind.json.JsonMapper` (not `ObjectMapper`)" in body
    assert "has no `setObjectMapper`" in body
    assert "Never move the file or the mapper back to Jackson 2 for them (one exception:" in body
    # Kafka: what the recipe leaves, the helper, the diamond message
    assert "`JacksonUtils.enhancedObjectMapper()` (Jackson 2) → `JacksonMapperUtils.enhancedJsonMapper()`" in body
    assert "`cannot infer type arguments for JacksonJsonSerializer<>`" in body
    assert "Fix the argument, keep the class." in body
    assert "`DefaultKafkaHeaderMapper` → `JsonKafkaHeaderMapper`" in body
    # the interface the recipe does not rename, and what the Jackson 2 classes do without Jackson 2 (javac, 4.1.0)
    assert "`mapping.Jackson2JavaTypeMapper` (and its `TypePrecedence`) → `mapping.JacksonJavaTypeMapper`" in body
    assert ("`new JsonSerializer<>()` / `new JsonDeserializer<>(Foo.class)` fail without Jackson 2 (`cannot access "
            "TypeReference`): use the `JacksonJson…` class, never add Jackson 2 for them") in body
    assert "a use without a mapper compiles even without Jackson 2 databind" not in body
    # a counterpart that is missing from its own package means an older Spring module, not a wrong name
    assert "remove the pin so the BOM manages it, never rename back" in body
    # Redis: the counterparts take the plain Jackson 3 ObjectMapper, have no setter and no no-arg constructor
    assert "The recipe renames none of them." in body
    assert "These take `tools.jackson.databind.ObjectMapper` (a `JsonMapper` is one)." in body
    assert "it has no `setObjectMapper`" in body and "pass the mapper to the constructor" in body
    assert "`no suitable constructor found for GenericJacksonJsonRedisSerializer(no arguments)`" in body
    # the no-arg form: json() keeps the typing, a bare builder does not (run on spring-data-redis 4.1.0)
    assert "`new GenericJackson2JsonRedisSerializer()` (no mapper) → `RedisSerializer.json()`" in body
    assert "a bare `builder().build()` has no typing and reads stored entries as maps" in body
    assert "one exception: a mapper that a Jackson 2 only library hands out, jackson-2-3.md Rule B" in body
    assert "`enableDefaultTyping(PolymorphicTypeValidator)`, `enableUnsafeDefaultTyping()`" in body
    assert "all mean: use the counterpart, never add Jackson 2 for them" in body
    # one variable for a Kafka class and a Jackson 2 only library: the variable stays
    assert "leave it and give that library its own instance (jackson-2-3.md)" in body
    # Batch: same names, JsonMapper, no way back
    assert "keep their names but take `JsonMapper`" in body
    assert "going back to Jackson 2 cannot work" in body
    # pointers instead of repeating the other notes
    for pointer in ("boot4-packages.md", "spring-security-7.md", "jackson-2-3.md"):
        assert pointer in body


def test_the_note_fits_its_budget():
    text = (NOTES / NOTE).read_text(encoding="utf-8")
    assert len(text.splitlines()) <= 160
    assert len(_note().body()) <= 6600          # the body is what reaches the prompt
