"""The javax → jakarta package table of the analyzer (`sbmig/analyze/jakarta_packages.yml`, read by
`sbmig.analyze.jsp.jakarta_rules`; it lived under sbmig/triage/handlers until PLAN-v2 step 4)."""
from __future__ import annotations

from pathlib import Path

from sbmig.analyze import jsp


def test_table_lives_next_to_the_reader_and_loads():
    assert jsp._JAKARTA_TABLE == Path(jsp.__file__).resolve().with_name("jakarta_packages.yml")
    assert jsp._JAKARTA_TABLE.is_file()
    rules = dict(jsp.jakarta_rules())
    assert len(rules) >= 20
    assert rules["javax.persistence"] is True and "javax.security.cert" in rules
    assert "javax.sql" not in rules and "javax.xml.parsers" not in rules


def test_table_semantics_longest_prefix_and_non_recursive_rules():
    rules = jsp.jakarta_rules()
    assert jsp.javax_ee_token("javax.servlet.http.Cookie", rules)
    assert jsp.javax_ee_token("javax.annotation.security.RolesAllowed", rules)
    assert not jsp.javax_ee_token("javax.annotation.processing.Generated", rules)
    assert not jsp.javax_ee_token("javax.sql.DataSource", rules)
    non_recursive = [f for f, rec in rules if not rec]
    if non_recursive:                           # `<from>.<Class>` only, never a sub-package
        f = non_recursive[0]
        assert jsp.javax_ee_token(f"{f}.Foo", rules) and not jsp.javax_ee_token(f"{f}.sub.Foo", rules)
