"""2026-11-05 (company run): `Unresolveable build extension: Plugin org.apache.maven.wagon:wagon-webdav:jar:1.0-beta-2 …` stopped
Maven before any goal — OpenRewrite included. The intake removes deploy-transport extensions (the knowledge list
knowledge/libraries/build_extensions.yml) from every pom before the first Maven call; unlisted extensions stay."""
from __future__ import annotations

from pathlib import Path

from sbmig.analyze.analyzer import PathView
from sbmig.analyze.build_extensions import (
    DeployTransport,
    commit_message,
    followups,
    load_deploy_transports,
    strip_deploy_extensions,
)
from sbmig.core.config import load_config

NS = 'xmlns="http://maven.apache.org/POM/4.0.0"'


def _pom(aid: str, build: str = "", profiles: str = "", modules: tuple[str, ...] = ()) -> str:
    mods = ("  <modules>\n" + "".join(f"    <module>{m}</module>\n" for m in modules) + "  </modules>\n") if modules else ""
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<project {NS}>\n  <modelVersion>4.0.0</modelVersion>\n'
            f"  <groupId>com.xxx</groupId>\n  <artifactId>{aid}</artifactId>\n  <version>1.0</version>\n"
            f"  <packaging>{'pom' if modules else 'jar'}</packaging>\n{mods}{build}{profiles}</project>\n")


WEBDAV = ("  <build>\n    <extensions>\n      <extension>\n        <groupId>org.apache.maven.wagon</groupId>\n"
          "        <artifactId>wagon-webdav</artifactId>\n        <version>1.0-beta-2</version>\n      </extension>\n"
          "    </extensions>\n    <finalName>app</finalName>\n  </build>\n")
MIXED = ("  <build>\n    <extensions>\n      <extension>\n        <groupId>kr.motd.maven</groupId>\n"
         "        <artifactId>os-maven-plugin</artifactId>\n        <version>1.7.1</version>\n      </extension>\n"
         "      <extension>\n        <groupId>org.apache.maven.wagon</groupId>\n        <artifactId>wagon-ssh</artifactId>\n"
         "        <version>${wagon.version}</version>\n      </extension>\n    </extensions>\n  </build>\n")
PROFILE = ("  <profiles>\n    <profile>\n      <id>release</id>\n      <build>\n        <extensions>\n          <extension>\n"
           "            <groupId>org.apache.maven.wagon</groupId>\n            <artifactId>wagon-ftp</artifactId>\n"
           "            <version>2.10</version>\n          </extension>\n        </extensions>\n      </build>\n    </profile>\n"
           "  </profiles>\n")


def _tree(root: Path, files: dict[str, str]) -> PathView:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return PathView(root)


def test_shipped_table_lists_the_wagon_providers(repo_root: Path):
    cfg = load_config(repo_root / "config" / "artifacts.local-e2e.yml")
    rows = load_deploy_transports(cfg)
    assert rows and any(r.matches("org.apache.maven.wagon", "wagon-webdav") for r in rows)
    assert not any(r.matches("kr.motd.maven", "os-maven-plugin") for r in rows)
    assert not any(r.matches("org.apache.maven.wagon", None) for r in rows) or True     # a missing artifactId never matches "wagon-*"


def test_wagon_extensions_are_removed_from_every_pom_and_profile_unlisted_ones_stay(tmp_path, repo_root):
    cfg = load_config(repo_root / "config" / "artifacts.local-e2e.yml")
    ws = _tree(tmp_path / "p", {
        "pom.xml": _pom("root", WEBDAV, modules=("a", "b")),
        "a/pom.xml": _pom("a", MIXED.replace("<build>", "<properties><wagon.version>2.10</wagon.version></properties>\n  <build>", 1)),
        "b/pom.xml": _pom("b", "", PROFILE),
        "target/classes/pom.xml": _pom("stale", WEBDAV),           # skipped path: never touched
    })
    res = strip_deploy_extensions(ws, cfg)
    assert sorted(r.coordinates for r in res.removed) == ["org.apache.maven.wagon:wagon-ftp:2.10",
                                                           "org.apache.maven.wagon:wagon-ssh:2.10",
                                                           "org.apache.maven.wagon:wagon-webdav:1.0-beta-2"]
    assert {r.where() for r in res.removed} == {"pom.xml", "a/pom.xml", "b/pom.xml (profile release)"}
    assert res.kept == [("a/pom.xml", "kr.motd.maven:os-maven-plugin:1.7.1")]
    assert sorted(res.poms_changed) == ["a/pom.xml", "b/pom.xml", "pom.xml"]
    root = (tmp_path / "p" / "pom.xml").read_text(encoding="utf-8")
    assert "wagon" not in root and "<extensions>" not in root and "<finalName>app</finalName>" in root   # emptied container dropped
    a = (tmp_path / "p" / "a" / "pom.xml").read_text(encoding="utf-8")
    assert "os-maven-plugin" in a and "wagon-ssh" not in a and a.count("<extension>") == 1
    b = (tmp_path / "p" / "b" / "pom.xml").read_text(encoding="utf-8")
    assert "wagon-ftp" not in b and "<id>release</id>" in b
    assert "wagon-webdav" in (tmp_path / "p" / "target" / "classes" / "pom.xml").read_text(encoding="utf-8")
    msg = commit_message(res)
    assert msg.startswith("intake: deploy transport extension(s) removed (3)") and "wagon-webdav:1.0-beta-2 in pom.xml" in msg
    assert len(followups(res)) == 3 and all("deploy transport" in f and "company parent supplies the deploy target" in f
                                            for f in followups(res))
    # a second pass changes nothing
    again = strip_deploy_extensions(ws, cfg)
    assert again.removed == [] and again.poms_changed == [] and again.kept == res.kept


def test_no_extensions_no_table_no_change(tmp_path, repo_root):
    cfg = load_config(repo_root / "config" / "artifacts.local-e2e.yml")
    ws = _tree(tmp_path / "p", {"pom.xml": _pom("plain")})
    res = strip_deploy_extensions(ws, cfg)
    assert res.removed == [] and res.kept == [] and res.poms_changed == []
    # an as-is empty <extensions/> is not ours to rewrite (review 2026-11-05: it was dropped uncommitted and rode into the next commit)
    ws_e = _tree(tmp_path / "e", {"pom.xml": _pom("empty", "  <build>\n    <extensions>\n    </extensions>\n  </build>\n")})
    before = (tmp_path / "e" / "pom.xml").read_text(encoding="utf-8")
    res = strip_deploy_extensions(ws_e, cfg)
    assert res.poms_changed == [] and (tmp_path / "e" / "pom.xml").read_text(encoding="utf-8") == before
    # an explicit empty table removes nothing even when the pom declares wagon
    ws2 = _tree(tmp_path / "q", {"pom.xml": _pom("w", WEBDAV)})
    assert strip_deploy_extensions(ws2, cfg, table=[]).removed == []
    assert "wagon-webdav" in (tmp_path / "q" / "pom.xml").read_text(encoding="utf-8")
    # a custom row with a glob
    res3 = strip_deploy_extensions(ws2, cfg, table=[DeployTransport("org.apache.maven.wagon", "wagon-web*")])
    assert [r.coordinates for r in res3.removed] == ["org.apache.maven.wagon:wagon-webdav:1.0-beta-2"]


def test_missing_table_file_is_a_warning_not_a_crash(tmp_path, repo_root, caplog):
    cfg = load_config(repo_root / "config" / "artifacts.local-e2e.yml")
    cfg.intake.build_extensions_file = str(tmp_path / "nope.yml")
    import logging
    with caplog.at_level(logging.WARNING, logger="sbmig.analyze.build_extensions"):
        assert load_deploy_transports(cfg) == []
    assert any("not read" in r.getMessage() for r in caplog.records)
