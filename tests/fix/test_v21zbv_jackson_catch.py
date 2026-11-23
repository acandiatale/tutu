"""v2.1z-bv (docs/INTERFACES.md "v2.1z-bv"): a company project's fix loop was blocked as an oscillation on catch clauses
after the recipe moved `JsonProcessingException` to Jackson 3's unchecked `tools.jackson.core.JacksonException`:
`exception JacksonException has already been caught` (a Jackson catch after `catch (RuntimeException e)`) and
`exception IOException is never thrown in body of corresponding try statement` (a dead IOException catch around
Jackson 3 calls). The note `jackson-2-3.md` (Rule A) now says what to do for both, and its keywords select it for
these messages when they name a Jackson exception or the erroring file imports Jackson 3."""
from __future__ import annotations

import re
from pathlib import Path

from sbmig.core.interfaces import FixContext
from sbmig.core.models import BuildError, ErrorCluster, ErrorKind, Phase
from sbmig.fix import ContextPacker, load_notes_index

NOTES = Path(__file__).resolve().parents[2] / "knowledge" / "notes"
NOTE = "jackson-2-3.md"
SRC_FILE = "app/src/main/java/com/example/app/PayloadReader.java"
ALREADY_CAUGHT = "exception JacksonException has already been caught"
NEVER_THROWN = "exception IOException is never thrown in body of corresponding try statement"
CAUGHT_KEYWORD = ("exception (JacksonException|DatabindException|StreamReadException|StreamWriteException) "
                  "has already been caught")
# the shape of the company run: a Jackson catch after a broader RuntimeException catch, and a dead IOException catch
JACKSON_SOURCE = """package com.example.app;

import java.io.IOException;
import java.util.Map;

import tools.jackson.core.JacksonException;
import tools.jackson.databind.ObjectMapper;

public class PayloadReader {
    private final ObjectMapper mapper = new ObjectMapper();

    public Map<?, ?> read(String json) {
        try {
            return mapper.readValue(json, Map.class);
        } catch (RuntimeException e) {
            throw new IllegalStateException("unexpected", e);
        } catch (JacksonException e) {
            return Map.of();
        }
    }

    public Map<?, ?> parse(String json) {
        try {
            return mapper.readValue(json, Map.class);
        } catch (IOException e) {
            return Map.of();
        }
    }
}
"""
CAUGHT_LINE = 17          # `} catch (JacksonException e) {`
NEVER_THROWN_LINE = 25    # `} catch (IOException e) {`
# the same dead IOException catch in a file that has nothing to do with Jackson
PLAIN_SOURCE = """package com.example.app;

import java.io.IOException;
import java.util.Map;

public class PayloadReader {
    public Map<?, ?> parse(String text) {
        try {
            return Map.of("n", Integer.parseInt(text));
        } catch (IOException e) {
            return Map.of();
        }
    }
}
"""
PLAIN_LINE = 10


def _note():
    return {e.file: e for e in load_notes_index(NOTES)}[NOTE]


def _ctx() -> FixContext:
    return FixContext(phase=Phase.VERIFY, iteration=1, script_mode="new", case_id="k1")


def _cluster(message: str, line: int) -> ErrorCluster:
    err = BuildError(kind=ErrorKind.COMPILE, message=message, file=SRC_FILE, line=line, column=11,
                     location="class com.example.app.PayloadReader", signature="s1",
                     raw=f"[ERROR] /p/{SRC_FILE}:[{line},11] {message}")
    return ErrorCluster(id="case:k1", kind=ErrorKind.COMPILE, errors=[err], case_id="k1", signatures=["s1"])


def test_rule_a_carries_both_catch_rules_and_both_messages():
    body = _note().body()
    rule_a = re.sub(r"\s+", " ", body.split("## Rule A", 1)[1].split("## Rule B", 1)[0])
    # the conversion stays
    assert "`JsonProcessingException` / `JsonMappingException` → `tools.jackson.core.JacksonException`" in rule_a
    assert "extends `RuntimeException`" in rule_a and "`DatabindException` is its subclass" in rule_a
    # (a) a Jackson catch after a broader one: move it up, unchanged, delete nothing, no multi-catch
    assert f"`{ALREADY_CAUGHT}`" in rule_a
    assert ("move that catch block, unchanged, above the broader `catch (RuntimeException …)` / "
            "`catch (Exception …)` of the same try; delete neither block") in rule_a
    assert "never a multi-catch with `RuntimeException` / `Exception`" in rule_a
    # (b) a dead IOException catch around Jackson calls: retype it, keep the body, or remove it when a duplicate
    assert f"`{NEVER_THROWN}` where the try's only I/O source was a Jackson call" in rule_a
    assert "change that catch's type to `JacksonException`, keep its body" in rule_a
    assert "when the same try already catches `JacksonException`, remove the dead `IOException` block" in rule_a
    # the old wording ("drop a throws / catch that only existed for it") no longer contradicts (a)
    assert "drop a `throws` / `catch`" not in rule_a
    assert "a `catch` with its own handling is kept, never dropped" in rule_a
    assert "a `throws` clause that only existed for the checked Jackson 2 exception may go" in rule_a
    # both javac messages are listed under the typical errors
    facts = re.sub(r"\s+", " ", body.split("## Rule A", 1)[0])
    assert f"`{ALREADY_CAUGHT}`" in facts and f"`{NEVER_THROWN}`" in facts


def test_already_caught_keyword_matches_the_jackson_exceptions_only():
    note = _note()
    patterns = [rx.pattern for rx in note.compiled()]
    assert CAUGHT_KEYWORD in patterns
    for name in ("JacksonException", "DatabindException", "StreamReadException", "StreamWriteException"):
        text = f"exception {name} has already been caught"
        hit = [rx.pattern for rx in note.compiled() if rx.search(text)]
        assert CAUGHT_KEYWORD in hit, (name, hit)
    # DatabindException is named by no older keyword: the new one alone selects the note for it
    text = "exception DatabindException has already been caught"
    assert [rx.pattern for rx in note.compiled() if rx.search(text)] == [CAUGHT_KEYWORD]
    for text in ("exception IllegalArgumentException has already been caught",
                 "exception java.sql.SQLException has already been caught", NEVER_THROWN,
                 "exception SQLException is never thrown in body of corresponding try statement"):
        assert note.score(text) == 0, (text, [rx.pattern for rx in note.compiled() if rx.search(text)])


def test_already_caught_jackson_exception_selects_the_note(cfg, ws, put):
    put(SRC_FILE, JACKSON_SOURCE)
    packer = ContextPacker(cfg)
    title = _note().title
    for case_type in ("spring_boot", "msa", "spring_legacy"):
        p = packer.pack_script(ws, _cluster(ALREADY_CAUGHT, CAUGHT_LINE), _ctx(), f"case={case_type}",
                               case_type=case_type)
        assert p.notes_included[0] == title, (case_type, p.notes_included)
        body = re.sub(r"\s+", " ", dict(p.sections)[f"Migration note: {title}"])
        assert "move that catch block, unchanged, above the broader" in body


def test_never_thrown_ioexception_in_a_jackson3_file_selects_the_note(cfg, ws, put):
    """The message names no Jackson type; the erroring file's `tools.jackson` imports select the note."""
    put(SRC_FILE, JACKSON_SOURCE)
    packer = ContextPacker(cfg)
    title = _note().title
    for case_type in ("spring_boot", "spring_legacy", "pure_java"):
        p = packer.pack_script(ws, _cluster(NEVER_THROWN, NEVER_THROWN_LINE), _ctx(), f"case={case_type}",
                               case_type=case_type)
        assert title in p.notes_included, (case_type, p.notes_included)
        body = re.sub(r"\s+", " ", dict(p.sections)[f"Migration note: {title}"])
        assert "change that catch's type to `JacksonException`, keep its body" in body


def test_never_thrown_ioexception_without_jackson_does_not_select_the_note(cfg, ws, put):
    put(SRC_FILE, PLAIN_SOURCE)
    packer = ContextPacker(cfg)
    title = _note().title
    for case_type in ("spring_boot", "spring_legacy", "pure_java"):
        p = packer.pack_script(ws, _cluster(NEVER_THROWN, PLAIN_LINE), _ctx(), f"case={case_type}",
                               case_type=case_type)
        assert title not in p.notes_included, (case_type, p.notes_included)
