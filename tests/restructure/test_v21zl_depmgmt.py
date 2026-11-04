"""v2.1z-l (company re-run 2026-11-04): a role-parent module inherits the as-is root's `<dependencyManagement>` and an own
entry wins over the Boot BOM the restructure imports — `spring-security-*:${org.springSecurity-version}` (4.2.7.RELEASE) shadowed
the BOM's 7.x and the as-is properties (`org.springSecurity-version`, `java-version`) survived because the entry referenced
them. The restructure now removes an own management entry whose g:a the chain / the imported BOM manages at an equal or newer
line (the properties die with it); a newer own pin and a BOM import itself stay."""
from __future__ import annotations

import re
from pathlib import Path

from sbmig.build import boot_bom
from sbmig.core.config import Config
from sbmig.core.models import PomInfo
from sbmig.restructure.pom_editor import PomEditor
from sbmig.restructure.restructurer import Restructurer

from .helpers import HEAD, CaseType, dep, java, make_ws, profile
from .test_v21o_bom_facts import BACK, BOOT_FACTS, SEC, TEXT, ccfg, facts  # noqa: F401 - fixtures re-exported

MGMT = (HEAD + "    <groupId>com.acme</groupId>\n    <artifactId>shop</artifactId>\n    <version>1.0.0</version>\n"
        "    <properties>\n        <java-version>1.8</java-version>\n"
        "        <org.springSecurity-version>4.2.7.RELEASE</org.springSecurity-version>\n"
        "        <text.version>1.13.0</text.version>\n    </properties>\n"
        "    <dependencyManagement>\n        <dependencies>\n"
        + dep("org.springframework.security", "spring-security-core", "${org.springSecurity-version}")
        + dep("org.apache.commons", "commons-text", "${text.version}")          # NEWER than the managed 1.12.0: stays
        + dep("com.acme", "acme-bom", "9", scope="import", extra="<type>pom</type>") +
        "        </dependencies>\n    </dependencyManagement>\n"
        "    <dependencies>\n" + dep("org.springframework.security", "spring-security-core")
        + dep("org.apache.commons", "commons-text") + "    </dependencies>\n</project>\n")


def _layout(tmp_path: Path, cfg: Config):
    ws = make_ws(tmp_path, {"pom.xml": MGMT, "src/main/java/com/acme/shop/Main.java": java("com.acme.shop", "Main")}, "mgmt")
    prof = profile(ws, CaseType.SPRING_LEGACY, artifact_id="shop", version="1.0.0", base_package="com.acme.shop")
    prof.poms = [PomInfo(path="pom.xml", artifact_id="shop", group_id="com.acme",
                         dependencies=PomEditor(ws.root / "pom.xml").dependencies())]
    res = Restructurer().apply(ws, prof, cfg, mode="layout",
                               params={"asis_effective": lambda: {"managed": {}, "direct": {}, "by_module": {}},
                                       "effective_pom": lambda: None})
    assert res.ok, res.error
    return ws, res


def _managed(ws, pom: str = BACK) -> dict[str, str | None]:
    ed = PomEditor(ws.root / pom)
    return {ed.gav_of(el).key: ed.gav_of(el).version for el in ed.dependency_elements(True)}


def test_an_own_management_entry_the_imported_bom_manages_is_removed(tmp_path: Path, ccfg: Config, facts, monkeypatch):
    monkeypatch.setattr(boot_bom, "load_boot_bom_facts", lambda cfg, version=None: BOOT_FACTS)
    ccfg.artifacts.parent_manages_spring_boot = False
    ws, res = _layout(tmp_path, ccfg)
    managed = _managed(ws)
    assert SEC not in managed                                            # 4.2.7 would have shadowed the BOM's 7.1.0
    assert managed.get(TEXT) == "${text.version}"                        # newer than managed → the owners' pin stays
    assert managed.get("com.acme:acme-bom") == "9"                       # a BOM import is never touched
    assert "org.springframework.boot:spring-boot-dependencies" in managed  # the restructure's own import
    assert any(d.startswith(f"{SEC}: 4.2.7.RELEASE → managed 7.1.0 ({BACK}, dependencyManagement)")
               for d in res.managed_versions_dropped), res.managed_versions_dropped
    props = PomEditor(ws.root / BACK).properties()
    assert "org.springSecurity-version" not in props                     # its last reference went with the entry
    assert "java-version" not in props                                    # orphan (nothing references it)
    assert props.get("text.version") == "1.13.0"
    # the root's management block was copied into shop-back AND shop-boot (role parents): both copies go
    assert any(re.match(r"\d+ as-is dependencyManagement entr\(ies\) removed", n) for n in res.notes), res.notes
    assert any(d.endswith("(shop-boot/pom.xml, dependencyManagement)") for d in res.managed_versions_dropped)
    deps = {d.key: d.version for d in PomEditor(ws.root / BACK).dependencies()}
    assert deps[SEC] is None                                             # still declared, version from the BOM


def test_the_switch_off_keeps_the_management_entries(tmp_path: Path, ccfg: Config, facts, monkeypatch):
    monkeypatch.setattr(boot_bom, "load_boot_bom_facts", lambda cfg, version=None: BOOT_FACTS)
    ccfg.artifacts.parent_manages_spring_boot = False
    ccfg.layout.drop_managed_versions = "off"
    ws, _res = _layout(tmp_path, ccfg)
    assert _managed(ws).get(SEC) == "${org.springSecurity-version}"
