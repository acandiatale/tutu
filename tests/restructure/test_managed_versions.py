"""Round 9 (D1): explicit dependency versions the new parent chain manages are dropped after the pom edits."""
from __future__ import annotations

from pathlib import Path

import pytest

from sbmig.core.config import Config
from sbmig.core.models import CaseType
from sbmig.restructure import PomEditor, Restructurer

from .helpers import HEAD, dep, java, make_ws, profile
from .test_restructurer import msa_files, msa_profile

JAXB_API = "jakarta.xml.bind:jakarta.xml.bind-api"
JAXB_RT = "org.glassfish.jaxb:jaxb-runtime"
SKIP_NOTE = "managed-version cleanup skipped: no effective pom"


def legacy_files() -> dict[str, str]:
    pom = (HEAD + "    <groupId>com.acme</groupId>\n    <artifactId>tool</artifactId>\n    <version>1</version>\n"
           "    <properties>\n        <jaxb.version>3.0.2</jaxb.version>\n    </properties>\n"
           "    <dependencyManagement>\n        <dependencies>\n"
           + dep("com.h2database", "h2", "1.4.200").replace("        ", "            ", 1)
           + "        </dependencies>\n    </dependencyManagement>\n"
           "    <dependencies>\n"
           + dep("jakarta.xml.bind", "jakarta.xml.bind-api", "3.0.1")
           + dep("org.glassfish.jaxb", "jaxb-runtime", "${jaxb.version}")
           + dep("com.fasterxml.jackson.core", "jackson-core", "2.99.0")              # newer than managed
           + dep("org.yaml", "snakeyaml", "${undefined.version}")                       # unresolvable
           + dep("com.google.guava", "guava", "20.0")                                   # optional (protected)
           + dep("org.springframework.boot", "spring-boot-starter-actuator", "2.7.0")   # mandatory (protected)
           + dep("com.oracle", "ojdbc", "11", "system",
                 "            <systemPath>${project.basedir}/lib/ojdbc.jar</systemPath>\n")
           + dep("org.apache.commons", "commons-text", "1.9", extra="            <classifier>sources</classifier>\n")
           + dep("org.weird", "odd", "release-x")                                        # unparsable
           + dep("org.slf4j", "jul-to-slf4j", "1.7.36", "test")
           + "    </dependencies>\n</project>\n")
    return {"pom.xml": pom, "src/main/java/com/acme/tool/Main.java": java("com.acme.tool", "Main"),
            "lib/ojdbc.jar": "jar"}


MANAGED = {JAXB_API: "4.0.2", JAXB_RT: "4.0.5", "com.fasterxml.jackson.core:jackson-core": "2.19.2",
           "org.yaml:snakeyaml": "2.4", "com.google.guava:guava": "33.0.0-jre", "com.oracle:ojdbc": "21",
           "org.springframework.boot:spring-boot-starter-actuator": "4.1.0", "org.apache.commons:commons-text": "1.13",
           "org.weird:odd": "1.0", "org.slf4j:jul-to-slf4j": "2.0.17", "com.h2database:h2": "2.3.232"}


def run(tmp_path: Path, cfg: Config, name: str, params: dict | None = None, mode: str = "gte"):
    cfg.layout.drop_managed_versions = mode  # type: ignore[assignment]
    ws = make_ws(tmp_path, legacy_files(), name)
    prof = profile(ws, CaseType.PURE_JAVA, artifact_id="tool", version="1", base_package="com.acme.tool")
    res = Restructurer().apply(ws, prof, cfg, mode="layout", params=params)
    assert res.ok, res.error
    return ws, res


def versions(ws, pom: str = "module/comp-jar/pom.xml") -> dict[str, str | None]:
    return {d.key: d.version for d in PomEditor(ws.root / pom).dependencies()}


def test_gte_drops_managed_versions_and_keeps_the_rest(tmp_path: Path, cfg: Config) -> None:
    calls: list[int] = []

    def effective():
        calls.append(1)
        return {"managed": MANAGED, "by_module": {}}
    ws, res = run(tmp_path, cfg, "gte", {"effective_pom": effective})
    assert calls == [1]
    v = versions(ws)
    assert v[JAXB_API] is None and v[JAXB_RT] is None                          # literal and ${prop} dropped
    assert v["org.slf4j:jul-to-slf4j"] is None                                 # test scope is fine too
    assert v["com.fasterxml.jackson.core:jackson-core"] == "2.99.0"            # deliberately newer pin stays
    assert v["org.yaml:snakeyaml"] == "${undefined.version}"                   # unresolvable → skip
    assert v["com.google.guava:guava"] == "33.4.8-jre"                         # optional bump, not dropped
    assert v["com.oracle:ojdbc"] == "11"                                       # system scope never
    assert v["org.apache.commons:commons-text"] == "1.9"                       # classifier: managed per type
    assert v["org.weird:odd"] == "release-x"                                   # unknown comparison → keep
    # v2.1o: a property only the dropped versions referenced is removed with them (round 9 left it alone)
    assert "jaxb.version" not in (ws.root / "pom.xml").read_text()
    assert res.version_properties_removed == ["pom.xml: jaxb.version=3.0.2"]
    assert sorted(res.managed_versions_dropped) == sorted([
        f"{JAXB_API}: 3.0.1 → managed 4.0.2 (module/comp-jar/pom.xml)",
        f"{JAXB_RT}: 3.0.2 → managed 4.0.5 (module/comp-jar/pom.xml)",
        "org.slf4j:jul-to-slf4j: 1.7.36 → managed 2.0.17 (module/comp-jar/pom.xml)"])
    assert any(n.startswith("3 explicit dependency version(s) dropped") and JAXB_API in n for n in res.notes)
    assert any("newer than the parent-managed one kept" in n and "jackson-core 2.99.0" in n for n in res.notes)
    root = PomEditor(ws.root / "pom.xml")
    assert {d.key: d.version for d in root.managed_dependencies()}.get("com.h2database:h2") == "1.4.200"
    # mandatory deps on the root (managed by the parent) and the boot-jar comp dependency are untouched
    assert versions(ws, "module/boot-jar/pom.xml")["${project.groupId}:tool-comp"] == "${project.version}"
    # second run on the already restructured tree: nothing left to drop
    res2 = Restructurer().apply(ws, profile(ws, CaseType.PURE_JAVA, artifact_id="tool", version="1"), cfg,
                                mode="layout", params={"effective_pom": effective})
    assert res2.ok and res2.managed_versions_dropped == [] and res2.changed_files == []


def test_any_drops_newer_pins_too(tmp_path: Path, cfg: Config) -> None:
    ws, res = run(tmp_path, cfg, "any", {"effective_pom": lambda: {"managed": MANAGED}}, mode="any")
    v = versions(ws)
    assert v["com.fasterxml.jackson.core:jackson-core"] is None and v["org.weird:odd"] is None
    assert v["com.oracle:ojdbc"] == "11" and v["com.google.guava:guava"] == "33.4.8-jre"
    assert len(res.managed_versions_dropped) == 5


@pytest.mark.parametrize("params", [None, {"effective_pom": lambda: None}, {"effective_pom": "not callable"},
                                    {"effective_pom": lambda: (_ for _ in ()).throw(RuntimeError("mvn"))}])
def test_no_effective_pom_skips_with_note(tmp_path: Path, cfg: Config, params) -> None:
    ws, res = run(tmp_path, cfg, "skip", params)
    assert SKIP_NOTE in res.notes and res.managed_versions_dropped == []
    assert versions(ws)[JAXB_API] == "3.0.1"


def test_off_does_nothing_and_never_calls(tmp_path: Path, cfg: Config) -> None:
    def boom():
        raise AssertionError("must not be called")
    ws, res = run(tmp_path, cfg, "off", {"effective_pom": boom}, mode="off")
    assert versions(ws)[JAXB_API] == "3.0.1" and res.managed_versions_dropped == []
    assert SKIP_NOTE not in res.notes


def test_msa_prefers_the_declaring_services_chain(tmp_path: Path, cfg: Config) -> None:
    files = msa_files()
    files["user-service/pom.xml"] = files["user-service/pom.xml"].replace(
        "    </dependencies>", dep("jakarta.xml.bind", "jakarta.xml.bind-api", "3.0.1") + "    </dependencies>")
    files["order-service/pom.xml"] = files["order-service/pom.xml"].replace(
        "    </dependencies>", dep("jakarta.xml.bind", "jakarta.xml.bind-api", "3.0.1") + "    </dependencies>")
    ws = make_ws(tmp_path, files, "msa-mv")
    data = {"managed": {JAXB_API: "4.0.2"},          # merged view (first wins) — must not leak into user-service
            "by_module": {"com.acme:order-service": {"managed": {JAXB_API: "4.0.2"}},
                          "com.acme:user-service": {"managed": {}}}}
    res = Restructurer().apply(ws, msa_profile(ws), cfg, mode="msa_pom", params={"effective_pom": lambda: data})
    assert res.ok, res.error
    assert versions(ws, "order-service/pom.xml")[JAXB_API] is None
    assert versions(ws, "user-service/pom.xml")[JAXB_API] == "3.0.1"
    assert res.managed_versions_dropped == [f"{JAXB_API}: 3.0.1 → managed 4.0.2 (order-service/pom.xml)"]
