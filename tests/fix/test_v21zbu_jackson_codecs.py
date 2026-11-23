"""v2.1z-bu (docs/INTERFACES.md "v2.1z-bu", rule 1): a company run ended with a WebFlux module unfixed because the fix
loop did not know the Spring 7 Jackson 3 codecs. The module's code was on Jackson 3 (`tools.jackson`), its codec
configuration still built `new Jackson2JsonDecoder(objectMapper)`, and javac said
`tools.jackson.databind.ObjectMapper cannot be converted to com.fasterxml.jackson.databind.ObjectMapper`.
The note `jackson-2-3.md` now carries the codec rule (Rule A) and its keywords select it for such a case."""
from __future__ import annotations

import re
from pathlib import Path

from sbmig.core.interfaces import FixContext
from sbmig.core.models import BuildError, ErrorCluster, ErrorKind, Phase
from sbmig.fix import ContextPacker, load_notes_index

NOTES = Path(__file__).resolve().parents[2] / "knowledge" / "notes"
NOTE = "jackson-2-3.md"
CODEC_FILE = "web/src/main/java/com/example/web/CodecConfig.java"
CONVERT_ERROR = ("incompatible types: tools.jackson.databind.ObjectMapper cannot be converted to "
                 "com.fasterxml.jackson.databind.ObjectMapper")
# the shape the company run had: code on Jackson 3, codec configuration still on Spring's Jackson 2 codecs
CODEC_SOURCE = """package com.example.web;

import org.springframework.context.annotation.Configuration;
import org.springframework.http.codec.ServerCodecConfigurer;
import org.springframework.http.codec.json.Jackson2JsonDecoder;
import org.springframework.http.codec.json.Jackson2JsonEncoder;
import org.springframework.web.reactive.config.WebFluxConfigurer;
import tools.jackson.databind.ObjectMapper;

@Configuration
public class CodecConfig implements WebFluxConfigurer {
    private final ObjectMapper objectMapper;

    public CodecConfig(ObjectMapper objectMapper) {
        this.objectMapper = objectMapper;
    }

    @Override
    public void configureHttpMessageCodecs(ServerCodecConfigurer configurer) {
        configurer.defaultCodecs().jackson2JsonDecoder(new Jackson2JsonDecoder(objectMapper));
        configurer.defaultCodecs().jackson2JsonEncoder(new Jackson2JsonEncoder(objectMapper));
    }
}
"""
# a file whose only Jackson trace is the codec import (no tools.jackson, no ObjectMapper): only the codec keywords see it
IMPORT_ONLY_SOURCE = """package com.example.web;

import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.codec.json.Jackson2JsonDecoder;

@Configuration
public class CodecConfig {
    @Bean
    Jackson2JsonDecoder decoder() {
        return new Jackson2JsonDecoder(codecSettings.mapper());
    }
}
"""


def _note():
    return {e.file: e for e in load_notes_index(NOTES)}[NOTE]


def _ctx() -> FixContext:
    return FixContext(phase=Phase.VERIFY, iteration=1, script_mode="new", case_id="k1")


def _cluster(message: str, line: int, symbol: str | None = None) -> ErrorCluster:
    err = BuildError(kind=ErrorKind.COMPILE, message=message, file=CODEC_FILE, line=line, column=60, symbol=symbol,
                     location="class com.example.web.CodecConfig", signature="s1",
                     raw=f"[ERROR] /p/{CODEC_FILE}:[{line},60] {message}")
    return ErrorCluster(id="case:k1", kind=ErrorKind.COMPILE, errors=[err], case_id="k1", signatures=["s1"])


def test_rule_a_names_the_jackson3_codecs_and_forbids_going_back():
    body = _note().body()
    rule_a = body.split("## Rule A", 1)[1].split("## Rule B", 1)[0]
    flat = re.sub(r"\s+", " ", rule_a)
    for name in ("org.springframework.http.codec.json.Jackson2JsonDecoder", "JacksonJsonDecoder", "JacksonJsonEncoder",
                 "tools.jackson.databind.json.JsonMapper", "JsonMapper.Builder", "jacksonJsonDecoder(…)",
                 "jacksonJsonEncoder(…)", "org.springframework.http.codec.smile.JacksonSmileDecoder",
                 "JacksonSmileEncoder", "org.springframework.http.codec.cbor.JacksonCborDecoder", "JacksonCborEncoder",
                 "JsonMapper.builder()…build()"):
        assert name in flat, name
    assert "Boot 4.1 auto-configures that bean" in flat
    assert f"`{CONVERT_ERROR.split(': ', 1)[1]}` at a codec constructor is this case" in flat
    assert "never move the file, the mapper or the module back to Jackson 2" in flat
    assert "never remove a Jackson 3 dependency from a pom" in flat
    # the Jackson 3 codecs start in spring-web 7.0: a missing JacksonJsonDecoder means an as-is Spring pin wins
    assert ("`cannot find symbol … class JacksonJsonDecoder` (or `JacksonJsonEncoder`) with "
            "`location: package org.springframework.http.codec.json` means the module's spring-web is older than 7.0"
            in flat)
    assert "Remove the pin so the BOM manages Spring" in flat
    assert "never go back to `Jackson2JsonDecoder` or Jackson 2 for it" in flat
    # the typical-errors list keeps one bullet for the message and points at Rule A for the codec case
    facts = re.sub(r"\s+", " ", body.split("## Rule A", 1)[0])
    assert facts.count("cannot be converted to com.fasterxml.jackson.databind.ObjectMapper") == 1
    assert "WebFlux codec, Rule A" in facts


def test_codec_keywords_match_the_codec_names():
    note = _note()
    for text in ("new Jackson2JsonDecoder(mapper)", "new Jackson2JsonEncoder(mapper)", "Jackson2SmileDecoder",
                 "Jackson2CborEncoder", "configurer.defaultCodecs().jackson2JsonDecoder(d)", "jackson2SmileEncoder",
                 "new JacksonJsonDecoder(jsonMapper)", "JacksonJsonEncoder", "import org.springframework.http.codec."
                 "ServerCodecConfigurer;", "public void configureHttpMessageCodecs(ServerCodecConfigurer c)",
                 "ExchangeStrategies.builder().codecs(c -> c.defaultCodecs())"):
        assert note.score(text) > 0, text
    # the codec import alone is matched by the new keywords, not by the older Jackson ones
    line = "import org.springframework.http.codec.json.Jackson2JsonDecoder;"
    hit = [rx.pattern for rx in note.compiled() if rx.search(line)]
    assert hit == ["Jackson2(Json|Smile|Cbor)(Decoder|Encoder)"], hit


def test_codec_keywords_stay_off_unrelated_codec_and_webflux_text():
    note = _note()
    for text in ("import org.springframework.http.codec.json.KotlinSerializationJsonDecoder;",
                 "cannot find symbol\n  symbol: class Jaxb2XmlDecoder",
                 "import org.springframework.http.codec.multipart.FilePart;",
                 "import org.springframework.web.reactive.function.client.WebClient;",
                 "cannot find symbol\n  symbol: class Jackson2JsonRedisSerializer"):
        assert note.score(text) == 0, (text, [rx.pattern for rx in note.compiled() if rx.search(text)])


def test_the_codec_convert_error_selects_the_note(cfg, ws, put):
    put(CODEC_FILE, CODEC_SOURCE)
    packer = ContextPacker(cfg)
    title = _note().title
    for case_type in ("spring_boot", "msa", "spring_legacy"):
        p = packer.pack_script(ws, _cluster(CONVERT_ERROR, 20), _ctx(), f"case={case_type}", case_type=case_type)
        assert p.notes_included[0] == title, (case_type, p.notes_included)
        body = dict(p.sections)[f"Migration note: {title}"]
        assert "JacksonJsonDecoder" in body and "jacksonJsonDecoder(…)" in body


def test_a_missing_jackson3_codec_class_selects_the_note(cfg, ws, put):
    """spring-web older than 7.0 (an as-is pin over the Boot BOM) has no JacksonJsonDecoder: that error selects the note."""
    put(CODEC_FILE, CODEC_SOURCE.replace("Jackson2Json", "JacksonJson"))
    packer = ContextPacker(cfg)
    title = _note().title
    err = BuildError(kind=ErrorKind.COMPILE, message="cannot find symbol", file=CODEC_FILE, line=5, column=41,
                     symbol="class JacksonJsonDecoder", location="package org.springframework.http.codec.json",
                     signature="s1", raw=f"[ERROR] /p/{CODEC_FILE}:[5,41] cannot find symbol\n"
                                         "  symbol:   class JacksonJsonDecoder\n"
                                         "  location: package org.springframework.http.codec.json")
    cl = ErrorCluster(id="case:k1", kind=ErrorKind.COMPILE, errors=[err], case_id="k1", signatures=["s1"])
    p = packer.pack_script(ws, cl, _ctx(), "case=spring_boot", case_type="spring_boot")
    assert p.notes_included[0] == title, p.notes_included
    assert "older than 7.0" in dict(p.sections)[f"Migration note: {title}"]


def test_a_file_importing_the_jackson2_codec_selects_the_note(cfg, ws, put):
    """The error text names no Jackson type; the erroring file's import of `Jackson2JsonDecoder` selects the note."""
    put(CODEC_FILE, IMPORT_ONLY_SOURCE)
    packer = ContextPacker(cfg)
    title = _note().title
    cl = _cluster("cannot find symbol", 11, symbol="variable codecSettings")
    p = packer.pack_script(ws, cl, _ctx(), "case=spring_boot", case_type="spring_boot")
    assert title in p.notes_included, p.notes_included
    assert "JsonMapper" in dict(p.sections)[f"Migration note: {title}"]
