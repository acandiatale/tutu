"""v2.1z-bw (docs/INTERFACES.md "v2.1z-bw"): two company runs ended blocked because the fix LLM went back and forth
between Jackson 2 and Jackson 3 where the notes gave no answer. A survey ran `UpgradeJackson_2_3` on code that uses
Jackson's own API and compiled the result against Jackson 3.1.4: mapper setters on an injected mapper, the modules
built into databind 3, `throws IOException` on serializer overrides and their helpers, and names the recipe writes
that do not exist in 3.1. The note `jackson-3-api.md` answers those errors; its keywords select it for them and keep
it out of the earlier rounds' shapes (WebFlux codecs, duplicate catch clauses) and of unrelated `IOException` errors.
The error texts below are the stored javac messages of that survey, with the sources moved to `com.example`."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from sbmig.core.interfaces import FixContext
from sbmig.core.models import BuildError, ErrorCluster, ErrorKind, Phase
from sbmig.fix import ContextPacker, load_notes_index

from .test_v21zbu_jackson_codecs import CODEC_FILE, CODEC_SOURCE, CONVERT_ERROR
from .test_v21zbv_jackson_catch import ALREADY_CAUGHT, CAUGHT_LINE, JACKSON_SOURCE

NOTES = Path(__file__).resolve().parents[2] / "knowledge" / "notes"
NOTE = "jackson-3-api.md"
RULES_NOTE = "jackson-2-3.md"
PKG = "app/src/main/java/com/example/app"

# a mapper that arrives from outside and is mutated (what the recipe leaves: it folds only local / static mappers)
MAPPER_SOURCE = """package com.example.app;

import com.fasterxml.jackson.annotation.JsonInclude;
import tools.jackson.databind.DeserializationFeature;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.module.SimpleModule;

import java.text.SimpleDateFormat;

public class MapperConfig {
    private final ObjectMapper objectMapper;

    public MapperConfig(ObjectMapper objectMapper) {
        this.objectMapper = objectMapper;
    }

    public void init() {
        objectMapper.configure(DeserializationFeature.ACCEPT_SINGLE_VALUE_AS_ARRAY, true);
        objectMapper.setDefaultPropertyInclusion(JsonInclude.Include.NON_NULL);
        objectMapper.setDateFormat(new SimpleDateFormat("yyyy-MM-dd HH:mm:ss"));
        objectMapper.registerModule(new SimpleModule());
    }
}
"""
# the recipe deleted `JavaTimeModule module = new JavaTimeModule();` and its addSerializer lines, not `return module;`
JAVATIME_SOURCE = """package com.example.app;

import tools.jackson.databind.json.JsonMapper;
import tools.jackson.databind.JacksonModule;

import java.time.format.DateTimeFormatter;

public class JavaTimeConfig {
    private static final DateTimeFormatter FMT = DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss");

    public JacksonModule javaTimeModule() {
        return module;
    }

    public JsonMapper mapper() {
        return JsonMapper.builder().build();
    }
}
"""
# a serializer whose private helper still declares IOException (second wave after the recipe)
SERIALIZER_SOURCE = """package com.example.app;

import tools.jackson.core.JsonGenerator;
import tools.jackson.databind.SerializationContext;
import tools.jackson.databind.ser.std.StdSerializer;

import java.io.IOException;
import java.math.BigDecimal;

public class MoneySerializer extends StdSerializer<BigDecimal> {

    public MoneySerializer() {
        super(BigDecimal.class);
    }

    @Override
    public void serialize(BigDecimal value, JsonGenerator gen, SerializationContext provider) {
        gen.writeStartObject();
        writeAmount(gen, value);
        gen.writeEndObject();
    }

    private void writeAmount(JsonGenerator gen, BigDecimal value) throws IOException {
        gen.writeStringProperty("amount", value.toPlainString());
        gen.writeObjectProperty("scale", value.scale());
    }
}
"""
# the tempting counter-fix: `throws IOException` back on the override
THROWS_BACK_SOURCE = SERIALIZER_SOURCE.replace(
    "SerializationContext provider) {", "SerializationContext provider) throws IOException {")
NAMING_SOURCE = """package com.example.app;

import tools.jackson.databind.PropertyNamingStrategies;
import tools.jackson.databind.json.JsonMapper;

public class Naming {
    public JsonMapper snake() {
        tools.jackson.databind.PropertyNamingStrategies strategy = PropertyNamingStrategies.SNAKE_CASE;
        return JsonMapper.builder().propertyNamingStrategy(strategy).build();
    }
}
"""
MODIFIER_SOURCE = """package com.example.app;

import tools.jackson.databind.ser.BeanSerializerModifier;

public class NullToEmptyModifier extends BeanSerializerModifier {
}
"""
# the same unreported IOException in a file that has nothing to do with Jackson
PLAIN_SOURCE = """package com.example.app;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;

public class FileReader {
    public String read(Path path) {
        return load(path);
    }

    private String load(Path path) throws IOException {
        return Files.readString(path);
    }
}
"""

UNREPORTED = "unreported exception IOException; must be caught or declared to be thrown"
OVERRIDE_MSG = ("serialize(BigDecimal,JsonGenerator,SerializationContext) in MoneySerializer cannot override "
                "serialize(T,JsonGenerator,SerializationContext) in StdSerializer")
OVERRIDE_DETAIL = ("  overridden method does not throw IOException\n  where T is a type-variable:\n"
                   "    T extends Object declared in class StdSerializer")

# (name, file, source, line, message, symbol, location, detail lines of the raw javac output)
CASES = [
    ("injected-mapper-setter", "MapperConfig.java", MAPPER_SOURCE, 20, "cannot find symbol",
     "method setDateFormat(SimpleDateFormat)", "variable objectMapper of type ObjectMapper", ""),
    ("injected-mapper-register", "MapperConfig.java", MAPPER_SOURCE, 21, "cannot find symbol",
     "method registerModule(SimpleModule)", "variable objectMapper of type ObjectMapper", ""),
    ("javatime-module-leftover", "JavaTimeConfig.java", JAVATIME_SOURCE, 12, "cannot find symbol",
     "variable module", "class com.example.app.JavaTimeConfig", ""),
    ("override-throws-ioexception", "MoneySerializer.java", THROWS_BACK_SOURCE, 17, OVERRIDE_MSG, None, None,
     OVERRIDE_DETAIL),
    ("helper-unreported-ioexception", "MoneySerializer.java", SERIALIZER_SOURCE, 19, UNREPORTED, None, None, ""),
    ("recipe-writeObjectProperty", "MoneySerializer.java", SERIALIZER_SOURCE, 25, "cannot find symbol",
     "method writeObjectProperty(String,int)", "variable gen of type JsonGenerator", ""),
    ("recipe-naming-strategies", "Naming.java", NAMING_SOURCE, 9,
     "incompatible types: PropertyNamingStrategies cannot be converted to PropertyNamingStrategy", None, None, ""),
    ("recipe-bean-serializer-modifier", "NullToEmptyModifier.java", MODIFIER_SOURCE, 3, "cannot find symbol",
     "class BeanSerializerModifier", "package tools.jackson.databind.ser", ""),
]


def _notes():
    return {e.file: e for e in load_notes_index(NOTES)}


def _note():
    return _notes()[NOTE]


def _ctx() -> FixContext:
    return FixContext(phase=Phase.VERIFY, iteration=1, script_mode="new", case_id="k1")


def _cluster(file: str, line: int, message: str, symbol: str | None = None, location: str | None = None,
             detail: str = "") -> ErrorCluster:
    raw = f"[ERROR] /p/{file}:[{line},9] {message}"
    if symbol:
        raw += f"\n  symbol:   {symbol}"
    if location:
        raw += f"\n  location: {location}"
    if detail:
        raw += "\n" + detail
    err = BuildError(kind=ErrorKind.COMPILE, message=message, file=file, line=line, column=9, symbol=symbol,
                     location=location, signature="s1", raw=raw)
    return ErrorCluster(id="case:k1", kind=ErrorKind.COMPILE, errors=[err], case_id="k1", signatures=["s1"])


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_the_api_errors_select_the_note_first_or_after_the_rules_note(cfg, ws, put, case):
    _, name, source, line, message, symbol, location, detail = case
    rel = f"{PKG}/{name}"
    put(rel, source)
    packer = ContextPacker(cfg)
    notes = _notes()
    title, rules = notes[NOTE].title, notes[RULES_NOTE].title
    for case_type in ("spring_boot", "spring_legacy", "msa"):
        p = packer.pack_script(ws, _cluster(rel, line, message, symbol, location, detail), _ctx(),
                               f"case={case_type}", case_type=case_type)
        got = p.notes_included
        assert title in got, (case_type, got)
        pos = got.index(title)
        assert pos == 0 or (pos == 1 and got[0] == rules), (case_type, got)
        assert f"Migration note: {title}" in dict(p.sections)


def test_the_note_stays_out_of_the_codec_and_catch_cases(cfg, ws, put):
    packer = ContextPacker(cfg)
    title = _note().title
    put(CODEC_FILE, CODEC_SOURCE)
    codec = _cluster(CODEC_FILE, 20, CONVERT_ERROR)
    put(f"{PKG}/PayloadReader.java", JACKSON_SOURCE)
    caught = _cluster(f"{PKG}/PayloadReader.java", CAUGHT_LINE, ALREADY_CAUGHT)
    for cl in (codec, caught):
        for case_type in ("spring_boot", "spring_legacy"):
            p = packer.pack_script(ws, cl, _ctx(), f"case={case_type}", case_type=case_type)
            assert title not in p.notes_included, (cl.errors[0].message, p.notes_included)
            assert p.notes_included[0] == _notes()[RULES_NOTE].title


def test_the_note_stays_out_of_an_unrelated_ioexception_error(cfg, ws, put):
    rel = f"{PKG}/FileReader.java"
    put(rel, PLAIN_SOURCE)
    packer = ContextPacker(cfg)
    title = _note().title
    for case_type in ("spring_boot", "spring_legacy", "pure_java"):
        p = packer.pack_script(ws, _cluster(rel, 9, UNREPORTED), _ctx(), f"case={case_type}", case_type=case_type)
        assert title not in p.notes_included, (case_type, p.notes_included)


def test_stored_javac_fragments_hit_a_keyword_and_broad_text_does_not():
    note = _note()
    for text in ("location: variable objectMapper of type ObjectMapper",
                 "symbol:   method configure(DeserializationFeature,boolean)\n  location: class CustomObjectMapper",
                 "symbol:   variable WRITE_BIGDECIMAL_AS_PLAIN\n  location: class SerializationFeature",
                 "symbol:   method visibility(PropertyAccessor,Visibility)\n  location: class Builder",
                 "import tools.jackson.databind.ext.javatime.JavaTimeModule;",
                 "package tools.jackson.module.paramnames does not exist",
                 "symbol:   method defaultSerializeField(String,BigDecimal,JsonGenerator)",
                 "symbol:   class ObjectCodec",
                 ("deserializeKey(String,DeserializationContext) in CodeKeyDeserializer cannot override "
                  "deserializeKey(String,DeserializationContext) in KeyDeserializer"),
                 "incompatible types: JacksonException cannot be converted to IOException",
                 "constructor DatabindException in class DatabindException cannot be applied to given types;",
                 "symbol:   method addDeserializationProblemHandler(LenientHandler)",
                 "incompatible types: PropertyNamingStrategy cannot be converted to PropertyNamingStrategies",
                 "<artifactId>jackson-datatype-jsr310</artifactId>"):
        assert note.score(text) > 0, text
    for text in ("import tools.jackson.databind.ObjectMapper;", "import tools.jackson.core.JacksonException;",
                 CONVERT_ERROR, ALREADY_CAUGHT, UNREPORTED,
                 "method does not override or implement a method from a supertype",
                 "cannot find symbol\n  symbol:   method setTimeZone(TimeZone)\n  location: variable fmt of type DateFormat",
                 "import com.fasterxml.jackson.databind.ObjectMapper;"):
        assert note.score(text) == 0, (text, [rx.pattern for rx in note.compiled() if rx.search(text)])
    broad = {r"tools\.jackson\.", "ObjectMapper", "JsonMapper", "ObjectMapper|JsonMapper", "IOException"}
    assert not broad & set(note.keywords)
    assert len(note.keywords) == len(set(note.keywords))


def test_index_entry_follows_the_rules_note_and_matches_the_front_matter():
    data = yaml.safe_load((NOTES / "index.yml").read_text(encoding="utf-8"))
    files = [n["file"] for n in data["notes"]]
    assert files[files.index(RULES_NOTE) + 1] == NOTE
    entry = data["notes"][files.index(NOTE)]
    text = (NOTES / NOTE).read_text(encoding="utf-8")
    front = yaml.safe_load(re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL).group(1))
    assert entry["keywords"] == front["keywords"] and entry["title"] == front["title"]
    assert entry["cases"] == [] and entry["phases"] == ["spring"] and entry["always"] is False
    # the body is what reaches the prompt (the front matter is cut off)
    assert len(text.splitlines()) <= 160 and len(_note().body()) <= 7200


def test_the_note_pins_the_answers_a_wrong_edit_would_lose():
    body = _flat(_note().body())
    # an injected mapper is rebuilt, not mutated; Boot's mapper is customized elsewhere
    assert "`this.mapper = injected.rebuild()…build();`, then use that" in body
    assert "`JsonMapperBuilderCustomizer` bean (boot4-packages.md)" in body
    assert "a `rebuild()` copy there compiles but leaves the shared mapper unconfigured" in body
    # replacements that compile either way and differ at run time (checked on Jackson 2.21.4 / 3.1.4)
    assert "incl.withValueInclusion(X).withContentInclusion(X)" in body
    assert "value alone starts writing null map entries" in body
    assert "vc.withCreatorVisibility(Visibility.NONE).withScalarConstructorVisibility(Visibility.NONE)" in body
    assert "`new UncheckedIOException(new IOException(e))`" in body
    assert "On a generator made from a bare `JsonFactory` that throws at run time" in body
    # default typing, the staple of Redis configurations (compiled and run on 2.21.4 / 3.1.4)
    assert "`ObjectMapper.DefaultTyping`" in body and "→ `tools.jackson.databind.DefaultTyping`" in body
    assert "with the SAME arguments (a `JsonTypeInfo.As.PROPERTY` argument stays" in body
    assert "`tools.jackson.databind.cfg.DateTimeFeature.WRITE_DATES_AS_TIMESTAMPS`" in body
    assert "`LaissezFaireSubTypeValidator is not public in tools.jackson.databind.jsontype.impl`" in body
    assert "`BasicPolymorphicTypeValidator.builder().allowIfBaseType(Object.class) .build()`" in body
    # the note is for code on Jackson 3 only
    assert "An object typed `com.fasterxml.jackson…`" in body and "keeps Jackson 2's API and its checked exceptions" in body
    # the built-in modules: never the Jackson 2 artifacts back
    assert ("Never add `jackson-datatype-jsr310`, `jackson-datatype-jdk8`, `jackson-module-parameter-names` "
            "(Jackson 2) to code on `tools.jackson`") in body
    # ... and a file that stays on Jackson 2 keeps its module (deleting it there compiles and fails at run time)
    assert "A file still on `com.fasterxml.jackson`" in body and "keeps its module" in body
    assert "add `com.fasterxml.jackson.datatype:jackson-datatype-jsr310`" in body
    assert "the recipe deleted `JavaTimeModule module = …` WITH its `addSerializer` lines" in body
    # IOException: the helper loses `throws` too; putting it back on the override is the wrong direction
    assert "`overridden method does not throw IOException`" in body
    assert ("the helper must lose `throws IOException` too. Putting `throws` back on the override is the wrong "
            "direction") in body
    assert "`JacksonException cannot be converted to IOException`" in body
    # the recipe's names that do not exist in 3.1
    assert "`writeObjectProperty(name, v)` → `writePOJOProperty(name, v)`" in body
    assert "`BeanSerializerModifier` → `tools.jackson.databind.ser.ValueSerializerModifier`" in body
    assert "keep `@Override`" in body
    assert "declare it `tools.jackson.databind.PropertyNamingStrategy`" in body
    # the rules, codecs and catch clauses stay in jackson-2-3.md
    for other in ("Rule B", "JacksonJsonDecoder", "has already been caught", "MappingJackson2HttpMessageConverter"):
        assert other not in body, other
