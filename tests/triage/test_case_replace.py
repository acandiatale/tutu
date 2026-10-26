"""Handler case-replace (sbmig/triage/handlers/case_replace.py): learned text replaces of catalog cases.

The catalog is faked through ``sys.modules["sbmig.kb.catalog"]`` so these tests do not depend on the KB module."""
from __future__ import annotations

import re
import sys
import types
from pathlib import Path
from typing import ClassVar

import pytest

from sbmig.core.models import (
    BuildError,
    CaseAction,
    CaseHit,
    ErrorCluster,
    ErrorKind,
    ReplacePair,
    SourceStats,
)
from sbmig.triage import HandlerRegistry
from sbmig.triage.handlers import case_replace as cr

from .conftest import make_profile

FILTER = "src/main/java/com/acme/web/ClientIpFilter.java"
SIG = "compile|cannot_find_symbol|clientIp"


class FakeCatalog:
    """Stand-in for sbmig.kb.catalog.CaseCatalog: `cases` = {signature_generic: (id, name, status, action)}."""
    cases: ClassVar[dict] = {}
    instances: ClassVar[list] = []
    fail = False

    def __init__(self, kb_dir, cfg=None):
        self.kb_dir = Path(kb_dir)
        FakeCatalog.instances.append(self)

    def match(self, errors):
        if FakeCatalog.fail:
            raise RuntimeError("index half-written")
        hits = []
        for sig, (cid, name, status, action) in FakeCatalog.cases.items():
            errs = [e for e in errors if e.signature_generic == sig]
            if errs:
                hits.append(CaseHit(case_id=cid, name=name, status=status, action=action, errors=errs))
        return hits


@pytest.fixture()
def catalog(monkeypatch):
    mod = types.ModuleType("sbmig.kb.catalog")
    mod.CaseCatalog = FakeCatalog
    monkeypatch.setitem(sys.modules, "sbmig.kb.catalog", mod)
    monkeypatch.setattr(FakeCatalog, "cases", {})
    monkeypatch.setattr(FakeCatalog, "instances", [])
    monkeypatch.setattr(FakeCatalog, "fail", False)
    cr.clear_catalog_cache()
    yield FakeCatalog
    cr.clear_catalog_cache()


@pytest.fixture()
def rcfg(cfg, tmp_path):
    kb = tmp_path / "kb"
    (kb / "catalog").mkdir(parents=True)
    cfg.kb.dir = str(kb)
    cfg.kb.catalog = True
    cfg.kb.case_replace = True
    cfg.kb.case_replace_scope = "error-lines"
    return cfg


def _replace(*pairs: tuple[str, str]) -> CaseAction:
    return CaseAction(kind="replace", replace_pairs=[ReplacePair(old=o, new=n) for o, n in pairs])


def _case(sig=SIG, cid="a1b2c3d4e5", name="LegacyRequestHelper.clientIp — cannot find symbol", status="candidate",
          action=None):
    FakeCatalog.cases[sig] = (cid, name, status, action or _replace(("clientIp", "remoteClientIp")))


def _err(line=5, file=FILTER, sig=SIG, kind=ErrorKind.COMPILE) -> BuildError:
    return BuildError(kind=kind, message="cannot find symbol", file=file, line=line, symbol="method clientIp()",
                      signature=f"{sig}|{line}", signature_generic=sig)


def _cluster(*errs: BuildError, kind=ErrorKind.COMPILE) -> ErrorCluster:
    return ErrorCluster(id="c1", kind=kind, file=errs[0].file if errs else None, errors=list(errs))


def _write(root: Path, rel: str, data: str | bytes) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        p.write_bytes(data)
    else:
        p.write_text(data, encoding="utf-8")
    return p


SRC = """package com.acme.web;

import static com.acme.legacy.LegacyRequestHelper.clientIp;

class ClientIpFilter {
    String a(Req r) { return clientIp(r); }
    String b(Req r) { return clientIp(r); }
}
"""                 # line 6 = a (reported below), line 7 = b


def _plain(notes):
    """Handler notes without the round-10 `resolver: case-replace:<id>:<hash>` lines (read by the error tracker)."""
    return [n for n in notes if not n.startswith("resolver: ")]


def _handler(fake_kit):
    return fake_kit.handler("case-replace")


# ---------------------------------------------------------------------------------------------------------------------
# matching
# ---------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("status", ["candidate", "active"])
def test_matches_candidate_and_active(fake_kit, catalog, rcfg, status):
    _case(status=status)
    h = _handler(fake_kit)
    assert h.matches(_cluster(_err()), make_profile(), rcfg)
    assert h.matches(_cluster(_err(kind=ErrorKind.TEST_COMPILE), kind=ErrorKind.TEST_COMPILE), None, rcfg)


@pytest.mark.parametrize("status,action", [
    ("observed", None),
    ("rejected", None),
    ("candidate", CaseAction(kind="hint")),
    ("candidate", CaseAction(kind="recipe", recipe_ids=["x"])),
    ("candidate", CaseAction(kind="replace")),                                   # no pairs
    ("candidate", _replace(("clientIp", "clientIp"))),                           # no-op pair
])
def test_no_match_status_or_action(fake_kit, catalog, rcfg, status, action):
    _case(status=status, action=action)
    assert not _handler(fake_kit).matches(_cluster(_err()), make_profile(), rcfg)


def test_no_match_without_file_or_line(fake_kit, catalog, rcfg):
    _case()
    h = _handler(fake_kit)
    no_line = BuildError(kind=ErrorKind.COMPILE, message="cannot find symbol", file=FILTER, signature_generic=SIG)
    no_file = BuildError(kind=ErrorKind.COMPILE, message="cannot find symbol", line=5, signature_generic=SIG)
    assert not h.matches(_cluster(no_line, no_file), make_profile(), rcfg)
    assert h.matches(_cluster(no_line, _err()), make_profile(), rcfg)        # one located error is enough


def test_no_match_config_switches(fake_kit, catalog, rcfg):
    _case()
    h, cl = _handler(fake_kit), _cluster(_err())
    assert h.matches(cl, None, rcfg)
    assert not h.matches(cl, None, None)
    rcfg.kb.case_replace = False
    assert not h.matches(cl, None, rcfg)
    rcfg.kb.case_replace, rcfg.kb.catalog = True, False
    assert not h.matches(cl, None, rcfg)


def test_no_match_other_kind_or_unknown_error(fake_kit, catalog, rcfg):
    _case()
    h = _handler(fake_kit)
    dep = _err(kind=ErrorKind.DEPENDENCY_RESOLUTION)
    assert not h.matches(_cluster(dep, kind=ErrorKind.DEPENDENCY_RESOLUTION), None, rcfg)
    assert not h.matches(_cluster(_err(sig="compile|incompatible_types|Foo")), None, rcfg)


def test_no_match_when_catalog_module_missing(fake_kit, monkeypatch, rcfg):
    monkeypatch.setitem(sys.modules, "sbmig.kb.catalog", None)        # import → ImportError
    cr.clear_catalog_cache()
    assert not _handler(fake_kit).matches(_cluster(_err()), None, rcfg)


def test_no_match_without_catalog_dir_or_on_catalog_error(fake_kit, catalog, rcfg, tmp_path):
    _case()
    h, cl = _handler(fake_kit), _cluster(_err())
    catalog.fail = True
    assert not h.matches(cl, None, rcfg)                               # never raises
    catalog.fail = False
    rcfg.kb.dir = str(tmp_path / "empty-kb")                           # no catalog/ yet: the KB is not touched
    n = len(catalog.instances)
    assert not h.matches(cl, None, rcfg)
    assert len(catalog.instances) == n


def test_catalog_instance_cached_per_kb_dir(fake_kit, catalog, rcfg):
    _case()
    h, cl = _handler(fake_kit), _cluster(_err())
    for _ in range(3):
        assert h.matches(cl, None, rcfg)
    assert len(catalog.instances) == 1
    assert catalog.instances[0].kb_dir == Path(rcfg.kb.dir)


# ---------------------------------------------------------------------------------------------------------------------
# applying
# ---------------------------------------------------------------------------------------------------------------------
def test_error_lines_scope_reported_line_and_imports_only(fake_kit, catalog, rcfg):
    _case()
    p = _write(fake_kit.root, FILTER, SRC)
    h = _handler(fake_kit)
    changes = h.apply(fake_kit.ws, _cluster(_err(line=6)), make_profile(), rcfg)
    assert [(c.path, c.op) for c in changes] == [(FILTER, "modify")]
    assert p.read_text(encoding="utf-8") == SRC.replace(
        "LegacyRequestHelper.clientIp;", "LegacyRequestHelper.remoteClientIp;").replace(
        "a(Req r) { return clientIp(r)", "a(Req r) { return remoteClientIp(r)")
    assert "String b(Req r) { return clientIp(r); }" in p.read_text(encoding="utf-8")       # line 7 untouched
    assert _plain(h.notes) == [("case a1b2c3d4e5 (LegacyRequestHelper.clientIp — cannot find symbol): replaced "
                        "clientIp→remoteClientIp on 2 line(s) of ClientIpFilter.java")]
    assert h.flags == []


def test_file_scope(fake_kit, catalog, rcfg):
    _case(name="")
    rcfg.kb.case_replace_scope = "file"
    p = _write(fake_kit.root, FILTER, SRC)
    h = _handler(fake_kit)
    assert [c.path for c in h.apply(fake_kit.ws, _cluster(_err(line=6)), None, rcfg)] == [FILTER]
    assert p.read_text(encoding="utf-8") == SRC.replace("clientIp(", "remoteClientIp(").replace(
        ".clientIp;", ".remoteClientIp;")
    assert _plain(h.notes) == [("case a1b2c3d4e5: replaced clientIp→remoteClientIp on 3 line(s) of ClientIpFilter.java "
                        "[scope: file]")]


def test_identifier_boundaries(fake_kit, catalog, rcfg):
    _case()
    line = "        String s = clientIp + clientIpCache + getClientIp() + r.clientIp() + $clientIp + clientIp_x;\n"
    p = _write(fake_kit.root, FILTER, "class ClientIpFilter {\n" + line + "}\n")
    _handler(fake_kit).apply(fake_kit.ws, _cluster(_err(line=2)), None, rcfg)
    assert p.read_text(encoding="utf-8").splitlines()[1] == (
        "        String s = remoteClientIp + clientIpCache + getClientIp() + r.remoteClientIp() + $clientIp + clientIp_x;")


def test_non_identifier_pair_on_import_line(fake_kit, catalog, rcfg):
    sig = "compile|package_does_not_exist|javax.servlet"
    _case(sig=sig, cid="ffee001122", name="javax.servlet — package does not exist",
          action=_replace(("javax.servlet", "jakarta.servlet")))
    src = ("package com.acme;\n"
           "import javax.servlet.http.HttpServletRequest;\n"
           "import javax.servletx.Other;\n"
           "class A {\n"
           "    // see javax.servlet docs\n"
           "    HttpServletRequest r;\n"
           "}\n")
    p = _write(fake_kit.root, "src/main/java/com/acme/A.java", src)
    h = _handler(fake_kit)
    changes = h.apply(fake_kit.ws, _cluster(_err(line=6, file="src/main/java/com/acme/A.java", sig=sig)), None, rcfg)
    assert [c.path for c in changes] == ["src/main/java/com/acme/A.java"]
    assert p.read_text(encoding="utf-8") == src.replace("import javax.servlet.http.", "import jakarta.servlet.http.")
    assert _plain(h.notes) == [("case ffee001122 (javax.servlet — package does not exist): replaced "
                        "javax.servlet→jakarta.servlet on 1 line(s) of A.java")]


def test_literal_pair_bounded_at_identifier_ends(fake_kit, catalog, rcfg):
    _case(action=_replace(("1.0.0", "3.0.0")))
    p = _write(fake_kit.root, FILTER, 'class F {\n    String v = "1.0.0" + "11.0.0" + "1.0.0x";\n}\n')
    _handler(fake_kit).apply(fake_kit.ws, _cluster(_err(line=2)), None, rcfg)
    assert p.read_text(encoding="utf-8").splitlines()[1] == '    String v = "3.0.0" + "11.0.0" + "1.0.0x";'


def test_pairs_applied_in_one_pass_without_chaining(fake_kit, catalog, rcfg):
    _case(action=_replace(("clientIp", "remoteIp"), ("remoteIp", "peerIp")))
    p = _write(fake_kit.root, FILTER, "class F {\n    String s = clientIp + remoteIp;\n}\n")
    h = _handler(fake_kit)
    h.apply(fake_kit.ws, _cluster(_err(line=2)), None, rcfg)
    assert p.read_text(encoding="utf-8").splitlines()[1] == "    String s = remoteIp + peerIp;"
    assert "replaced clientIp→remoteIp, remoteIp→peerIp on 1 line(s)" in h.notes[0]


def test_each_case_only_on_its_own_lines(fake_kit, catalog, rcfg):
    other = "compile|cannot_find_symbol|userId"
    _case()
    _case(sig=other, cid="0000000002", name="", action=_replace(("userId", "memberId")))
    src = "class F {\n    String a = clientIp + userId;\n    String b = clientIp + userId;\n}\n"
    p = _write(fake_kit.root, FILTER, src)
    h = _handler(fake_kit)
    changes = h.apply(fake_kit.ws, _cluster(_err(line=2), _err(line=3, sig=other)), None, rcfg)
    assert [c.path for c in changes] == [FILTER]                       # one FileChange per file
    assert p.read_text(encoding="utf-8").splitlines()[1:3] == ["    String a = remoteClientIp + userId;",
                                                               "    String b = clientIp + memberId;"]
    notes = _plain(h.notes)
    assert len(notes) == 2 and notes[1].startswith("case 0000000002: replaced userId→memberId on 1 line(s)")
    assert [n for n in h.notes if n.startswith("resolver: ")] == [
        f"resolver: case-replace:a1b2c3d4e5:{cr.pairset_hash([('clientIp', 'remoteClientIp')])}",
        f"resolver: case-replace:0000000002:{cr.pairset_hash([('userId', 'memberId')])}"]


def test_crlf_and_cp949_preserved(fake_kit, catalog, rcfg):
    _case()
    crlf_rel, legacy_rel = "src/main/java/com/acme/A.java", "src/main/java/com/acme/B.java"
    crlf = _write(fake_kit.root, crlf_rel, b"class A {\r\n    String s = clientIp;\r\n}\r\n")
    legacy = _write(fake_kit.root, legacy_rel,
                    "class B {\n    // 한글 주석\n    String s = clientIp + \"가나다\";\n}\n".encode("cp949"))
    changes = _handler(fake_kit).apply(fake_kit.ws, _cluster(_err(line=2, file=crlf_rel), _err(line=3, file=legacy_rel)),
                                       None, rcfg)
    assert sorted(c.path for c in changes) == [crlf_rel, legacy_rel]
    assert crlf.read_bytes() == b"class A {\r\n    String s = remoteClientIp;\r\n}\r\n"
    assert legacy.read_bytes() == ("class B {\n    // 한글 주석\n    String s = remoteClientIp + \"가나다\";\n}\n"
                                   .encode("cp949"))


def test_absolute_error_path(fake_kit, catalog, rcfg):
    _case()
    p = _write(fake_kit.root, FILTER, SRC)
    changes = _handler(fake_kit).apply(fake_kit.ws, _cluster(_err(line=6, file=str(p.resolve()))), None, rcfg)
    assert [c.path for c in changes] == [FILTER]


def test_nothing_to_change_returns_empty(fake_kit, catalog, rcfg):
    _case(action=_replace(("serverIp", "hostIp")))
    p = _write(fake_kit.root, FILTER, SRC)
    h = _handler(fake_kit)
    assert h.matches(_cluster(_err(line=6)), None, rcfg)
    assert h.apply(fake_kit.ws, _cluster(_err(line=6)), None, rcfg) == []
    assert h.notes == [] and p.read_text(encoding="utf-8") == SRC
    # a reported file that does not exist (generated / outside the workspace) is skipped
    assert h.apply(fake_kit.ws, _cluster(_err(line=6, file="src/main/java/Missing.java")), None, rcfg) == []


def test_preemptive_returns_empty(fake_kit, catalog, rcfg):
    _case()
    _write(fake_kit.root, FILTER, SRC)
    h = _handler(fake_kit)
    assert h.apply(fake_kit.ws, None, make_profile(), rcfg) == []
    assert h.notes == []


# ---------------------------------------------------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------------------------------------------------
def test_registry_lists_case_replace_last():
    ids = HandlerRegistry.default(None).ids()
    assert ids[-1] == "case-replace" and ids.count("case-replace") == 1


def test_specific_handler_wins(fake_kit, catalog, rcfg):
    sig = "compile|package_does_not_exist|javax.persistence"
    _case(sig=sig, action=_replace(("javax.persistence", "jakarta.persistence")))
    err = BuildError(kind=ErrorKind.COMPILE, message="package javax.persistence does not exist",
                     file="src/main/java/com/acme/Svc.java", line=3, raw="package javax.persistence does not exist",
                     signature_generic=sig)
    prof = make_profile()
    prof.source = SourceStats(javax_imports={"javax.persistence": 3}, packages=["com.acme"], base_package="com.acme")
    ids = [h.id for h in fake_kit.registry.matching(_cluster(err), prof, rcfg)]
    assert ids[0] == "jakarta-namespace-sweep" and ids[-1] == "case-replace"


# ---------------------------------------------------------------------------------------------------------------------
# round 10 (D17): resolver id per replace set, javax → jakarta package pairs never touch JDK packages
# ---------------------------------------------------------------------------------------------------------------------
def test_resolver_id_names_case_and_pairset(fake_kit, catalog, rcfg):
    _case()
    _write(fake_kit.root, FILTER, SRC)
    h = _handler(fake_kit)
    assert h.resolver_id == "case-replace"
    h.apply(fake_kit.ws, _cluster(_err(line=6)), make_profile(), rcfg)
    expected = cr.pairset_hash([("clientIp", "remoteClientIp")])
    assert re.fullmatch(r"[0-9a-f]{8}", expected)
    assert h.resolver_id == f"case-replace:a1b2c3d4e5:{expected}"
    assert h.notes[-1] == f"resolver: case-replace:a1b2c3d4e5:{expected}"       # read by the error tracker
    # nothing applied → plain handler id again (no stale resolver from the previous apply)
    assert h.apply(fake_kit.ws, None, make_profile(), rcfg) == []
    assert h.resolver_id == "case-replace"


def test_pairset_hash_formula_is_order_and_duplicate_free():
    import hashlib

    a = cr.pairset_hash([("b", "c"), ("a", "b"), ("a", "b"), ("x", "x"), ("", "y")])
    want = hashlib.sha1("a\tb\nb\tc".encode("utf-8")).hexdigest()[:8]
    assert a == want == cr.pairset_hash([ReplacePair(old="a", new="b"), ReplacePair(old="b", new="c")])


JAXB_SRC = """package com.acme.web;

import javax.xml.bind.JAXBContext;
import javax.xml.bind.annotation.*;
import javax.xml.parsers.DocumentBuilderFactory;
import javax.sql.DataSource;
import javax.naming.InitialContext;

class Xml {
    JAXBContext c; DataSource d; DocumentBuilderFactory f; InitialContext i;
}
"""
JAXB_SIG = "compile|package_does_not_exist|javax.xml.bind"


def test_dotted_javax_pair_renames_ee_packages_only(fake_kit, catalog, rcfg):
    _case(sig=JAXB_SIG, cid="jaxb000001", name="javax.xml.bind",
          action=_replace(("javax.xml.bind", "jakarta.xml.bind")))
    p = _write(fake_kit.root, FILTER, JAXB_SRC)
    h = _handler(fake_kit)
    c = _cluster(_err(line=3, sig=JAXB_SIG))
    assert h.matches(c, make_profile(), rcfg)
    assert [x.path for x in h.apply(fake_kit.ws, c, make_profile(), rcfg)] == [FILTER]
    text = p.read_text(encoding="utf-8")
    assert "import jakarta.xml.bind.JAXBContext;" in text and "import jakarta.xml.bind.annotation.*;" in text
    assert "import javax.xml.parsers.DocumentBuilderFactory;" in text and "import javax.sql.DataSource;" in text
    assert "import javax.naming.InitialContext;" in text


def test_broad_learned_javax_pair_never_touches_jdk_packages(fake_kit, catalog, rcfg):
    _case(sig=JAXB_SIG, cid="jaxb000002", name="javax.xml", action=_replace(("javax.xml", "jakarta.xml")))
    p = _write(fake_kit.root, FILTER, JAXB_SRC)
    h = _handler(fake_kit)
    h.apply(fake_kit.ws, _cluster(_err(line=3, sig=JAXB_SIG)), make_profile(), rcfg)
    text = p.read_text(encoding="utf-8")
    assert "import jakarta.xml.bind.JAXBContext;" in text             # under the jakarta table: renamed
    assert "import javax.xml.parsers.DocumentBuilderFactory;" in text  # JDK: kept whatever was learned
    assert "import javax.sql.DataSource;" in text


SWING_SRC = """package com.acme.web;

import javax.persistence.Query;
import javax.swing.tree.DefaultMutableTreeNode;
import javax.swing.tree.TreeNode;
import javax.sql.DataSource;

class Menu {
    Query q; DefaultMutableTreeNode n; TreeNode t; DataSource d;
}
"""
PERSIST_SIG = "compile|package_does_not_exist|javax.persistence"


@pytest.mark.parametrize("pair", [("javax", "jakarta"), ("javax.", "jakarta.")])
def test_bare_learned_javax_pair_never_touches_swing(fake_kit, catalog, rcfg, pair):
    """2026-10-26 (company run after round 10): a KB written by the old tokenizer learned the bare token pair
    `javax` → `jakarta`; applied to the import lines of a file with a jakarta error it turned
    `javax.swing.tree.DefaultMutableTreeNode` into `jakarta.swing.tree…` (cannot find symbol). The guard covers it."""
    _case(sig=PERSIST_SIG, cid="pers000001", name="javax.persistence", action=_replace(pair))
    p = _write(fake_kit.root, FILTER, SWING_SRC)
    h = _handler(fake_kit)
    h.apply(fake_kit.ws, _cluster(_err(line=3, sig=PERSIST_SIG)), make_profile(), rcfg)
    text = p.read_text(encoding="utf-8")
    assert "import jakarta.persistence.Query;" in text
    assert "import javax.swing.tree.DefaultMutableTreeNode;" in text and "import javax.swing.tree.TreeNode;" in text
    assert "import javax.sql.DataSource;" in text
