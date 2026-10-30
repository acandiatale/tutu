"""v2.1m: binary files under Maven resource filtering get their extensions into maven-resources-plugin (maven-war-plugin
for filtered `<webResources>`) `<nonFilteredFileExtensions>` — written by sbmig (restructure, and the loop's reactive
try through `reactive_targets` + `binary_resource_plan(errors=…)`), never by the LLM. Offline."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from sbmig.core.config import BuildConfig, Config, load_config
from sbmig.restructure import Restructurer
from sbmig.restructure.resources_filtering import (
    RESOURCES_PLUGIN,
    WAR_PLUGIN,
    ResourcePlan,
    apply_non_filtered_extensions,
    binary_resource_plan,
    reactive_targets,
)

from .helpers import HEAD, CaseType, all_, dep, java, make_ws, profile, t, xml
from .test_company_layout import _boot_multi_files, _boot_multi_profile, _roles

REPO_ROOT = Path(__file__).resolve().parents[2]
NUL_BLOB = b"SQLite\x00\xff\xfe\x00\x81" * 20          # binary content, unknown extension
FILTERED = ("    <build>\n        <resources>\n            <resource>\n"
            "                <directory>src/main/resources</directory>\n"
            "                <filtering>true</filtering>\n            </resource>\n        </resources>\n"
            "{plugins}    </build>\n")
ROLE_PARENT = ("    <parent>\n        <groupId>com.company.platform</groupId>\n        <artifactId>comp-jar</artifactId>\n"
               "        <version>4.0.0</version>\n        <relativePath/>\n    </parent>\n")


def _pom(aid: str, *, parent: str = "", build: str = "", packaging: str = "", modules: str = "") -> str:
    return (HEAD + parent + f"    <groupId>com.acme</groupId>\n    <artifactId>{aid}</artifactId>\n"
            "    <version>1.0.0</version>\n" + (f"    <packaging>{packaging}</packaging>\n" if packaging else "")
            + modules + build + "</project>\n")


def _filtered_pom(aid: str = "shop", *, parent: str = "", plugins: str = "") -> str:
    return _pom(aid, parent=parent, build=FILTERED.format(plugins=plugins))


def _binaries(root: Path, base: str = "") -> None:
    """fonts/a.woff2 (table), data/items.dmdb (NUL bytes), img/logo.png (plugin default), data/README.txt (text),
    data/BLOB (no extension) under `<base>src/main/resources`."""
    res = root / base / "src/main/resources"
    for d in ("fonts", "data", "img"):
        (res / d).mkdir(parents=True, exist_ok=True)
    (res / "fonts/a.woff2").write_bytes(b"wOF2\x00\x01\x02")
    (res / "data/items.dmdb").write_bytes(NUL_BLOB)
    (res / "img/logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00")
    (res / "data/README.txt").write_text("read me\n", encoding="utf-8")
    (res / "data/BLOB").write_bytes(b"\x00\x01\x02")


def _ws(tmp_path: Path, files: dict[str, str], name: str = "rf"):
    return make_ws(tmp_path, files, name)


def _cfg(*exts: str) -> SimpleNamespace:
    return SimpleNamespace(build=BuildConfig(non_filtered_extensions=list(exts)))


def _exts(ws, pom: str = "pom.xml", where: str = "build/plugins/plugin", artifact: str = RESOURCES_PLUGIN):
    root = xml(ws.root / pom)
    for p in all_(root, where):
        if t(p, "artifactId") == artifact:
            return [x.text for x in all_(p, "configuration/nonFilteredFileExtensions/nonFilteredFileExtension")]
    return None


# ------------------------------------------------------------------------------------------------ the plan
def test_plan_lists_the_binary_extensions_and_apply_is_idempotent(tmp_path: Path) -> None:
    ws = _ws(tmp_path, {"pom.xml": _filtered_pom()})
    _binaries(ws.root)
    plan = binary_resource_plan(ws, _cfg(), ["pom.xml"])
    assert plan.extensions_by_pom == {"pom.xml": ["dmdb", "woff2"]}      # png = plugin default, txt = text
    assert plan.plugins_by_pom == {"pom.xml": [RESOURCES_PLUGIN]}
    assert plan.files == ["src/main/resources/data/BLOB", "src/main/resources/data/items.dmdb",
                          "src/main/resources/fonts/a.woff2"]
    assert len(plan.followups) == 1 and "src/main/resources/data/BLOB" in plan.followups[0]
    assert "exclude it from filtering" in plan.followups[0] and "rename it" in plan.followups[0]
    assert plan.lines() == ["pom.xml: dmdb, woff2 → maven-resources-plugin"]
    assert apply_non_filtered_extensions(ws, plan) == ["pom.xml"]
    root = xml(ws.root / "pom.xml")
    plugin = all_(root, "build/plugins/plugin")[0]
    assert t(plugin, "groupId") == "org.apache.maven.plugins" and t(plugin, "version") is None   # parent manages it
    assert _exts(ws) == ["dmdb", "woff2"]
    nfe = all_(plugin, "configuration/nonFilteredFileExtensions")[0]
    assert nfe.get("combine.children") == "append"          # an outside parent's own list is kept by Maven
    assert t(root, "build/resources/resource/filtering") == "true"          # filtering stays on
    before = (ws.root / "pom.xml").read_bytes()
    again = binary_resource_plan(ws, _cfg(), ["pom.xml"])
    assert again.empty and again.extensions_by_pom == {}
    assert again.files == ["src/main/resources/data/BLOB"]                   # what is left: the extension-less one
    assert apply_non_filtered_extensions(ws, plan) == []                     # the old plan again: nothing to add
    assert (ws.root / "pom.xml").read_bytes() == before


def test_company_extensions_are_written_to_every_filtering_pom(tmp_path: Path) -> None:
    files = {"pom.xml": _pom("agg", packaging="pom", modules="    <modules>\n        <module>a</module>\n"
                                                             "        <module>b</module>\n    </modules>\n"),
             "a/pom.xml": _filtered_pom("a", parent=ROLE_PARENT), "b/pom.xml": _pom("b", parent=ROLE_PARENT)}
    ws = _ws(tmp_path, files)
    cfg = _cfg("DMDB", ".xyz", "png", "properties")
    assert cfg.build.non_filtered_extensions == ["dmdb", "xyz", "png", "properties"]
    plan = binary_resource_plan(ws, cfg, None)
    # no binary file at all: the company list alone, never the image default / a text extension; b filters nothing
    assert plan.extensions_by_pom == {"a/pom.xml": ["dmdb", "xyz"]}
    assert apply_non_filtered_extensions(ws, plan) == ["a/pom.xml"]
    assert _exts(ws, "a/pom.xml") == ["dmdb", "xyz"]


def test_company_extensions_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("SBMIG_BUILD__NON_FILTERED_EXTENSIONS", "[DMDB, .xyz]")
    cfg = load_config(REPO_ROOT / "config" / "artifacts.local-e2e.yml", repo_root=REPO_ROOT)
    assert cfg.build.non_filtered_extensions == ["dmdb", "xyz"]
    assert BuildConfig().non_filtered_extensions == []
    assert BuildConfig(non_filtered_extensions="dmdb, xyz").non_filtered_extensions == ["dmdb", "xyz"]


def test_an_existing_list_only_gets_the_missing_entries(tmp_path: Path) -> None:
    plugins = ("        <plugins>\n            <plugin>\n"
               "                <artifactId>maven-resources-plugin</artifactId>\n"
               "                <version>3.3.1</version>\n"
               "                <executions>\n                    <execution>\n"
               "                        <id>copy</id>\n"
               "                        <configuration>\n"
               "                            <nonFilteredFileExtensions>\n"
               "                                <nonFilteredFileExtension>zip</nonFilteredFileExtension>\n"
               "                            </nonFilteredFileExtensions>\n"
               "                        </configuration>\n"
               "                    </execution>\n                </executions>\n"
               "                <configuration>\n                    <encoding>UTF-8</encoding>\n"
               "                    <nonFilteredFileExtensions>\n"
               "                        <nonFilteredFileExtension>WOFF2</nonFilteredFileExtension>\n"
               "                    </nonFilteredFileExtensions>\n"
               "                </configuration>\n            </plugin>\n        </plugins>\n")
    ws = _ws(tmp_path, {"pom.xml": _filtered_pom(plugins=plugins)})
    _binaries(ws.root)
    plan = binary_resource_plan(ws, _cfg(), None)
    assert plan.extensions_by_pom == {"pom.xml": ["dmdb"]}                  # woff2 is listed already (any case)
    assert apply_non_filtered_extensions(ws, plan) == ["pom.xml"]
    root = xml(ws.root / "pom.xml")
    plugin = all_(root, "build/plugins/plugin")[0]
    assert t(plugin, "version") == "3.3.1" and t(plugin, "configuration/encoding") == "UTF-8"
    assert _exts(ws) == ["WOFF2", "dmdb"]                                    # appended, the existing one kept
    assert all_(plugin, "configuration/nonFilteredFileExtensions")[0].get("combine.children") is None
    execution = [x.text for x in all_(plugin, "executions/execution/configuration/nonFilteredFileExtensions/"
                                               "nonFilteredFileExtension")]
    assert execution == ["zip"]                                              # an execution's list is left alone
    text = (ws.root / "pom.xml").read_text(encoding="utf-8")
    assert ("                        <nonFilteredFileExtension>WOFF2</nonFilteredFileExtension>\n"
            "                        <nonFilteredFileExtension>dmdb</nonFilteredFileExtension>\n") in text


MANAGED = ("        <pluginManagement>\n            <plugins>\n                <plugin>\n"
           "                    <groupId>org.apache.maven.plugins</groupId>\n"
           "                    <artifactId>maven-resources-plugin</artifactId>\n"
           "                    <version>3.3.1</version>\n                </plugin>\n            </plugins>\n"
           "        </pluginManagement>\n")
BUILT = ("        <plugins>\n            <plugin>\n                <artifactId>maven-resources-plugin</artifactId>\n"
         "            </plugin>\n        </plugins>\n")


@pytest.mark.parametrize("case", ["management-no-parent", "management-role-parent", "build-and-management"])
def test_plugin_element_decision(tmp_path: Path, case: str) -> None:
    parent = ROLE_PARENT if case == "management-role-parent" else ""
    plugins = MANAGED + (BUILT if case == "build-and-management" else "")
    ws = _ws(tmp_path, {"pom.xml": _filtered_pom(parent=parent, plugins=plugins)})
    _binaries(ws.root)
    assert apply_non_filtered_extensions(ws, binary_resource_plan(ws, _cfg(), None)) == ["pom.xml"]
    managed = _exts(ws, where="build/pluginManagement/plugins/plugin")
    built = _exts(ws)
    if case == "management-no-parent":
        # no <build><plugins> declaration, no outside parent: the managed entry is extended, nothing created
        assert managed == ["dmdb", "woff2"] and built is None
        assert all_(xml(ws.root / "pom.xml"), "build/plugins") == []
    elif case == "management-role-parent":
        # on a role parent an inherited <build><plugins> declaration would win over this pom's management entry
        assert managed == [] and built == ["dmdb", "woff2"]
    else:
        assert built == ["dmdb", "woff2"] and managed == []                  # the <build><plugins> one wins
    assert apply_non_filtered_extensions(ws, binary_resource_plan(ws, _cfg(), None)) == []   # idempotent


def _child(aid: str, *, own_filtering: bool = False, role: bool = False) -> str:
    parent = ROLE_PARENT if role else ("    <parent>\n        <groupId>com.acme</groupId>\n"
                                       "        <artifactId>shop</artifactId>\n        <version>1.0.0</version>\n"
                                       "    </parent>\n")
    return _pom(aid, parent=parent, build=FILTERED.format(plugins="") if own_filtering else "")


def test_root_declared_filtering_is_written_to_the_root_once(tmp_path: Path) -> None:
    modules = "    <modules>\n        <module>core</module>\n        <module>web</module>\n    </modules>\n"
    root = _pom("shop", packaging="pom", modules=modules, build=FILTERED.format(plugins=""))
    ws = _ws(tmp_path, {"pom.xml": root, "core/pom.xml": _child("core"),
                        "web/pom.xml": _child("web", own_filtering=True)})
    (ws.root / "core/src/main/resources/data").mkdir(parents=True)
    (ws.root / "core/src/main/resources/data/items.dmdb").write_bytes(NUL_BLOB)     # inherited <resources>
    (ws.root / "web/src/main/resources/fonts").mkdir(parents=True)
    (ws.root / "web/src/main/resources/fonts/f.ttf").write_bytes(b"\x00\x01\x00\x00")   # own <resources>
    plan = binary_resource_plan(ws, _cfg(), None)
    # both modules inherit the root's plugin configuration → one list in the root
    assert plan.extensions_by_pom == {"pom.xml": ["dmdb", "ttf"]}
    assert plan.files == ["core/src/main/resources/data/items.dmdb", "web/src/main/resources/fonts/f.ttf"]
    assert apply_non_filtered_extensions(ws, plan) == ["pom.xml"]
    assert _exts(ws) == ["dmdb", "ttf"] and _exts(ws, "core/pom.xml") is None and _exts(ws, "web/pom.xml") is None


def test_a_module_on_a_role_parent_gets_its_own_entry(tmp_path: Path) -> None:
    modules = "    <modules>\n        <module>core</module>\n        <module>back</module>\n    </modules>\n"
    root = _pom("shop", packaging="pom", modules=modules, build=FILTERED.format(plugins=""))
    ws = _ws(tmp_path, {"pom.xml": root, "core/pom.xml": _child("core", role=True),
                        "back/pom.xml": _child("back", own_filtering=True, role=True)})
    for mod in ("core", "back"):
        (ws.root / f"{mod}/src/main/resources/data").mkdir(parents=True)
        (ws.root / f"{mod}/src/main/resources/data/items.dmdb").write_bytes(NUL_BLOB)
    plan = binary_resource_plan(ws, _cfg(), None)
    # core inherits nothing from the aggregator root (its parent is comp-jar) and declares no filtering itself
    assert plan.extensions_by_pom == {"back/pom.xml": ["dmdb"]}
    assert apply_non_filtered_extensions(ws, plan, poms=["pom.xml"]) == []          # `poms` restricts
    assert apply_non_filtered_extensions(ws, plan) == ["back/pom.xml"]
    assert _exts(ws, "back/pom.xml") == ["dmdb"] and _exts(ws) is None


def test_filtered_web_resources_extend_the_war_plugin(tmp_path: Path) -> None:
    war = ("    <build>\n        <plugins>\n            <plugin>\n"
           "                <artifactId>maven-war-plugin</artifactId>\n"
           "                <configuration>\n                    <webResources>\n                        <resource>\n"
           "                            <directory>src/main/webapp</directory>\n"
           "                            <filtering>true</filtering>\n"
           "                        </resource>\n                    </webResources>\n"
           "                </configuration>\n            </plugin>\n        </plugins>\n    </build>\n")
    ws = _ws(tmp_path, {"pom.xml": _pom("web", packaging="war", build=war)})
    (ws.root / "src/main/webapp/fonts").mkdir(parents=True)
    (ws.root / "src/main/webapp/fonts/x.eot").write_bytes(b"\x00\x01")
    plan = binary_resource_plan(ws, _cfg(), None)
    assert plan.extensions_by_pom == {"pom.xml": ["eot"]} and plan.plugins_by_pom == {"pom.xml": [WAR_PLUGIN]}
    assert plan.lines() == ["pom.xml: eot → maven-war-plugin"]
    assert apply_non_filtered_extensions(ws, plan) == ["pom.xml"]
    assert _exts(ws, artifact=WAR_PLUGIN) == ["eot"] and _exts(ws) is None
    root = xml(ws.root / "pom.xml")
    assert t(all_(root, "build/plugins/plugin")[0], "configuration/webResources/resource/filtering") == "true"


def test_the_plan_never_raises(tmp_path: Path) -> None:
    ws = _ws(tmp_path, {"pom.xml": "<project><unclosed></project>\n", "ok/pom.xml": _filtered_pom("ok")})
    (ws.root / "ok/src/main/resources").mkdir(parents=True)
    (ws.root / "ok/src/main/resources/k.jks").write_bytes(b"\xfe\xed\xfe\xed\x00")
    plan = binary_resource_plan(ws, _cfg(), None)
    assert plan.extensions_by_pom == {"ok/pom.xml": ["jks"]}               # the broken pom is skipped
    assert binary_resource_plan(object(), None, None) == ResourcePlan()
    assert reactive_targets(object(), [object()]) == []
    assert apply_non_filtered_extensions(ws, ResourcePlan(extensions_by_pom={"missing/pom.xml": ["x"]})) == []


# ------------------------------------------------------------------------------------------------ reactive
def _err(message: str, *, goal: str | None = "resources:resources", module: str | None = None,
         file: str | None = None, raw: str = "") -> SimpleNamespace:
    return SimpleNamespace(message=message, raw=raw, goal=goal, module=module, file=file)


FAIL = ("Failed to execute goal org.apache.maven.plugins:maven-resources-plugin:3.3.1:resources (default-resources) "
        "on project {aid}: filtering {path} to {to} failed with MalformedInputException: Input length = 1")


def test_reactive_targets_from_the_file_the_error_names(tmp_path: Path) -> None:
    ws = _ws(tmp_path, {"pom.xml": _filtered_pom()})
    # the scan does not flag x.dmdb (it does not even exist), the error names it
    err = _err(FAIL.format(aid="shop", path="src/main/resources/data/x.dmdb", to="target/classes/data/x.dmdb"))
    assert reactive_targets(ws, [err]) == ["pom.xml"]
    plan = binary_resource_plan(ws, _cfg(), None, errors=[err])
    assert plan.extensions_by_pom == {"pom.xml": ["dmdb"]} and plan.files == ["src/main/resources/data/x.dmdb"]
    assert apply_non_filtered_extensions(ws, plan) == ["pom.xml"]
    assert binary_resource_plan(ws, _cfg(), None, errors=[err]).empty        # once written, nothing to add
    # another failure (javac, a text file in cp949) is not this case
    javac = _err("unmappable character (0xEB) for encoding UTF-8", goal="compiler:compile")
    assert reactive_targets(ws, [javac]) == []
    props = _err(FAIL.format(aid="shop", path="src/main/resources/messages.properties", to="t/m.properties"))
    assert reactive_targets(ws, [props]) == ["pom.xml"]
    assert binary_resource_plan(ws, _cfg(), None, errors=[props]).empty      # never a text extension
    assert binary_resource_plan(ws, _cfg(), None, errors=[]) == ResourcePlan()


def test_reactive_targets_in_a_multi_module_tree(tmp_path: Path) -> None:
    modules = "    <modules>\n        <module>shop-back</module>\n        <module>shop-boot</module>\n    </modules>\n"
    files = {"pom.xml": _pom("shop", packaging="pom", modules=modules),
             "shop-back/pom.xml": _filtered_pom("shop-back", parent=ROLE_PARENT),
             "shop-boot/pom.xml": _pom("shop-boot", parent=ROLE_PARENT)}           # filtering from comp-jar only
    ws = _ws(tmp_path, files)
    (ws.root / "shop-boot/src/main/resources/data").mkdir(parents=True)
    (ws.root / "shop-boot/src/main/resources/data/items.dmdb").write_bytes(NUL_BLOB)
    absolute = str(ws.root / "shop-back/src/main/resources/data/y.qqq")
    by_path = _err(FAIL.format(aid="shop-back", path=absolute, to="/x/target/classes/data/y.qqq"))
    assert reactive_targets(ws, [by_path]) == ["shop-back/pom.xml"]
    assert binary_resource_plan(ws, _cfg(), None, errors=[by_path]).extensions_by_pom == {
        "shop-back/pom.xml": ["qqq"]}
    # maven-resources-plugin 3.2.x names no file: the module of `on project` / BuildError.module; its filtering comes
    # from the outside parent, so its default src/main/resources is scanned as filtered
    bare = _err("Failed to execute goal org.apache.maven.plugins:maven-resources-plugin:3.3.1:resources "
                "(default-resources) on project shop-boot: Input length = 1", module="shop-boot")
    assert reactive_targets(ws, [bare]) == ["shop-boot/pom.xml"]
    plan = binary_resource_plan(ws, _cfg("xyz"), None, errors=[bare])
    assert plan.extensions_by_pom == {"shop-boot/pom.xml": ["dmdb", "xyz"]}
    assert plan.files == ["shop-boot/src/main/resources/data/items.dmdb"]
    # nothing names a module → every pom declaring filtering
    anon = _err("MalformedInputException: Input length = 1", goal="resources:resources")
    assert reactive_targets(ws, [anon]) == ["shop-back/pom.xml"]
    war = _err("Failed to execute goal org.apache.maven.plugins:maven-war-plugin:3.4.0:war (default-war) on project "
               "shop-back: filtering shop-back/src/main/webapp/f.ttf to t/f.ttf failed with "
               "MalformedInputException: Input length = 1", goal="war:war")
    plan = binary_resource_plan(ws, _cfg(), None, errors=[war])
    assert plan.plugins_by_pom == {"shop-back/pom.xml": [WAR_PLUGIN]} and plan.extensions_by_pom == {
        "shop-back/pom.xml": ["ttf"]}


# ------------------------------------------------------------------------------------------ restructure wiring
@pytest.fixture()
def ccfg(cfg: Config) -> Config:
    """The round-9c company layout with role parents (as `test_v21l_pin_versions.ccfg`)."""
    L = cfg.layout
    L.module_dir, L.boot_module = "", "{name}-boot"
    L.comp_module, L.front_module = "{name}-back", "{name}-front"
    L.boot_as_is = "in-place"
    L.comp_artifact_suffix = "-back"
    return _roles(cfg)


def _events(ws, name: str) -> list[dict]:
    if not ws.journal_path.is_file():
        return []
    lines = ws.journal_path.read_text(encoding="utf-8").splitlines()
    return [e for e in (json.loads(x) for x in lines if x.strip()) if e.get("event") == name]


def _legacy_files() -> dict[str, str]:
    pom = (HEAD + "    <groupId>com.acme</groupId>\n    <artifactId>shop</artifactId>\n    <version>1.0.0</version>\n"
           "    <dependencies>\n" + dep("org.springframework", "spring-context", "6.1.14") + "    </dependencies>\n"
           + FILTERED.format(plugins="") + "</project>\n")
    return {"pom.xml": pom, "src/main/java/com/acme/shop/Main.java": java("com.acme.shop", "Main"),
            "src/main/resources/application.properties": "app.version=@project.version@\n"}


def test_layout_writes_the_extensions_into_the_back_module(tmp_path: Path, ccfg: Config) -> None:
    ccfg.build.non_filtered_extensions = ["xyz"]
    ws = make_ws(tmp_path, _legacy_files(), "layout-rf")
    _binaries(ws.root)
    ws.commit("binaries")
    prof = profile(ws, CaseType.SPRING_LEGACY, artifact_id="shop", version="1.0.0", base_package="com.acme.shop")
    res = Restructurer().apply(ws, prof, ccfg, mode="layout")
    assert res.ok, res.error
    back = "shop-back/pom.xml"
    assert res.non_filtered_extensions == [f"{back}: dmdb, woff2, xyz → maven-resources-plugin"]
    assert _exts(ws, back) == ["dmdb", "woff2", "xyz"] and back in res.changed_files
    assert t(xml(ws.root / back), "build/resources/resource/filtering") == "true"
    assert any(n.startswith("nonFilteredFileExtensions written for binary files under filtered resources")
               and back in n for n in res.notes)
    assert any("shop-back/src/main/resources/data/BLOB" in f and "exclude it from filtering" in f
               for f in res.manual_followups)
    ev = _events(ws, "non_filtered_extensions")
    assert len(ev) == 1 and ev[0]["poms"] == {back: ["dmdb", "woff2", "xyz"]}
    assert ev[0]["plugins"] == {back: [RESOURCES_PLUGIN]} and len(ev[0]["followups"]) == 1
    # the already-restructured pass finds nothing left to write
    before = (ws.root / back).read_bytes()
    res2 = Restructurer().apply(ws, prof, ccfg, mode="layout")
    assert res2.ok and "already restructured" in res2.notes
    assert res2.non_filtered_extensions == [] and (ws.root / back).read_bytes() == before
    assert len(_events(ws, "non_filtered_extensions")) == 2              # the extension-less file is still there


def test_layout_without_binaries_changes_nothing(tmp_path: Path, ccfg: Config) -> None:
    ws = make_ws(tmp_path, _legacy_files(), "layout-none")
    prof = profile(ws, CaseType.SPRING_LEGACY, artifact_id="shop", version="1.0.0", base_package="com.acme.shop")
    res = Restructurer().apply(ws, prof, ccfg, mode="layout")
    assert res.ok, res.error
    assert res.non_filtered_extensions == [] and _exts(ws, "shop-back/pom.xml") is None
    assert not any("nonFilteredFileExtensions" in n for n in res.notes)
    assert _events(ws, "non_filtered_extensions") == []


def test_boot_pom_writes_each_module_that_filters(tmp_path: Path, ccfg: Config) -> None:
    files = _boot_multi_files()
    files["shop-core/pom.xml"] = files["shop-core/pom.xml"].replace(
        "</project>", FILTERED.format(plugins="") + "</project>")
    ws = make_ws(tmp_path, files, "boot-rf")
    (ws.root / "shop-core/src/main/resources/data").mkdir(parents=True)
    (ws.root / "shop-core/src/main/resources/data/items.dmdb").write_bytes(NUL_BLOB)
    ws.commit("binaries")
    res = Restructurer().apply(ws, _boot_multi_profile(ws), ccfg, mode="boot_pom")
    assert res.ok, res.error
    assert res.non_filtered_extensions == ["shop-core/pom.xml: dmdb → maven-resources-plugin"]
    assert _exts(ws, "shop-core/pom.xml") == ["dmdb"]
    assert _exts(ws, "shop-api/pom.xml") is None and _exts(ws, "shop-boot/pom.xml") is None
    res2 = Restructurer().apply(ws, _boot_multi_profile(ws), ccfg, mode="boot_pom")
    assert res2.ok and res2.non_filtered_extensions == [] and res2.changed_files == []


# ------------------------------------------------------------------------------------------ structure review facts
def test_the_review_fact_line_reports_what_is_left(tmp_path: Path, cfg: Config) -> None:
    from sbmig.review import collect_review_input

    ws = _ws(tmp_path, {"pom.xml": _filtered_pom()})
    _binaries(ws.root)
    prefix = "- binary files under a filtered `<resource>`"

    def line() -> str:
        text = collect_review_input(ws, None, cfg).render()
        return next((ln for ln in text.splitlines() if ln.startswith(prefix)), "")

    before = line()
    assert "`src/main/resources/data/items.dmdb`" in before and "`src/main/resources/fonts/a.woff2`" in before
    apply_non_filtered_extensions(ws, binary_resource_plan(ws, cfg, None))
    after = line()
    # covered by <nonFilteredFileExtensions> now: only the extension-less file is left
    assert after and "items.dmdb" not in after and "a.woff2" not in after
    assert "`src/main/resources/data/BLOB`" in after
    (ws.root / "src/main/resources/data/BLOB").unlink()
    assert line() == ""
