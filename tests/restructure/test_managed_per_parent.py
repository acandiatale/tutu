"""Review T2: without the post-restructure capture, the managed versions come from the company parent each pom really
inherits (root pom → base-parent, a module → its role parent), not from one set merged over all three parents: a
version is neither stripped because another parent manages it nor compared against another parent's line; the Spring
Cloud BOM decision looks at the pom's own parent too."""
from __future__ import annotations

from pathlib import Path

import pytest

from sbmig.build import parent_chain
from sbmig.core.config import Config

from .test_v21o_bom_facts import BACK, BASE, CTX, SEC, TEXT, _layout, _versions, ccfg  # noqa: F401

BOOTJ = "com.company.platform:boot-jar:4.0.0"
COMPJ = "com.company.platform:comp-jar:4.0.0"


def _facts(comp: dict[str, str], boot: dict[str, str]) -> parent_chain.ParentChainFacts:
    merged = {**comp, **boot}
    return parent_chain.ParentChainFacts(roots=[BASE, BOOTJ, COMPJ], managed=dict(merged),
                                         root_managed={BASE: {}, BOOTJ: dict(boot), COMPJ: dict(comp)},
                                         properties={"x.y": "1"}, sources=["test facts"], poms=3)


@pytest.fixture()
def per_parent(monkeypatch):
    holder: dict[str, parent_chain.ParentChainFacts] = {}
    monkeypatch.setattr(parent_chain, "load_parent_chain_facts", lambda cfg: holder["f"])
    return holder


def test_a_version_only_another_parent_manages_is_kept(tmp_path: Path, ccfg: Config, per_parent) -> None:
    # boot-jar manages spring-context, comp-jar does not: the back module (parent comp-jar) keeps its version
    per_parent["f"] = _facts(comp={SEC: "7.1.0", TEXT: "1.12.0"}, boot={CTX: "7.0.8"})
    ws, res, _p, _pa = _layout(tmp_path, ccfg, name="perparent")
    from sbmig.restructure import PomEditor
    assert PomEditor(ws.root / BACK).get_parent().artifact_id == "comp-jar"
    v = _versions(ws)
    assert v[CTX] is not None                                       # not stripped: comp-jar does not manage it
    assert v[SEC] is None and f"{SEC}: 5.7.11 → managed 7.1.0 ({BACK})" in res.managed_versions_dropped
    assert not any(d.startswith(CTX) for d in res.managed_versions_dropped)


def test_the_cloud_bom_decision_looks_at_the_poms_own_parent(tmp_path: Path, ccfg: Config, per_parent) -> None:
    from types import SimpleNamespace

    from sbmig.restructure import PomEditor, Restructurer
    from sbmig.restructure.restructurer import _Ctx

    from .helpers import make_ws
    from .test_v21o_bom_facts import _legacy_files

    cloud = "org.springframework.cloud:spring-cloud-commons"
    per_parent["f"] = _facts(comp={}, boot={cloud: "5.0.0"})
    ws = make_ws(tmp_path, _legacy_files(), "cloud")
    prof = SimpleNamespace(libraries=[SimpleNamespace(name="spring-cloud")])
    c = _Ctx(ws, prof, ccfg, SimpleNamespace(notes=[], manual_followups=[], bom_imports=[]), {}, False)
    ed = PomEditor(ws.root / "pom.xml")
    ed.set_parent(__import__("sbmig.core.models", fromlist=["GAV"]).GAV(
        group_id="com.company.platform", artifact_id="comp-jar", version="4.0.0"))
    r = Restructurer()
    r._cloud_bom(c, ed, "pom.xml")
    assert ed.has_managed_dependency("org.springframework.cloud", "spring-cloud-dependencies")
