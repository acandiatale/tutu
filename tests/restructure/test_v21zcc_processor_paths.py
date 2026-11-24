"""v2.1z-cc: a maven-compiler-plugin annotation processor `<path>` without a `<version>` gets one in the restructure,
right after the dependency versions are settled (`_drop_managed_versions`): managed → left; the pom's own dependency;
what the dependency step would write (Boot BOM unless the as-is build is newer, else as-is); the project's other
poms' one literal version; else unknown + a note. Offline: both captures are plain dicts, the parent chain / Boot BOM
facts are monkeypatched (empty by default, tests/conftest.py)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sbmig.build import boot_bom, parent_chain
from sbmig.core.config import Config
from sbmig.core.models import PomInfo
from sbmig.restructure import PomEditor, Restructurer

from .helpers import HEAD, CaseType, dep, java, make_ws, profile
from .test_company_layout import _roles

LOMBOK = "org.projectlombok:lombok"
MSP = "org.mapstruct:mapstruct-processor"
GUAVA = "com.google.guava:guava"            # an optional in-house artifact of the e2e config (protected)
BOOT2_PARENT = "org.springframework.boot:spring-boot-starter-parent:2.7.18"
IN_REPO = "com.acme:shop:3.0.0"
CORE, API, BOOTM = "shop-core/pom.xml", "shop-api/pom.xml", "shop-boot/pom.xml"
SEC = "annotationProcessorPaths"
BOOTJ = "com.company.platform:boot-jar:4.0.0"
COMPJ = "com.company.platform:comp-jar:4.0.0"
BASE = "com.company.platform:base-parent:4.0.0"


@pytest.fixture()
def ccfg(cfg: Config) -> Config:
    """The round-9c company layout with role parents (as `test_v21l_pin_versions.ccfg`)."""
    L = cfg.layout
    L.module_dir, L.boot_module = "", "{name}-boot"
    L.comp_module, L.front_module = "{name}-back", "{name}-front"
    L.boot_as_is = "in-place"
    L.comp_artifact_suffix = "-back"
    # the e2e config lists lombok as an optional in-house artifact (protected, bumped); a company's lombok is not
    cfg.artifacts.optional = [s for s in cfg.artifacts.optional if s.artifact_id != "lombok"]
    return _roles(cfg)


# ------------------------------------------------------------------------------------------------ builders
def path(g: str, a: str, v: str | None = None, extra: str = "", ind: str = " " * 24) -> str:
    inner = ind + "    "
    out = f"{ind}<path>\n{inner}<groupId>{g}</groupId>\n{inner}<artifactId>{a}</artifactId>\n"
    if v:
        out += f"{inner}<version>{v}</version>\n"
    return out + extra + f"{ind}</path>\n"


def ga(key: str, v: str | None = None, extra: str = "", ind: str = " " * 24) -> str:
    g, a = key.split(":")
    return path(g, a, v, extra, ind)


def compiler(paths: str) -> str:
    """A `<build><plugins>` maven-compiler-plugin with the given `<path>` lines."""
    return ("    <build>\n        <plugins>\n            <plugin>\n"
            "                <groupId>org.apache.maven.plugins</groupId>\n"
            "                <artifactId>maven-compiler-plugin</artifactId>\n"
            "                <configuration>\n                    <annotationProcessorPaths>\n"
            + paths +
            "                    </annotationProcessorPaths>\n                </configuration>\n"
            "            </plugin>\n        </plugins>\n    </build>\n")


def _root() -> str:
    return (HEAD + "    <parent>\n        <groupId>org.springframework.boot</groupId>\n"
            "        <artifactId>spring-boot-starter-parent</artifactId>\n        <version>2.7.18</version>\n"
            "        <relativePath/>\n    </parent>\n"
            "    <groupId>com.acme</groupId>\n    <artifactId>shop</artifactId>\n    <version>3.0.0</version>\n"
            "    <packaging>pom</packaging>\n    <modules>\n        <module>shop-boot</module>\n"
            "        <module>shop-core</module>\n        <module>shop-api</module>\n    </modules>\n"
            "</project>\n")


def child(aid: str, deps: str = "", build: str = "", head: str = "", tail: str = "") -> str:
    return (HEAD + "    <parent>\n        <groupId>com.acme</groupId>\n        <artifactId>shop</artifactId>\n"
            "        <version>3.0.0</version>\n    </parent>\n"
            f"    <artifactId>{aid}</artifactId>\n{head}"
            + (f"    <dependencies>\n{deps}    </dependencies>\n" if deps else "")
            + build + tail + "</project>\n")


def files(core: str | None = None, api: str | None = None, boot: str | None = None) -> dict[str, str]:
    return {
        "pom.xml": _root(),
        BOOTM: boot if boot is not None else child("shop-boot", dep("org.springframework.boot",
                                                                    "spring-boot-starter-web")),
        "shop-boot/src/main/java/com/acme/shop/ShopApplication.java": java(
            "com.acme.shop", "ShopApplication", "@SpringBootApplication"),
        CORE: core if core is not None else child("shop-core"),
        "shop-core/src/main/java/com/acme/shop/core/Repo.java": java("com.acme.shop.core", "Repo"),
        API: api if api is not None else child("shop-api"),
        "shop-api/src/main/java/com/acme/shop/api/Api.java": java("com.acme.shop.api", "Api"),
    }


def asis(managed: dict[str, str] | None = None, direct: dict[str, dict[str, str]] | None = None) -> dict:
    """The as-is capture: the Boot 2 parent manages `managed` for every module (in-repo parent followed)."""
    m = dict(managed or {})
    mods = {f"com.acme:{a}": {"managed": m, "parent": IN_REPO, "direct": dict((direct or {}).get(a, {}))}
            for a in ("shop-boot", "shop-core", "shop-api")}
    mods["com.acme:shop"] = {"managed": m, "direct": {}, "parent": BOOT2_PARENT}
    return {"managed": m, "direct": {}, "plugins": {}, "properties": {}, "parent": BOOT2_PARENT,
            "modules": list(mods), "by_module": mods}


def _inventory(ws, prof):
    poms = []
    for f in sorted(ws.root.rglob("pom.xml")):
        if ".git" in f.parts:
            continue
        ed = PomEditor(f)
        poms.append(PomInfo(path=f.relative_to(ws.root).as_posix(), artifact_id=ed.artifact_id() or "?",
                            group_id=ed.effective_group_id(), dependencies=ed.dependencies()))
    prof.poms = poms
    return prof


def _profile(ws):
    p = profile(ws, CaseType.SPRING_BOOT, artifact_id="shop", version="3.0.0", packaging="pom",
                base_package="com.acme.shop", main_classes=["com.acme.shop.ShopApplication"])
    p.is_multi_module = True
    p.boot_modules = ["shop-boot"]
    return p


def run(tmp_path: Path, ccfg: Config, fs: dict[str, str], *, capture: dict | None = None,
        after: dict | None | str = "empty", name: str = "proc"):
    ws = make_ws(tmp_path, fs, name)
    eff = (lambda: {"managed": {"org.springframework:spring-core": "7.0.1"}, "by_module": {}}) \
        if after == "empty" else (lambda: after)
    params = {"asis_effective": lambda: capture if capture is not None else asis(), "effective_pom": eff}
    res = Restructurer().apply(ws, _inventory(ws, _profile(ws)), ccfg, mode="boot_pom", params=params)
    assert res.ok, res.error
    return ws, res, params


def paths_of(ws, pom: str) -> dict[str, str | None]:
    ed = PomEditor(ws.root / pom)
    return {r.key: r.version for r in ed.artifact_refs() if r.section == "annotationProcessorPath"}


def proc_lines(res) -> list[str]:
    return [x for x in res.versions_pinned if f"({SEC}, " in x]


def proc_notes(res) -> list[str]:
    return [n for n in res.notes if "annotation processor path" in n]


def _events(ws) -> list[dict]:
    if not ws.journal_path.is_file():
        return []
    lines = ws.journal_path.read_text(encoding="utf-8").splitlines()
    return [e for e in (json.loads(x) for x in lines if x.strip())
            if e.get("event") == "versions_pinned" and e.get("section") == SEC]


def again(ws, ccfg: Config, params: dict):
    before = {p: (ws.root / p).read_bytes() for p in ("pom.xml", CORE, API, BOOTM)}
    res2 = Restructurer().apply(ws, _inventory(ws, _profile(ws)), ccfg, mode="boot_pom", params=params)
    assert res2.ok, res2.error
    assert res2.changed_files == [] and proc_lines(res2) == []
    assert {p: (ws.root / p).read_bytes() for p in before} == before
    return res2


# --------------------------------------------------------------------------------------------- step 1: managed
def test_managed_in_the_poms_own_dependency_management_is_left(tmp_path: Path, ccfg: Config) -> None:
    mgmt = ("    <dependencyManagement>\n        <dependencies>\n"
            + dep("org.mapstruct", "mapstruct-processor", "1.5.5.Final").replace("        <", "            <")
            + "        </dependencies>\n    </dependencyManagement>\n")
    ws, res, _ = run(tmp_path, ccfg, files(core=child("shop-core", build=compiler(ga(MSP)), head=mgmt)))
    assert paths_of(ws, CORE) == {MSP: None}
    assert proc_lines(res) == [] and proc_notes(res) == []


def test_managed_by_the_company_chain_in_the_effective_pom_is_left(tmp_path: Path, ccfg: Config) -> None:
    after = {"managed": {}, "by_module": {"com.acme:shop-core": {"managed": {LOMBOK: "1.18.42"}},
                                          "com.acme:shop-api": {"managed": {}}}}
    fs = files(core=child("shop-core", build=compiler(ga(LOMBOK))), api=child("shop-api", build=compiler(ga(LOMBOK))))
    ws, res, _ = run(tmp_path, ccfg, fs, capture=asis({LOMBOK: "1.18.30"}), after=after)
    assert paths_of(ws, CORE) == {LOMBOK: None}              # the chain manages it for this module
    assert paths_of(ws, API) == {LOMBOK: "1.18.30"}          # not for this one → the as-is version
    assert proc_lines(res) == [f"{LOMBOK}: 1.18.30 ← as-is managed by {BOOT2_PARENT} ({SEC}, {API})"]


def test_managed_per_company_parent_without_the_effective_pom(tmp_path: Path, ccfg: Config, monkeypatch) -> None:
    # comp-jar manages lombok, boot-jar does not: judged per parent, never on the merged set
    facts = parent_chain.ParentChainFacts(roots=[BASE, BOOTJ, COMPJ], managed={LOMBOK: "1.18.42"},
                                          root_managed={BASE: {}, BOOTJ: {}, COMPJ: {LOMBOK: "1.18.42"}},
                                          properties={"x.y": "1"}, sources=["test facts"], poms=3)
    monkeypatch.setattr(parent_chain, "load_parent_chain_facts", lambda cfg: facts)
    fs = files(core=child("shop-core", build=compiler(ga(LOMBOK))),
               boot=child("shop-boot", dep("org.springframework.boot", "spring-boot-starter-web"),
                          build=compiler(ga(LOMBOK))))
    ws, res, _ = run(tmp_path, ccfg, fs, capture=asis({LOMBOK: "1.18.30"}), after=None)
    assert PomEditor(ws.root / CORE).get_parent().artifact_id == "comp-jar"
    assert PomEditor(ws.root / BOOTM).get_parent().artifact_id == "boot-jar"
    assert paths_of(ws, CORE) == {LOMBOK: None}
    assert paths_of(ws, BOOTM) == {LOMBOK: "1.18.30"}
    assert proc_lines(res) == [f"{LOMBOK}: 1.18.30 ← as-is managed by {BOOT2_PARENT} ({SEC}, {BOOTM})"]


# ------------------------------------------------------------------------------------- step 2: own dependency
def test_the_poms_own_dependency_literal(tmp_path: Path, ccfg: Config) -> None:
    fs = files(core=child("shop-core", dep("org.projectlombok", "lombok", "1.18.36", scope="provided"),
                          build=compiler(ga(LOMBOK))))
    ws, res, _ = run(tmp_path, ccfg, fs, capture=asis({LOMBOK: "1.18.30"}))
    assert paths_of(ws, CORE) == {LOMBOK: "1.18.36"}         # the dependency wins over the as-is version
    assert proc_lines(res) == [f"{LOMBOK}: 1.18.36 ← the pom's own dependency ({SEC}, {CORE})"]


def test_the_poms_own_dependency_property(tmp_path: Path, ccfg: Config) -> None:
    props = "    <properties>\n        <lombok.version>1.18.36</lombok.version>\n    </properties>\n"
    fs = files(core=child("shop-core", dep("org.projectlombok", "lombok", "${lombok.version}", scope="provided"),
                          build=compiler(ga(LOMBOK)), head=props))
    ws, res, _ = run(tmp_path, ccfg, fs, capture=asis({LOMBOK: "1.18.30"}))
    assert paths_of(ws, CORE) == {LOMBOK: "${lombok.version}"}
    assert proc_lines(res) == [f"{LOMBOK}: ${{lombok.version}} ← the pom's own dependency ({SEC}, {CORE})"]


# ----------------------------------------------------------------------------- step 3: the dependency step's
def _boot_facts(monkeypatch, managed: dict[str, str]) -> None:
    facts = boot_bom.BootBomFacts(version="4.1.0", managed=dict(managed), sources=["test Boot BOM"])
    monkeypatch.setattr(boot_bom, "load_boot_bom_facts", lambda cfg, version=None: facts)


def test_the_boot_bom_version(tmp_path: Path, ccfg: Config, monkeypatch) -> None:
    _boot_facts(monkeypatch, {LOMBOK: "1.18.42"})
    ws, res, _ = run(tmp_path, ccfg, files(core=child("shop-core", build=compiler(ga(LOMBOK)))),
                     capture=asis({LOMBOK: "1.18.30"}))
    assert paths_of(ws, CORE) == {LOMBOK: "1.18.42"}
    assert proc_lines(res) == [
        f"{LOMBOK}: 1.18.42 ← Boot 4.1.0 BOM (not managed by the parent chain) ({SEC}, {CORE})"]


def test_an_as_is_version_newer_than_the_boot_bom(tmp_path: Path, ccfg: Config, monkeypatch) -> None:
    _boot_facts(monkeypatch, {LOMBOK: "1.18.42"})
    ws, res, _ = run(tmp_path, ccfg, files(core=child("shop-core", build=compiler(ga(LOMBOK)))),
                     capture=asis({LOMBOK: "1.18.50"}))
    assert paths_of(ws, CORE) == {LOMBOK: "1.18.50"}
    assert proc_lines(res) == [f"{LOMBOK}: 1.18.50 ← as-is managed by {BOOT2_PARENT} ({SEC}, {CORE})"]


def test_the_as_is_version_only(tmp_path: Path, ccfg: Config) -> None:
    ws, res, _ = run(tmp_path, ccfg, files(core=child("shop-core", build=compiler(ga(LOMBOK)))),
                     capture=asis({LOMBOK: "1.18.30"}))
    assert paths_of(ws, CORE) == {LOMBOK: "1.18.30"}
    assert proc_lines(res) == [f"{LOMBOK}: 1.18.30 ← as-is managed by {BOOT2_PARENT} ({SEC}, {CORE})"]
    assert f"1 annotation processor path version(s) written: {LOMBOK} 1.18.30 ({CORE})" in res.notes


# -------------------------------------------------------------------------------------- step 4: other poms
def test_another_project_poms_single_version(tmp_path: Path, ccfg: Config) -> None:
    fs = files(core=child("shop-core", build=compiler(ga(MSP, "1.5.5.Final"))),
               api=child("shop-api", build=compiler(ga(MSP))),
               boot=child("shop-boot", dep("org.mapstruct", "mapstruct-processor", "1.5.5.Final", scope="provided")))
    ws, res, _ = run(tmp_path, ccfg, fs)
    assert paths_of(ws, CORE) == {MSP: "1.5.5.Final"}        # had a version: untouched
    assert paths_of(ws, API) == {MSP: "1.5.5.Final"}
    assert proc_lines(res) == [f"{MSP}: 1.5.5.Final ← the project's other poms ({BOOTM}, {CORE}) ({SEC}, {API})"]


def test_two_project_poms_that_disagree_leave_it_unknown(tmp_path: Path, ccfg: Config) -> None:
    fs = files(core=child("shop-core", build=compiler(ga(MSP, "1.5.5.Final"))),
               api=child("shop-api", build=compiler(ga(MSP))),
               boot=child("shop-boot", dep("org.mapstruct", "mapstruct-processor", "1.6.3", scope="provided")))
    ws, res, params = run(tmp_path, ccfg, fs)
    assert paths_of(ws, API) == {MSP: None}
    assert proc_lines(res) == []
    assert proc_notes(res) == [(f"annotation processor path {MSP} in {API} left without a version: the project's "
                               "other poms declare more than one version (1.5.5.Final, 1.6.3)")]
    assert _events(ws)[0]["unknown"] == [f"{MSP} ({API})"]
    again(ws, ccfg, params)


def test_a_property_version_elsewhere_is_not_one_literal(tmp_path: Path, ccfg: Config) -> None:
    props = "    <properties>\n        <mapstruct.version>1.5.5.Final</mapstruct.version>\n    </properties>\n"
    fs = files(core=child("shop-core", build=compiler(ga(MSP, "${mapstruct.version}")), head=props),
               api=child("shop-api", build=compiler(ga(MSP))))
    ws, res, _ = run(tmp_path, ccfg, fs)
    assert paths_of(ws, API) == {MSP: None}
    assert proc_notes(res) == [(f"annotation processor path {MSP} in {API} left without a version: the project's "
                               "other poms declare it only through a property (${mapstruct.version})")]


def test_nothing_known_is_one_note_and_the_pom_is_untouched(tmp_path: Path, ccfg: Config) -> None:
    ws, res, params = run(tmp_path, ccfg, files(api=child("shop-api", build=compiler(ga(MSP)))))
    assert paths_of(ws, API) == {MSP: None}
    assert proc_lines(res) == []
    assert proc_notes(res) == [(f"annotation processor path {MSP} in {API} left without a version: no project pom, "
                               "as-is build or Boot BOM names one; the build reports it")]
    again(ws, ccfg, params)


# ------------------------------------------------------------------------------------------------ not touched
def test_a_path_with_a_version_is_not_touched(tmp_path: Path, ccfg: Config) -> None:
    fs = files(core=child("shop-core", dep("org.projectlombok", "lombok", "1.18.36", scope="provided"),
                          build=compiler(ga(LOMBOK, "1.18.20"))))
    ws, res, _ = run(tmp_path, ccfg, fs, capture=asis({LOMBOK: "1.18.30"}))
    assert paths_of(ws, CORE) == {LOMBOK: "1.18.20"}         # differs from the dependency: the review's business
    assert proc_lines(res) == [] and proc_notes(res) == []


def test_a_classifier_path_takes_only_the_own_dependency(tmp_path: Path, ccfg: Config) -> None:
    cls_x = "                            <classifier>x</classifier>\n"
    cls_y = "                            <classifier>y</classifier>\n"
    fs = files(core=child("shop-core", dep("org.projectlombok", "lombok", "1.18.36", scope="provided",
                                           extra="            <classifier>x</classifier>\n"),
                          build=compiler(ga(LOMBOK, extra=cls_x) + ga(LOMBOK, extra=cls_y))))
    ws, res, _ = run(tmp_path, ccfg, fs, capture=asis({LOMBOK: "1.18.30"}))
    ed = PomEditor(ws.root / CORE)
    got = [(PomEditor.text_of(r.element, "classifier"), r.version) for r in ed.artifact_refs()
           if r.section == "annotationProcessorPath"]
    assert got == [("x", "1.18.36"), ("y", None)]           # y: the as-is capture keys g:a only
    assert proc_lines(res) == [f"{LOMBOK}: 1.18.36 ← the pom's own dependency ({SEC}, {CORE})"]
    assert any(n.startswith(f"annotation processor path {LOMBOK} (classifier y) in {CORE} left without a version: "
                            "the pom declares no dependency of the same classifier / type") for n in res.notes)


def test_protected_and_own_module_keys_are_not_touched(tmp_path: Path, ccfg: Config) -> None:
    fs = files(core=child("shop-core", build=compiler(ga("com.acme:shop-api") + ga(GUAVA))))
    ws, res, _ = run(tmp_path, ccfg, fs, capture=asis({GUAVA: "31.0-jre"}))
    assert paths_of(ws, CORE)["com.acme:shop-api"] is None
    assert proc_lines(res) == [] and proc_notes(res) == []


def test_another_plugins_processor_paths_are_not_touched(tmp_path: Path, ccfg: Config) -> None:
    other = compiler(ga(LOMBOK)).replace("<groupId>org.apache.maven.plugins</groupId>",
                                         "<groupId>org.example</groupId>")
    ws, res, _ = run(tmp_path, ccfg, files(core=child("shop-core", build=other)), capture=asis({LOMBOK: "1.18.30"}))
    assert paths_of(ws, CORE) == {LOMBOK: None}
    assert proc_lines(res) == []


# ----------------------------------------------------------------------- where the paths are; several modules
def test_plugin_management_a_profile_and_an_execution(tmp_path: Path, ccfg: Config) -> None:
    pm = compiler(ga(LOMBOK)).replace("        <plugins>\n", "        <pluginManagement>\n        <plugins>\n", 1) \
        .replace("        </plugins>\n    </build>", "        </plugins>\n        </pluginManagement>\n    </build>")
    prof = ("    <profiles>\n        <profile>\n            <id>apt</id>\n"
            + "".join("    " * 2 + line + "\n" for line in compiler(ga(LOMBOK, ind=" " * 32)).splitlines())
            + "        </profile>\n    </profiles>\n")
    exe = compiler(ga(LOMBOK, ind=" " * 32)).replace(
        "                <configuration>\n", "                <executions>\n                    <execution>\n"
        "                        <id>default-compile</id>\n                        <configuration>\n") \
        .replace("                </configuration>\n", "                        </configuration>\n"
                 "                    </execution>\n                </executions>\n") \
        .replace("                    <annotationProcessorPaths>", "                            <annotationProcessorPaths>") \
        .replace("                    </annotationProcessorPaths>", "                            </annotationProcessorPaths>")
    fs = files(core=child("shop-core", build=pm), api=child("shop-api", tail=prof),
               boot=child("shop-boot", dep("org.springframework.boot", "spring-boot-starter-web"), build=exe))
    ws, res, params = run(tmp_path, ccfg, fs, capture=asis({LOMBOK: "1.18.30"}))
    for pom in (CORE, API, BOOTM):
        ed = PomEditor(ws.root / pom)
        got = [PomEditor.text_of(el, "version") for el, _prof in ed.annotation_processor_paths()]
        assert got == ["1.18.30"], pom
    assert sorted(proc_lines(res)) == sorted(
        f"{LOMBOK}: 1.18.30 ← as-is managed by {BOOT2_PARENT} ({SEC}, {p})" for p in (CORE, API, BOOTM))
    again(ws, ccfg, params)


def test_three_modules_in_one_restructure_and_the_record(tmp_path: Path, ccfg: Config) -> None:
    fs = files(core=child("shop-core", build=compiler(ga(LOMBOK))), api=child("shop-api", build=compiler(ga(LOMBOK))),
               boot=child("shop-boot", dep("org.springframework.boot", "spring-boot-starter-web"),
                          build=compiler(ga(LOMBOK))))
    ws, res, params = run(tmp_path, ccfg, fs, capture=asis({LOMBOK: "1.18.30"}))
    assert sorted(proc_lines(res)) == sorted(f"{LOMBOK}: 1.18.30 ← as-is managed by {BOOT2_PARENT} ({SEC}, {p})"
                                             for p in (BOOTM, CORE, API))
    assert len(proc_lines(res)) == len(set(proc_lines(res)))
    assert any(n.startswith(f"3 annotation processor path version(s) written: {LOMBOK} 1.18.30") for n in res.notes)
    ev = _events(ws)
    assert len(ev) == 1 and ev[0]["pinned"] == proc_lines(res) and ev[0]["unknown"] == []
    assert {CORE, API, BOOTM} <= set(res.changed_files)
    # the new line is indented like its siblings, the rest of the block is unchanged
    text = (ws.root / CORE).read_text(encoding="utf-8")
    assert ("                        <path>\n"
            "                            <groupId>org.projectlombok</groupId>\n"
            "                            <artifactId>lombok</artifactId>\n"
            "                            <version>1.18.30</version>\n"
            "                        </path>\n") in text
    again(ws, ccfg, params)


def test_a_pom_this_step_does_not_change_stays_byte_identical(tmp_path: Path, ccfg: Config) -> None:
    """The step only writes where it found a version: the same tree with and without the version-less path differs
    only in the path's pom, and that pom only by the one `<version>` line."""
    with_path = files(core=child("shop-core", build=compiler(ga(LOMBOK))))
    ws, res, _ = run(tmp_path, ccfg, with_path, capture=asis({LOMBOK: "1.18.30"}), name="a")
    without = files(core=child("shop-core", build=compiler(ga(LOMBOK, "1.18.30"))))
    ws2, res2, _ = run(tmp_path, ccfg, without, capture=asis({LOMBOK: "1.18.30"}), name="b")
    for p in ("pom.xml", CORE, API, BOOTM):
        assert (ws.root / p).read_bytes() == (ws2.root / p).read_bytes(), p
    assert sorted(res.changed_files) == sorted(res2.changed_files)
    assert proc_lines(res2) == []


def test_without_any_facts_of_the_new_chain_only_the_own_dependency(tmp_path: Path, ccfg: Config) -> None:
    """No effective pom and no parent chain facts: whether the new chain manages the g:a is unknown, so only step 2
    (the pom's own dependency, which Maven uses for that dependency anyway) writes a version."""
    fs = files(core=child("shop-core", dep("org.projectlombok", "lombok", "1.18.36", scope="provided"),
                          build=compiler(ga(LOMBOK))),
               api=child("shop-api", build=compiler(ga(LOMBOK))))
    ws, res, _ = run(tmp_path, ccfg, fs, capture=asis({LOMBOK: "1.18.30"}), after=None)
    assert paths_of(ws, CORE) == {LOMBOK: "1.18.36"}
    assert paths_of(ws, API) == {LOMBOK: None}
    assert (f"annotation processor path {LOMBOK} in {API} left without a version: the managed "
                                   "versions of the new parent chain are unknown (no effective pom, no parent chain "
                                   "facts)") in res.notes


def test_layout_mode_writes_the_path_of_the_split_module(tmp_path: Path, ccfg: Config) -> None:
    from .test_v21l_pin_versions import _after, _legacy_files, _legacy_profile
    from .test_v21l_pin_versions import _asis as _legacy_asis
    from .test_v21l_pin_versions import _inventory as _legacy_inventory

    fs = _legacy_files()
    fs["pom.xml"] = fs["pom.xml"].replace("</project>", compiler(ga("org.apache.commons:commons-text")) + "</project>")
    ws = make_ws(tmp_path, fs, "lay")
    res = Restructurer().apply(ws, _legacy_inventory(ws, _legacy_profile(ws)), ccfg, mode="layout",
                               params={"asis_effective": _legacy_asis, "effective_pom": lambda: _after()})
    assert res.ok, res.error
    back = "shop-back/pom.xml"
    text = "org.apache.commons:commons-text"
    # the dependency step carried the as-is version over into the dependency; the path follows it
    assert {d.key: d.version for d in PomEditor(ws.root / back).dependencies()}[text] == "1.10.0"
    assert paths_of(ws, back) == {text: "1.10.0"}
    assert proc_lines(res) == [f"{text}: 1.10.0 ← the pom's own dependency ({SEC}, {back})"]
    res2 = Restructurer().apply(ws, _legacy_profile(ws), ccfg, mode="layout",
                                params={"asis_effective": _legacy_asis, "effective_pom": lambda: _after()})
    assert res2.ok and res2.changed_files == [] and proc_lines(res2) == []


# ------------------------------------------------------------------------------- review round (P1, P2, P6, P7)
IMPORT_BOM = ("    <dependencyManagement>\n        <dependencies>\n            <dependency>\n"
              "                <groupId>com.acme.platform</groupId>\n                <artifactId>acme-bom</artifactId>\n"
              "                <version>2.0.0</version>\n                <type>pom</type>\n"
              "                <scope>import</scope>\n            </dependency>\n        </dependencies>\n"
              "    </dependencyManagement>\n")


def _chain_facts(monkeypatch) -> None:
    """Parent chain facts that manage nothing of interest (the fallback source of the managed versions)."""
    facts = parent_chain.ParentChainFacts(roots=[COMPJ], managed={"x:y": "1"}, properties={}, sources=["t"], poms=1)
    monkeypatch.setattr(parent_chain, "load_parent_chain_facts", lambda cfg: facts)


def test_p7_an_imported_bom_without_the_effective_pom_is_maybe_managed(tmp_path: Path, ccfg: Config,
                                                                       monkeypatch) -> None:
    """Without the effective pom the step cannot see what the BOM the module imports manages: no version from steps
    2 to 4 (the old as-is version would override what Maven takes from the BOM), one note."""
    _chain_facts(monkeypatch)
    fs = files(core=child("shop-core", dep("org.projectlombok", "lombok", "1.18.36", scope="provided"),
                          build=compiler(ga(LOMBOK)), head=IMPORT_BOM),
               api=child("shop-api", build=compiler(ga(LOMBOK))))
    ws, res, params = run(tmp_path, ccfg, fs, capture=asis({LOMBOK: "1.18.20"}), after=None)
    assert paths_of(ws, CORE) == {LOMBOK: None}                 # not even the own dependency (step 2)
    assert paths_of(ws, API) == {LOMBOK: "1.18.20"}             # no import there: step 3 as before
    assert proc_lines(res) == [f"{LOMBOK}: 1.18.20 ← as-is managed by {BOOT2_PARENT} ({SEC}, {API})"]
    assert (f"annotation processor path {LOMBOK} in {CORE} left without a version: it may be managed by the BOM "
            f"com.acme.platform:acme-bom imported in {CORE} (no effective pom of the new parent chain to tell)") \
        in res.notes
    assert _events(ws)[0]["unknown"] == [f"{LOMBOK} ({CORE})"]
    again(ws, ccfg, params)


def test_p7_a_profiles_dependency_management_without_the_effective_pom(tmp_path: Path, ccfg: Config,
                                                                       monkeypatch) -> None:
    _chain_facts(monkeypatch)
    prof = ("    <profiles>\n        <profile>\n            <id>pinned</id>\n"
            "            <dependencyManagement>\n                <dependencies>\n"
            + dep("org.projectlombok", "lombok", "1.18.42").replace("        <", "                    <")
            + "                </dependencies>\n            </dependencyManagement>\n"
            "        </profile>\n    </profiles>\n")
    fs = files(core=child("shop-core", build=compiler(ga(LOMBOK)), tail=prof))
    ws, res, _ = run(tmp_path, ccfg, fs, capture=asis({LOMBOK: "1.18.20"}), after=None)
    assert paths_of(ws, CORE) == {LOMBOK: None}
    assert proc_lines(res) == []
    assert (f"annotation processor path {LOMBOK} in {CORE} left without a version: it may be managed by the "
            f"<dependencyManagement> of profile pinned in {CORE} (no effective pom of the new parent chain to tell)") \
        in res.notes


def test_p7_with_the_effective_pom_an_import_changes_nothing(tmp_path: Path, ccfg: Config) -> None:
    """The effective pom includes what the imported BOM manages: step 1 is exact, the import is no reason to stop."""
    fs = files(core=child("shop-core", build=compiler(ga(LOMBOK)), head=IMPORT_BOM))
    ws, res, _ = run(tmp_path, ccfg, fs, capture=asis({LOMBOK: "1.18.30"}))
    assert paths_of(ws, CORE) == {LOMBOK: "1.18.30"}
    assert not any("may be managed" in n for n in res.notes)


def test_p6_a_repository_guess_is_named_and_not_declared(tmp_path: Path, ccfg: Config, monkeypatch) -> None:
    """No as-is version, no Boot BOM: the dependency step takes the repository's newest release. The path of the same
    pom follows the dependency but says where the version came from; another pom does not take it as declared."""
    _chain_facts(monkeypatch)
    monkeypatch.setattr(Restructurer, "_repository_release", lambda self, c, g, a: "9.9.9")
    fs = files(core=child("shop-core", dep("org.mapstruct", "mapstruct-processor", scope="provided"),
                          build=compiler(ga(MSP))),
               api=child("shop-api", build=compiler(ga(MSP))))
    ws, res, _ = run(tmp_path, ccfg, fs, capture=asis({LOMBOK: "1.18.30"}))
    assert {d.key: d.version for d in PomEditor(ws.root / CORE).dependencies()}[MSP] == "9.9.9"
    assert paths_of(ws, CORE) == {MSP: "9.9.9"}
    assert paths_of(ws, API) == {MSP: None}
    assert proc_lines(res) == [f"{MSP}: 9.9.9 ← the pom's own dependency (repository newest release) ({SEC}, {CORE})"]
    assert (f"annotation processor path {MSP} in {API} left without a version: no project pom, as-is build or Boot "
            "BOM names one; the build reports it") in res.notes


def test_p1_a_classifier_path_is_not_managed_by_the_plain_ga(tmp_path: Path, ccfg: Config) -> None:
    cls = "                            <classifier>x</classifier>\n"
    fs = files(core=child("shop-core", dep("org.projectlombok", "lombok", "1.18.36", scope="provided",
                                           extra="            <classifier>x</classifier>\n"),
                          build=compiler(ga(LOMBOK, extra=cls))))
    after = {"managed": {LOMBOK: "1.18.42"}, "by_module": {"com.acme:shop-core": {"managed": {LOMBOK: "1.18.42"}}}}
    ws, res, _ = run(tmp_path, ccfg, fs, capture=asis({LOMBOK: "1.18.30"}), after=after)
    assert paths_of(ws, CORE) == {LOMBOK: "1.18.36"}
    assert proc_lines(res) == [f"{LOMBOK}: 1.18.36 ← the pom's own dependency ({SEC}, {CORE})"]


def test_p2_without_an_as_is_capture_no_boot_bom_version(tmp_path: Path, ccfg: Config, monkeypatch) -> None:
    """The dependency step writes nothing without an as-is capture; neither does step 3."""
    _boot_facts(monkeypatch, {LOMBOK: "1.18.42"})
    fs = files(core=child("shop-core", dep("org.projectlombok", "lombok", scope="provided"),
                          build=compiler(ga(LOMBOK))))
    ws = make_ws(tmp_path, fs, "p2")
    params = {"asis_effective": lambda: None,
              "effective_pom": lambda: {"managed": {"org.springframework:spring-core": "7.0.1"}, "by_module": {}}}
    res = Restructurer().apply(ws, _inventory(ws, _profile(ws)), ccfg, mode="boot_pom", params=params)
    assert res.ok, res.error
    assert {d.key: d.version for d in PomEditor(ws.root / CORE).dependencies()}[LOMBOK] is None
    assert paths_of(ws, CORE) == {LOMBOK: None}
    assert proc_lines(res) == []
    assert (f"annotation processor path {LOMBOK} in {CORE} left without a version: no project pom, as-is build or "
            "Boot BOM names one; the build reports it") in res.notes
