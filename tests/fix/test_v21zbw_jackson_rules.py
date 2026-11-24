"""v2.1z-bw (docs/INTERFACES.md "v2.1z-bw", "Which rule applies"): two company runs were blocked as oscillations
because Rule A ("the module is on Jackson 3: finish the move") and Rule B ("a signature naming
`com.fasterxml.jackson.databind.*`: keep THAT file on Jackson 2") of the note `jackson-2-3.md` both matched one file,
and the note read `cannot access ObjectMapper … class file for com.fasterxml.jackson.databind.ObjectMapper not found`
as "add Jackson 2". The note now chooses the rule by the class the failing code talks to (Spring class / Jackson's own
API / another library with or without a Jackson 3 API), says that a mirror-image error means a wrong choice, and
system prompt rule 8 says the same. The note is still selected first for the errors of v2.1z-bu and v2.1z-bv."""
from __future__ import annotations

import re
from pathlib import Path

from sbmig.core.interfaces import FixContext
from sbmig.core.models import BuildError, ErrorCluster, ErrorKind, Phase
from sbmig.fix import ContextPacker, load_notes_index
from sbmig.fix.fixer import load_script_prompts

from . import test_v21zbu_jackson_codecs as bu
from . import test_v21zbv_jackson_catch as bv

NOTES = Path(__file__).resolve().parents[2] / "knowledge" / "notes"
NOTE = "jackson-2-3.md"
CANNOT_ACCESS = "cannot access ObjectMapper"
CLASS_FILE = "class file for com.fasterxml.jackson.databind.ObjectMapper not found"
OLD_RULE_8 = "a library or module still on Jackson 2 → keep that file on 2"
HIB_FILE = "app/src/main/java/com/example/app/JsonFormatConfig.java"
# a library that has both lines in one jar: the Jackson 3 mapper handed to its Jackson 2 class
HIB_SOURCE = """package com.example.app;

import org.hibernate.type.format.jackson.JacksonJsonFormatMapper;
import tools.jackson.databind.ObjectMapper;

public class JsonFormatConfig {
    JacksonJsonFormatMapper formatMapper(ObjectMapper objectMapper) {
        return new JacksonJsonFormatMapper(objectMapper);
    }
}
"""


def _note():
    return {e.file: e for e in load_notes_index(NOTES)}[NOTE]


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def _section(body: str, start: str, end: str) -> str:
    return _flat(body.split(start, 1)[1].split(end, 1)[0])


def _ctx() -> FixContext:
    return FixContext(phase=Phase.VERIFY, iteration=1, script_mode="new", case_id="k1")


def test_the_note_states_the_choice_before_rule_a():
    body = _note().body()
    assert body.index("## Which rule applies") < body.index("## Rule A")
    choice = _section(body, "## Which rule applies", "## Rule A")
    assert "Decide by the class the failing call talks to, never by the message alone" in choice
    # the two things that make the choice converge: one direction only, and a missing line is added
    assert ("code on `tools.jackson` is never moved back to `com.fasterxml.jackson`, and a missing line is added as "
            "a dependency, never avoided by moving code") in choice
    # a fully qualified Jackson 2 type in a Jackson 3 file was put there for a Jackson 2 only library: never undone
    assert ("A type written fully qualified as `com.fasterxml.jackson.…` in a file on `tools.jackson` marks the "
            "boundary to a Jackson 2 only library (Rule B): it stays") in choice
    assert "an argument typed `com.fasterxml.jackson.…` stays so and crosses as text (Rule B)" in choice
    assert "0. no file and no pom of the module uses `tools.jackson` (`<need_grep>` when unsure): Rule C" in choice
    assert "1. a Spring Framework / Spring Boot / Spring portfolio class (`org.springframework.*`): Rule A" in choice
    assert "`jackson-3-spring.md`" in choice and "`boot4-packages.md`" in choice
    assert "A Spring class no note names (Spring Cloud …): choice 3" in choice
    assert ("2. Jackson 3's own API (a call on a `tools.jackson` object: mapper methods, modules, serializers, "
            "exceptions): Rule A (`jackson-3-api.md`)") in choice
    assert ("3. another library: Rule A when a class of it that takes Jackson 3 types is on the classpath"
            in choice)
    assert "Hibernate's `Jackson3JsonFormatMapper(JsonMapper)`" in choice
    assert "elasticsearch-java's `Jackson3JsonpMapper(JsonMapper)`" in choice
    assert "otherwise Rule B (`jjwt-jackson`" in choice and "an in-house jar compiled against Jackson 2" in choice
    assert "Never swap the library's artifact for another to get a Jackson 3 class" in choice
    assert "once per file" not in choice and "mirror" not in choice


def test_a_missing_jackson3_is_added_never_avoided():
    """`package tools.jackson.… does not exist`: the module lacks Jackson 3 (its Jackson 2 came through another
    library); the dependency is added, the imports never go back."""
    facts = _flat(_note().body().split("## Which rule applies", 1)[0])
    assert ("`package tools.jackson.… does not exist` / `class file for tools.jackson.… not found` (Jackson 3 is "
            "missing from that module: add `tools.jackson.core:jackson-databind`, `version: null`; never move the "
            "imports back)") in facts


def test_class_file_not_found_no_longer_reads_as_add_jackson2():
    facts = _flat(_note().body().split("## Which rule applies", 1)[0])
    assert f"`{CANNOT_ACCESS} … {CLASS_FILE}`" in facts
    assert "(a Jackson 2 class used without Jackson 2 databind)" not in facts
    entry = facts.split(CLASS_FILE, 1)[1].split("; `package", 1)[0]
    assert "the same jar may hold a Jackson 3 class" in entry
    assert "choose the rule below first" in entry
    assert "Jackson 2 databind is added only under Rule B / C" in entry


def test_rule_b_trigger_no_longer_accepts_a_bare_databind_signature():
    rule_b = _section(_note().body(), "## Rule B", "## Rule C")
    assert "a signature naming `com.fasterxml.jackson.databind.*`" not in rule_b
    assert "Only by choice 3 above" in rule_b
    assert "a library that has no class taking Jackson 3 types" in rule_b
    assert ("never a Spring class the notes name, never a library whose Jackson 3 class is on the classpath"
            in rule_b)
    assert "`com.fasterxml.jackson.core:jackson-databind` (`version: null`)" in rule_b


def test_rule_b_never_moves_a_file_back():
    """Moving a file back to Jackson 2 is what looped: Rule B adds the dependency and names the Jackson 2 types at
    the boundary; a file is on Jackson 2 only when the recipe left it there."""
    rule_b = _section(_note().body(), "## Rule B", "## Rule C")
    assert "keep THAT file on `com.fasterxml.jackson.*`" not in rule_b
    assert ("A file the recipe left on `com.fasterxml.jackson.*` that uses Jackson only with that library stays as "
            "it is") in rule_b
    assert ("A file on `tools.jackson` keeps its imports: write the Jackson 2 types that library takes, returns or "
            "declares (an interface or base-class signature) fully qualified") in rule_b
    assert ("A call on such a Jackson 2 object follows Jackson 2: its checked "
            "`com.fasterxml.jackson.core.JsonProcessingException` / `IOException` is caught or declared; Rule A "
            "does not apply to it") in rule_b
    assert ("Never retype a variable of the file that a Jackson 3 call also uses and never cast between the lines: "
            "give the library its own Jackson 2 object") in rule_b
    assert "pass trees across as text" in rule_b
    assert "also from a parameter the library's signature fixes to Jackson 2" in rule_b
    # a mapper a company factory hands out stays, with the Spring Jackson 2 class it feeds (run-time data format)
    assert ("A mapper the library hands out (a company factory) has no Jackson 3 form: the Spring class it feeds (a "
            "Redis serializer, a converter) must be the Jackson 2 one. Keep that class, or put it back where an "
            "earlier fix renamed it, with the library's mapper declared fully qualified") in rule_b
    assert "and the Jackson 2 module artifacts a file kept on Jackson 2 imports" in rule_b
    assert "never replace the library's mapper with a new `JsonMapper`" in rule_b


def test_rule_c_brings_the_jackson2_modules_the_files_import():
    """A module that stays on Jackson 2 keeps `JavaTimeModule`: deleting it there compiles and fails at run time."""
    rule_c = _section(_note().body(), "## Rule C", "## Rule D")
    assert ("plus the Jackson 2 module artifacts the files import, e.g. "
            "`com.fasterxml.jackson.datatype:jackson-datatype-jsr310`") in rule_c


def test_rule_a_points_serializer_ioexception_to_the_api_note():
    rule_a = _section(_note().body(), "## Rule A", "## Rule B")
    assert rule_a.startswith(" — the module is on Jackson 3: finish the move in the failing file Chosen by 1-3 above")
    assert ("`unreported exception IOException` / `overridden method does not throw IOException` in a custom "
            "(de)serializer whose base type is `tools.jackson`: `jackson-3-api.md`") in rule_a
    assert "never put `throws` back on the override" in rule_a


def test_the_note_stays_within_its_size():
    text = (NOTES / NOTE).read_text(encoding="utf-8")
    assert len(text.splitlines()) <= 160


def test_rule_8_carries_the_choice_and_drops_the_old_sentence(cfg):
    load_script_prompts.cache_clear()
    system, _ = load_script_prompts("v1")
    flat = _flat(system)
    rule_8 = flat[flat.index("8. Target stack:"):flat.index("9. When a runtime concern remains")]
    assert OLD_RULE_8 not in flat
    assert ("code on `tools.jackson.*` is never moved back to `com.fasterxml.jackson.*` (never undo the recipe)"
            in rule_8)
    assert ("A Spring / Boot class that has a Jackson 3 counterpart, or Jackson 3's own API → finish the move "
            "(Rule A)") in rule_8
    assert ("A library with no Jackson 3 class → add `com.fasterxml.jackson.core:jackson-databind` "
            '(`"version": null`) and write the Jackson 2 types it needs fully qualified (Rule B)') in rule_8
    assert ("A module where no file and no pom uses `tools.jackson` → that dependency and the Jackson 2 modules "
            "its files import, imports unchanged (Rule C)") in rule_8
    assert ("`package tools.jackson.… does not exist` → add `tools.jackson.core:jackson-databind` "
            '(`"version": null`)') in rule_8
    assert "Never add `spring-boot-jackson2` to a converted project" in rule_8
    assert "`com.fasterxml.jackson.annotation.*` never changes" in rule_8
    # the reduced scope keeps its own rule 8 without Jackson
    reduced, _ = load_script_prompts("v1", (), None, "", "jdk-jakarta", str(cfg.target.java_version))
    assert "tools.jackson" not in reduced and OLD_RULE_8 not in _flat(reduced)


def test_the_note_is_still_first_for_the_earlier_errors(cfg, ws, put):
    title = _note().title
    packer = ContextPacker(cfg)
    put(bu.CODEC_FILE, bu.CODEC_SOURCE)
    p = packer.pack_script(ws, bu._cluster(bu.CONVERT_ERROR, 20), _ctx(), "case=spring_boot", case_type="spring_boot")
    assert p.notes_included[0] == title, p.notes_included
    assert "## Which rule applies" in dict(p.sections)[f"Migration note: {title}"]
    put(bv.SRC_FILE, bv.JACKSON_SOURCE)
    p = packer.pack_script(ws, bv._cluster(bv.ALREADY_CAUGHT, bv.CAUGHT_LINE), _ctx(), "case=spring_boot",
                           case_type="spring_boot")
    assert p.notes_included[0] == title, p.notes_included


def test_class_file_not_found_selects_the_note_with_the_choice(cfg, ws, put):
    put(HIB_FILE, HIB_SOURCE)
    title = _note().title
    err = BuildError(kind=ErrorKind.COMPILE, message=CANNOT_ACCESS, file=HIB_FILE, line=8, column=16,
                     location="class com.example.app.JsonFormatConfig", signature="s1",
                     raw=f"[ERROR] /p/{HIB_FILE}:[8,16] {CANNOT_ACCESS}\n  {CLASS_FILE}")
    cl = ErrorCluster(id="case:k1", kind=ErrorKind.COMPILE, errors=[err], case_id="k1", signatures=["s1"])
    p = ContextPacker(cfg).pack_script(ws, cl, _ctx(), "case=spring_boot", case_type="spring_boot")
    assert p.notes_included[0] == title, p.notes_included
    body = _flat(dict(p.sections)[f"Migration note: {title}"])
    assert "Hibernate's `Jackson3JsonFormatMapper(JsonMapper)`" in body
