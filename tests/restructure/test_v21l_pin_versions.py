"""v2.1l: dependency versions the as-is parent chain managed are carried over by the restructure (resolution
order step 2, `params["asis_effective"]`), right before the managed-version cleanup empties the ones the new
parent chain manages (step 1, `params["effective_pom"]`). Offline: both captures are plain dicts."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sbmig.core.config import Config
from sbmig.core.models import PomInfo
from sbmig.restructure import PomEditor, Restructurer

from .helpers import HEAD, CaseType, dep, java, make_ws, profile
from .test_company_layout import _boot_multi_files, _boot_multi_profile, _roles
from .test_restructurer import msa_files, msa_profile

LEGACY_PARENT = "com.company.legacy:legacy-parent:1.0.0"
BOOT2_PARENT = "org.springframework.boot:spring-boot-starter-parent:2.7.18"
TEXT = "org.apache.commons:commons-text"
WEB = "com.company.legacy:legacy-web-lib"
H2 = "com.h2database:h2"
CSV = "org.apache.commons:commons-csv"
GUAVA = "com.google.guava:guava"            # an optional in-house artifact of the e2e config (protected)
OJDBC = "com.oracle:ojdbc"
EPOLL = "io.netty:netty-transport-native-epoll"
MYSTERY = "org.unknown:mystery"
STARTER_WEB = "org.springframework.boot:spring-boot-starter-web"
LOMBOK = "org.projectlombok:lombok"
BACK = "shop-back/pom.xml"
SKIP_ASIS = "as-is version carry-over skipped: no as-is effective pom"
SKIP_AFTER = "managed-version cleanup skipped: no effective pom"


@pytest.fixture()
def ccfg(cfg: Config) -> Config:
    """The round-9c company layout with role parents (as `test_company_layout.ccfg`)."""
    L = cfg.layout
    L.module_dir, L.boot_module = "", "{name}-boot"
    L.comp_module, L.front_module = "{name}-back", "{name}-front"
    L.boot_as_is = "in-place"
    L.comp_artifact_suffix = "-back"
    return _roles(cfg)


def _legacy_files() -> dict[str, str]:
    system = "            <systemPath>${project.basedir}/lib/ojdbc.jar</systemPath>\n"
    classifier = "            <classifier>linux-x86_64</classifier>\n"
    pom = (HEAD + "    <parent>\n        <groupId>com.company.legacy</groupId>\n"
           "        <artifactId>legacy-parent</artifactId>\n        <version>1.0.0</version>\n"
           "        <relativePath/>\n    </parent>\n"
           "    <groupId>com.acme</groupId>\n    <artifactId>shop</artifactId>\n"
           "    <version>1.0.0</version>\n"
           "    <dependencies>\n"
           + dep("org.apache.commons", "commons-text")              # managed by the parent
           + dep("com.company.legacy", "legacy-web-lib")            # managed by the parent
           + dep("com.h2database", "h2", scope="runtime")           # direct only → "declared"
           + dep("com.google.guava", "guava")                       # optional → protected
           + dep("org.unknown", "mystery")                          # not in the capture
           + dep("com.oracle", "ojdbc", scope="system", extra=system)
           + dep("io.netty", "netty-transport-native-epoll", extra=classifier)   # the build decides
           + dep("org.apache.commons", "commons-csv", "1.9.0")      # explicit → never changed
           + dep("org.jsoup", "jsoup", "${jsoup.version}")          # ${…} is not version-less
           + "    </dependencies>\n</project>\n")
    return {"pom.xml": pom, "src/main/java/com/acme/shop/Main.java": java("com.acme.shop", "Main"),
            "lib/ojdbc.jar": "jar"}


def _asis() -> dict:
    managed = {TEXT: "1.10.0", WEB: "2.0.0", GUAVA: "20.0", OJDBC: "11", EPOLL: "4.1.100.Final"}
    direct = {**managed, H2: "2.2.224", CSV: "1.9.0"}
    mod = {"managed": managed, "direct": direct, "parent": LEGACY_PARENT}
    return {"managed": managed, "direct": direct, "plugins": {}, "properties": {}, "parent": LEGACY_PARENT,
            "modules": ["com.acme:shop"], "by_module": {"com.acme:shop": mod}}


def _after(managed: dict[str, str] | None = None) -> dict:
    return {"managed": {"org.springframework:spring-core": "7.0.1", **(managed or {})}, "by_module": {}}


def _inventory(ws, prof):
    """The analyzer's pom inventory of the as-is tree (`profile.poms`): what the as-is project declared."""
    poms = []
    for f in sorted(ws.root.rglob("pom.xml")):
        if ".git" in f.parts:
            continue
        ed = PomEditor(f)
        poms.append(PomInfo(path=f.relative_to(ws.root).as_posix(), artifact_id=ed.artifact_id() or "?",
                            group_id=ed.effective_group_id(), dependencies=ed.dependencies()))
    prof.poms = poms
    return prof


def _legacy_profile(ws):
    return profile(ws, CaseType.SPRING_LEGACY, artifact_id="shop", version="1.0.0",
                   base_package="com.acme.shop")


def _run(tmp_path: Path, ccfg: Config, params: dict | None, name: str = "pin"):
    ws = make_ws(tmp_path, _legacy_files(), name)
    res = Restructurer().apply(ws, _inventory(ws, _legacy_profile(ws)), ccfg, mode="layout", params=params)
    assert res.ok, res.error
    return ws, res


def _versions(ws, pom: str = BACK) -> dict[str, str | None]:
    return {d.key: d.version for d in PomEditor(ws.root / pom).dependencies()}


def _events(ws, name: str) -> list[dict]:
    if not ws.journal_path.is_file():
        return []
    lines = ws.journal_path.read_text(encoding="utf-8").splitlines()
    return [e for e in (json.loads(x) for x in lines if x.strip()) if e.get("event") == name]


# ------------------------------------------------------------------------------------------------ layout
def test_layout_carries_the_as_is_versions_over(tmp_path: Path, ccfg: Config) -> None:
    params = {"asis_effective": _asis, "effective_pom": lambda: _after()}
    ws, res = _run(tmp_path, ccfg, params)
    v = _versions(ws)
    assert v[TEXT] == "1.10.0" and v[WEB] == "2.0.0" and v[H2] == "2.2.224"
    assert v[GUAVA] == "33.4.8-jre"              # optional: the bump owns its version, not the as-is one
    assert v[OJDBC] is None                      # system scope never
    assert v[EPOLL] is None                      # classifier: left for the build to report
    assert v[MYSTERY] is None                    # unknown: left empty
    assert v[CSV] == "1.9.0" and v["org.jsoup:jsoup"] == "${jsoup.version}"
    assert res.versions_pinned == [
        f"{TEXT}: 1.10.0 ← as-is managed by {LEGACY_PARENT} ({BACK})",
        f"{WEB}: 2.0.0 ← as-is managed by {LEGACY_PARENT} ({BACK})",
        f"{H2}: 2.2.224 ← as-is declared ({BACK})"]
    # an entry the as-is project never declared (the generated boot module's starter) is not "unknown"
    assert res.versions_unknown == [f"{MYSTERY} ({BACK})"]
    assert (f"declare a version for {MYSTERY} in {BACK} or manage it in the parent — the as-is build did not "
            "resolve one") in res.manual_followups
    assert res.managed_versions_dropped == []    # a pin is not an "explicit version dropped"
    assert any(n.startswith("3 dependency version(s) carried over from the as-is build") and TEXT in n
               for n in res.notes)
    assert any(n.startswith("1 version-less dependency(ies) the as-is build did not resolve")
               for n in res.notes)
    ev = _events(ws, "versions_pinned")
    assert len(ev) == 1
    assert ev[0]["pinned"] == res.versions_pinned and ev[0]["unknown"] == res.versions_unknown
    assert BACK in res.changed_files
    # second run on the restructured tree: nothing left to pin, nothing changes
    res2 = Restructurer().apply(ws, _legacy_profile(ws), ccfg, mode="layout", params=params)
    assert res2.ok and res2.versions_pinned == [] and res2.changed_files == []


@pytest.mark.parametrize("asis", [None, lambda: None, dict, lambda: "garbage",
                                  lambda: {"managed": {}, "direct": {}, "by_module": {}, "parent": None},
                                  lambda: (_ for _ in ()).throw(RuntimeError("bad json"))],
                         ids=["missing", "none", "empty", "not-a-dict", "empty-shape", "raises"])
def test_no_as_is_capture_is_one_note_and_nothing_pinned(tmp_path: Path, ccfg: Config, asis) -> None:
    params = {"effective_pom": lambda: _after()}
    if asis is not None:
        params["asis_effective"] = asis
    ws, res = _run(tmp_path, ccfg, params)
    assert SKIP_ASIS in res.notes
    assert res.versions_pinned == [] and res.versions_unknown == []
    assert not any("declare a version for" in f for f in res.manual_followups)
    assert _versions(ws)[TEXT] is None
    assert _events(ws, "versions_pinned") == []


def test_the_new_chain_empties_the_pins_it_manages(tmp_path: Path, ccfg: Config) -> None:
    after = _after({TEXT: "1.12.0",              # newer → emptied (step 1)
                    H2: "2.1.214",               # older than the as-is one → the pin stays (no downgrade)
                    MYSTERY: "3.0"})             # the unknown one is managed → no follow-up
    ws, res = _run(tmp_path, ccfg, {"asis_effective": _asis, "effective_pom": lambda: after})
    v = _versions(ws)
    assert v[TEXT] is None and v[WEB] == "2.0.0" and v[H2] == "2.2.224"
    assert res.versions_pinned == [f"{WEB}: 2.0.0 ← as-is managed by {LEGACY_PARENT} ({BACK})",
                                   f"{H2}: 2.2.224 ← as-is declared ({BACK})"]
    assert res.managed_versions_dropped == [] and res.versions_unknown == []
    assert not any("declare a version for" in f for f in res.manual_followups)
    assert any(n.startswith("1 as-is version(s) not carried over: managed by the new parent chain")
               and TEXT in n for n in res.notes)
    # a pin kept because it is newer is a carried-over version, not an explicit as-is one
    assert (f"carried-over version newer than the parent-managed one kept: {H2} 2.2.224 (managed 2.1.214, "
            f"{BACK})") in res.notes
    assert not any(n.startswith("explicit version(s) newer") for n in res.notes)
    ev = _events(ws, "versions_pinned")[0]
    assert ev["managed_by_new_chain"] == [f"{TEXT} 1.12.0 ({BACK})"]


def test_without_the_new_chain_the_pins_are_undone(tmp_path: Path, ccfg: Config) -> None:
    """No effective pom of the restructured tree (Maven failed, e.g. on a version still missing): whether
    the new chain manages a pin is unknown → every pin of the pass is undone; the build reports the missing
    ones."""
    ws, res = _run(tmp_path, ccfg, {"asis_effective": _asis, "effective_pom": lambda: None})
    v = _versions(ws)
    assert v[TEXT] is None and v[WEB] is None and v[H2] is None
    assert res.versions_pinned == []
    assert res.versions_unknown == [f"{MYSTERY} ({BACK})"]
    assert SKIP_AFTER in res.notes
    assert any(n.startswith("as-is version carry-over undone for 3 dependency(ies)") for n in res.notes)
    assert _events(ws, "versions_pinned")[0]["undone"] == [f"{k} ({BACK})" for k in (TEXT, WEB, H2)]


def test_drop_off_still_checks_the_pins_but_never_the_explicit_versions(tmp_path: Path, ccfg: Config) -> None:
    ccfg.layout.drop_managed_versions = "off"  # type: ignore[assignment]
    calls: list[int] = []

    def after():
        calls.append(1)
        return _after({TEXT: "1.12.0", CSV: "1.14.0"})
    ws, res = _run(tmp_path, ccfg, {"asis_effective": _asis, "effective_pom": after})
    v = _versions(ws)
    assert calls == [1]
    assert v[TEXT] is None and v[WEB] == "2.0.0"  # the pin the new chain manages is emptied
    assert v[CSV] == "1.9.0"                      # off: explicit as-is versions stay
    assert res.managed_versions_dropped == []
    assert SKIP_AFTER not in res.notes


def test_drop_off_without_pins_never_captures(tmp_path: Path, ccfg: Config) -> None:
    """Only unknown ones left (nothing pinned): the capture would fail on the empty versions anyway."""
    ccfg.layout.drop_managed_versions = "off"  # type: ignore[assignment]

    def boom():
        raise AssertionError("must not be called")
    asis = {"managed": {"org.other:thing": "1.0"}, "direct": {}, "by_module": {}}
    _ws, res = _run(tmp_path, ccfg, {"asis_effective": lambda: asis, "effective_pom": boom})
    assert res.versions_pinned == []
    assert sorted(res.versions_unknown) == sorted(f"{k} ({BACK})" for k in (TEXT, WEB, H2, MYSTERY))
    assert sum(f.startswith("declare a version for") for f in res.manual_followups) == 4


# ------------------------------------------------------------------------------ repository (step 3)
REPO_KNOWS = {MYSTERY: ["0.9", "1.0.0", "1.1.0-RC1"]}


class _Repo:
    """A repository that knows MYSTERY (a prerelease newest, which is skipped)."""
    made = 0

    def __init__(self, cfg=None, *, fetch=None):
        type(self).made += 1

    def versions(self, group_id, artifact_id):
        return list(REPO_KNOWS.get(f"{group_id}:{artifact_id}", []))


@pytest.fixture()
def repo(monkeypatch):
    _Repo.made = 0
    monkeypatch.setattr("sbmig.build.repo_versions.RepositoryLookup", _Repo)
    return _Repo


REPO_PIN = (f"{MYSTERY}: 1.0.0 ← repository newest release (the as-is build did not resolve it) ({BACK})")
CONFIRM = (f"confirm {MYSTERY} 1.0.0 in {BACK}: the as-is build did not resolve a version, so sbmig took the "
           "newest release the repository has")


def test_the_repository_answers_what_the_as_is_build_did_not(tmp_path: Path, ccfg: Config, repo) -> None:
    calls: list[int] = []

    def after():
        calls.append(1)
        return _after()
    ws, res = _run(tmp_path, ccfg, {"asis_effective": _asis, "effective_pom": after})
    assert _versions(ws)[MYSTERY] == "1.0.0"
    assert res.versions_pinned[-1] == REPO_PIN and len(res.versions_pinned) == 4
    assert res.versions_unknown == [] and not any("declare a version" in f for f in res.manual_followups)
    assert CONFIRM in res.manual_followups
    assert any(n.startswith("1 dependency version(s) taken from the repository") and f"{MYSTERY} 1.0.0" in n
               for n in res.notes)
    assert calls == [1] and repo.made == 1       # one lookup per restructure call (the starter was asked too)
    assert _events(ws, "versions_pinned")[0]["from_repository"] == [REPO_PIN]


def test_a_repository_pin_the_new_chain_manages_is_emptied(tmp_path: Path, ccfg: Config, repo) -> None:
    after = _after({MYSTERY: "0.5"})             # managed, even older → the repository version is not wanted
    ws, res = _run(tmp_path, ccfg, {"asis_effective": _asis, "effective_pom": lambda: after})
    assert _versions(ws)[MYSTERY] is None
    assert REPO_PIN not in res.versions_pinned and res.versions_unknown == []
    assert CONFIRM not in res.manual_followups
    assert not any(n.startswith("1 as-is version(s) not carried over") for n in res.notes)


def test_a_repository_pin_is_undone_with_the_others(tmp_path: Path, ccfg: Config, repo) -> None:
    ws, res = _run(tmp_path, ccfg, {"asis_effective": _asis, "effective_pom": lambda: None})
    assert _versions(ws)[MYSTERY] is None and res.versions_pinned == []
    assert res.versions_unknown == [f"{MYSTERY} ({BACK})"]
    assert CONFIRM not in res.manual_followups
    assert any(f.startswith(f"declare a version for {MYSTERY}") for f in res.manual_followups)


@pytest.mark.parametrize("where", ["init", "versions"])
def test_a_broken_repository_lookup_only_means_unknown(tmp_path: Path, ccfg: Config, monkeypatch,
                                                        where) -> None:
    class Broken:
        def __init__(self, cfg=None, *, fetch=None):
            if where == "init":
                raise RuntimeError("no settings")

        def versions(self, group_id, artifact_id):
            raise OSError("socket")
    monkeypatch.setattr("sbmig.build.repo_versions.RepositoryLookup", Broken)
    after = _after({MYSTERY: "3.0"})
    ws, res = _run(tmp_path, ccfg, {"asis_effective": _asis, "effective_pom": lambda: after})
    assert _versions(ws)[MYSTERY] is None and res.versions_unknown == []      # managed → no follow-up
    assert len(res.versions_pinned) == 3


# ------------------------------------------------------------------------------ merged view (layout merge)
def test_merged_capture_prefers_the_managed_version(tmp_path: Path, ccfg: Config) -> None:
    """The as-is parent manages commons-text 1.10.0; module a declares 1.9 explicitly, module b version-less.
    b comes first in <modules> (its declaration survives the merge) while the reactor built a first (the
    merged `direct` is first-wins: 1.9) → b's pin is the managed 1.10.0."""
    root = (HEAD + "    <parent>\n        <groupId>com.company.legacy</groupId>\n"
            "        <artifactId>legacy-parent</artifactId>\n        <version>1.0.0</version>\n"
            "        <relativePath/>\n    </parent>\n"
            "    <groupId>com.acme</groupId>\n    <artifactId>suite</artifactId>\n"
            "    <version>5.0</version>\n    <packaging>pom</packaging>\n"
            "    <modules>\n        <module>b</module>\n        <module>a</module>\n    </modules>\n"
            "</project>\n")

    def child(aid: str, deps: str) -> str:
        return (HEAD + "    <parent>\n        <groupId>com.acme</groupId>\n"
                "        <artifactId>suite</artifactId>\n        <version>5.0</version>\n    </parent>\n"
                f"    <artifactId>{aid}</artifactId>\n"
                f"    <dependencies>\n{deps}    </dependencies>\n</project>\n")
    files = {"pom.xml": root,
             "a/pom.xml": child("suite-a", dep("org.apache.commons", "commons-text", "1.9")),
             "a/src/main/java/com/acme/a/A.java": java("com.acme.a", "A"),
             "b/pom.xml": child("suite-b", dep("org.apache.commons", "commons-text")),
             "b/src/main/java/com/acme/b/B.java": java("com.acme.b", "B")}
    ws = make_ws(tmp_path, files, "merged")
    prof = profile(ws, CaseType.SPRING_LEGACY, artifact_id="suite", version="5.0", packaging="pom",
                   base_package="com.acme")
    prof.is_multi_module = True
    managed = {TEXT: "1.10.0"}
    asis = {"managed": managed, "direct": {TEXT: "1.9"}, "parent": LEGACY_PARENT,
            "by_module": {"com.acme:suite": {"managed": managed, "direct": {}, "parent": LEGACY_PARENT},
                          "com.acme:suite-a": {"managed": managed, "direct": {TEXT: "1.9"},
                                               "parent": "com.acme:suite:5.0"},
                          "com.acme:suite-b": {"managed": managed, "direct": {TEXT: "1.10.0"},
                                               "parent": "com.acme:suite:5.0"}}}
    res = Restructurer().apply(ws, _inventory(ws, prof), ccfg, mode="layout",
                               params={"asis_effective": lambda: asis, "effective_pom": lambda: _after()})
    assert res.ok, res.error
    assert _versions(ws, "suite-back/pom.xml")[TEXT] == "1.10.0"
    assert res.versions_pinned == [f"{TEXT}: 1.10.0 ← as-is managed by {LEGACY_PARENT} (suite-back/pom.xml)"]


# ------------------------------------------------------------------------------------------ boot_pom / msa
def _boot_files() -> dict[str, str]:
    files = _boot_multi_files()
    end = "    </dependencies>"
    files["shop-core/pom.xml"] = files["shop-core/pom.xml"].replace(
        end, dep("org.apache.commons", "commons-text") + end)
    files["shop-api/pom.xml"] = files["shop-api/pom.xml"].replace(
        end, dep("com.acme", "shop-core") + end)                 # own module, no version
    return files


def _boot_asis() -> dict:
    root_m = {GUAVA: "31.0-jre", STARTER_WEB: "2.7.18", LOMBOK: "1.18.30", TEXT: "1.9"}
    in_repo = "com.acme:shop:3.0.0"
    core = {"managed": root_m, "parent": in_repo,
            "direct": {GUAVA: "31.0-jre", LOMBOK: "1.18.30", TEXT: "1.9"}}
    api = {"managed": root_m, "parent": in_repo,
           "direct": {STARTER_WEB: "2.7.18", LOMBOK: "1.18.30", "com.acme:shop-core": "3.0.0"}}
    boot = {**api, "direct": {STARTER_WEB: "2.7.18", LOMBOK: "1.18.30"}}
    root = {"managed": root_m, "direct": {LOMBOK: "1.18.30"}, "parent": BOOT2_PARENT}
    return {"managed": root_m, "direct": {LOMBOK: "1.18.30"}, "parent": BOOT2_PARENT,
            "by_module": {"com.acme:shop": root, "com.acme:shop-core": core, "com.acme:shop-api": api,
                          "com.acme:shop-boot": boot}}


def test_boot_pom_pins_and_the_boot_bom_empties_the_starters(tmp_path: Path, ccfg: Config) -> None:
    ws = make_ws(tmp_path, _boot_files(), "boot-pin")
    after = {"managed": {STARTER_WEB: "4.1.0", LOMBOK: "1.18.42"}, "by_module": {}}
    params = {"asis_effective": _boot_asis, "effective_pom": lambda: after}
    res = Restructurer().apply(ws, _inventory(ws, _boot_multi_profile(ws)), ccfg, mode="boot_pom",
                               params=params)
    assert res.ok, res.error
    core, api = _versions(ws, "shop-core/pom.xml"), _versions(ws, "shop-api/pom.xml")
    # the in-repo parent com.acme:shop is followed to the Boot 2 parent the managed version came from
    assert core[TEXT] == "1.9"
    assert res.versions_pinned == [f"{TEXT}: 1.9 ← as-is managed by {BOOT2_PARENT} (shop-core/pom.xml)"]
    assert core[GUAVA] is None                   # managed inside the project's pom (dependencyManagement)
    assert api[STARTER_WEB] is None              # pinned 2.7.18, emptied: the new chain manages 4.1.0
    assert api["com.acme:shop-core"] is None     # own module: the reactor resolves it
    assert res.managed_versions_dropped == [] and res.versions_unknown == []
    # second run: the starters are pinned and emptied again, the poms come back byte for byte
    res2 = Restructurer().apply(ws, _inventory(ws, _boot_multi_profile(ws)), ccfg, mode="boot_pom",
                                params=params)
    assert res2.ok and res2.changed_files == [] and res2.versions_pinned == []
    # shop-api + shop-boot, named once
    assert f"2 as-is version(s) not carried over: managed by the new parent chain ({STARTER_WEB} 4.1.0)" \
        in res2.notes


def test_msa_pom_pins_per_service(tmp_path: Path, ccfg: Config) -> None:
    files = msa_files()
    end = "    </dependencies>"
    files["user-service/pom.xml"] = files["user-service/pom.xml"].replace(
        end, dep("org.apache.commons", "commons-text") + end)
    ws = make_ws(tmp_path, files, "msa-pin")
    svc_parent = "org.springframework.boot:spring-boot-starter-parent:3.2.0"
    user = {"managed": {STARTER_WEB: "3.2.0", TEXT: "1.10.0"}, "parent": svc_parent,
            "direct": {STARTER_WEB: "3.2.0", TEXT: "1.10.0"}}
    order = {"managed": {STARTER_WEB: "3.2.0"}, "direct": {STARTER_WEB: "3.2.0"}, "parent": svc_parent}
    asis = {"managed": {STARTER_WEB: "3.2.0"}, "direct": {STARTER_WEB: "3.2.0"}, "parent": None,
            "by_module": {"com.acme:user-service": user, "com.acme:order-service": order}}
    after = {"managed": {STARTER_WEB: "4.1.0"}, "by_module": {}}
    res = Restructurer().apply(ws, _inventory(ws, msa_profile(ws)), ccfg, mode="msa_pom",
                               params={"asis_effective": lambda: asis, "effective_pom": lambda: after})
    assert res.ok, res.error
    got = _versions(ws, "user-service/pom.xml")
    assert got[TEXT] == "1.10.0" and got[STARTER_WEB] is None
    assert _versions(ws, "order-service/pom.xml")[STARTER_WEB] is None
    assert res.versions_pinned == [f"{TEXT}: 1.10.0 ← as-is managed by {svc_parent} (user-service/pom.xml)"]
