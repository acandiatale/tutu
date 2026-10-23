"""knowledge/libraries/jar_names.yml — jar file name stems → original Maven coordinates (`sbmig inventory`, 2026-10-23)."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

TABLE = Path(__file__).resolve().parents[2] / "knowledge" / "libraries" / "jar_names.yml"
_GA = re.compile(r"^[A-Za-z0-9_.\-]+:[A-Za-z0-9_.\-]+$")


def _rows() -> list[dict]:
    doc = yaml.safe_load(TABLE.read_text(encoding="utf-8")) or {}
    assert doc.get("version") == 1
    return list(doc.get("jars") or [])


def test_table_rows_are_well_formed():
    rows = _rows()
    assert len(rows) >= 60
    stems = [str(r["stem"]) for r in rows]
    assert len(stems) == len({s.lower() for s in stems}), "duplicate stems (case-insensitive)"
    for r in rows:
        stem = str(r["stem"])
        assert stem == stem.strip() and " " not in stem and not stem.lower().endswith(".jar"), stem
        assert not re.search(r"[-_]\d", stem), f"{stem}: the version belongs to the file name, not the stem"
        assert _GA.match(str(r["artifact"])), r
        if r.get("note") is not None:
            assert str(r["note"]).strip()


def test_common_era_jars_are_covered():
    by = {str(r["stem"]).lower(): str(r["artifact"]) for r in _rows()}
    assert by["commons-lang"] == "commons-lang:commons-lang"
    assert by["commons-lang3"] == "org.apache.commons:commons-lang3"
    assert by["servlet-api"] == "javax.servlet:servlet-api"
    assert by["jstl"] == "javax.servlet:jstl" and by["standard"] == "taglibs:standard"
    assert by["log4j"] == "log4j:log4j" and by["mysql-connector-java"] == "mysql:mysql-connector-java"
    assert by["ibatis-sqlmap"] == "org.apache.ibatis:ibatis-sqlmap" and by["mybatis"] == "org.mybatis:mybatis"
    assert by["cos"] == "servlets.com:cos"
