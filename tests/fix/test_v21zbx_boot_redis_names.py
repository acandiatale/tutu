"""v2.1z-bx (docs/INTERFACES.md "v2.1z-bx"): a company run oscillated on `RedisConnectionDetails`. Spring Boot 4 moved
it to `org.springframework.boot.data.redis.autoconfigure` AND renamed it `DataRedisConnectionDetails`
(`RedisProperties` -> `DataRedisProperties`). The note `boot4-packages.md` only said "auto-configuration classes moved to
`org.springframework.boot.<tech>.autoconfigure`", so the moved import named a class that does not exist, the LLM read
that as "removed" and ported the code to another type whose old name is gone as well. The note now lists the renames
(checked in the 4.1.0 and 3.3.13 jars) and says a moved name that is not found was renamed, not removed."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from sbmig.core.interfaces import FixContext
from sbmig.core.models import BuildError, ErrorCluster, ErrorKind, Phase
from sbmig.fix import ContextPacker, load_notes_index

NOTES = Path(__file__).resolve().parents[2] / "knowledge" / "notes"
NOTE = "boot4-packages.md"
FILE = "app/src/main/java/com/example/app/RedisConfig.java"
# the shape of the company run: the import already points at the Boot 4 package, with the Boot 3 simple name
SOURCE = """package com.example.app;

import org.springframework.boot.data.redis.autoconfigure.RedisConnectionDetails;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.data.redis.connection.RedisStandaloneConfiguration;
import org.springframework.data.redis.connection.lettuce.LettuceConnectionFactory;

@Configuration
public class RedisConfig {
    @Bean
    LettuceConnectionFactory connectionFactory(RedisConnectionDetails details) {
        RedisConnectionDetails.Standalone s = details.getStandalone();
        return new LettuceConnectionFactory(new RedisStandaloneConfiguration(s.getHost(), s.getPort()));
    }
}
"""
CASES = {
    "import-line": ("class RedisConnectionDetails", "package org.springframework.boot.data.redis.autoconfigure", 3),
    "usage": ("class RedisConnectionDetails", "class RedisConfig", 12),
    "properties": ("class RedisProperties", "package org.springframework.boot.data.redis.autoconfigure", 3),
}


def _note():
    return {e.file: e for e in load_notes_index(NOTES)}[NOTE]


def _body() -> str:
    return re.sub(r"\s+", " ", _note().body())


@pytest.mark.parametrize("name", sorted(CASES))
def test_the_missing_boot3_redis_name_selects_the_note_with_the_rename(cfg, ws, put, name):
    symbol, location, line = CASES[name]
    put(FILE, SOURCE)
    err = BuildError(kind=ErrorKind.COMPILE, message="cannot find symbol", file=FILE, line=line, column=8, symbol=symbol,
                     location=location, signature="s1", raw=f"[ERROR] /p/{FILE}:[{line},8] cannot find symbol")
    cl = ErrorCluster(id="case:k1", kind=ErrorKind.COMPILE, errors=[err], case_id="k1", signatures=["s1"])
    ctx = FixContext(phase=Phase.VERIFY, iteration=1, script_mode="new", case_id="k1")
    p = ContextPacker(cfg).pack_script(ws, cl, ctx, "case=spring_boot", case_type="spring_boot")
    title = _note().title
    assert title in p.notes_included, p.notes_included
    text = re.sub(r"\s+", " ", dict(p.sections)[f"Migration note: {title}"])
    assert "`RedisConnectionDetails` → `DataRedisConnectionDetails`" in text
    assert "`RedisProperties` → `DataRedisProperties`" in text


def test_the_note_says_renamed_not_removed():
    body = _body()
    assert ("a moved name that is not found in the new package was renamed, not removed. Never port the code to "
            "another type for it") in body
    assert "`autoconfigure.data.redis` → `org.springframework.boot.data.redis.autoconfigure`" in body
    assert "`Redis[Reactive|Repositories]AutoConfiguration` → `DataRedis[Reactive|Repositories]AutoConfiguration`" in body
    # Kafka only moved; the JPA repositories auto-configuration got the prefix too
    assert "`autoconfigure.kafka` → `org.springframework.boot.kafka.autoconfigure` (spring-boot-kafka), names unchanged" in body
    assert "`org.springframework.boot.data.jpa.autoconfigure.DataJpaRepositoriesAutoConfiguration`" in body


def test_the_new_keyword_matches_the_old_and_new_names_only():
    note = _note()
    kw = next(k for k in note.keywords if "RedisConnectionDetails" in k or "Redis(ConnectionDetails" in k)
    rx = re.compile(kw)
    for text in ("symbol: class RedisConnectionDetails", "symbol: class RedisProperties", "class DataRedisProperties",
                 "RedisAutoConfiguration", "RedisReactiveAutoConfiguration", "symbol: class KafkaProperties"):
        assert rx.search(text), text
    for text in ("RedisTemplate", "RedisConnectionFactory", "StringRedisSerializer", "KafkaTemplate"):
        assert not rx.search(text), text
