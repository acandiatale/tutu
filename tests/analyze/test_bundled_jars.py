"""v2.1z-i (company run 2026-11-04): a jar in the tree that no pom declares (`WEB-INF/lib/Crypt.jar`) is a bundled
jar — `scan_bundled_jars` lists it with coordinates and every class-holding package (`provides`), `providers` answers
"which bundled jar holds package p", and `analyze_project` puts the list on `profile.bundled_jars`."""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

from sbmig.analyze import analyze_project
from sbmig.analyze.analyzer import PathView
from sbmig.analyze.bundled_jars import declared_system_jars, providers, scan_bundled_jars
from sbmig.analyze.pom_parser import parse_poms
from sbmig.core.models import JarCoordinate

from .helpers import dep, pom

WEB_POM = pom("web", packaging="war")
SEED_JAR = "src/main/webapp/WEB-INF/lib/acme-seed-1.0.jar"


def jar_bytes(classes: tuple[str, ...] = ("com/acme/seed/Seed.class",), *,
              pom_props: tuple[str, str, str] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
        for c in classes:
            z.writestr(c, b"\xca\xfe\xba\xbe")
        if pom_props:
            g, a, v = pom_props
            z.writestr(f"META-INF/maven/{g}/{a}/pom.properties", f"groupId={g}\nartifactId={a}\nversion={v}\n")
    return buf.getvalue()


def _tree(root: Path, files: dict[str, str | bytes]) -> PathView:
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(body, bytes):
            p.write_bytes(body)
        else:
            p.write_text(body, encoding="utf-8")
    return PathView(root)


def _scan(ws, cfg):
    poms, _ = parse_poms(ws)
    return scan_bundled_jars(ws, cfg, poms, lookup=False)


# ---------------------------------------------------------------------------------------------------- the scan
def test_a_web_inf_lib_jar_is_found_with_every_package(tmp_path, cfg):
    ws = _tree(tmp_path, {"pom.xml": WEB_POM, SEED_JAR: jar_bytes(
        ("com/acme/seed/Seed.class", "com/acme/seed/util/Pad.class", "com/acme/sso/Token.class"))})
    [j] = _scan(ws, cfg)
    assert j.path == SEED_JAR
    assert j.provides == ["com.acme.seed", "com.acme.seed.util", "com.acme.sso"]
    assert j.packages == ["com.acme.seed", "com.acme.sso"]            # the capped top-level display list
    assert len(j.sha1) == 40 and j.source == "unknown" and not j.group_id


def test_build_output_and_ignored_dirs_are_pruned(tmp_path, cfg):
    ws = _tree(tmp_path, {"pom.xml": WEB_POM, SEED_JAR: jar_bytes(),
                          "target/web-1.0.0/WEB-INF/lib/acme-seed-1.0.jar": jar_bytes(),
                          "bin/WEB-INF/lib/copy-1.0.jar": jar_bytes()})
    ws.ignored_dirs = {"bin"}
    assert [j.path for j in _scan(ws, cfg)] == [SEED_JAR]


def test_a_jar_a_pom_declares_with_system_scope_is_not_bundled(tmp_path, cfg):
    sys_dep = ("<dependency><groupId>com.acme</groupId><artifactId>x</artifactId><version>1</version>"
               "<scope>system</scope><systemPath>${project.basedir}/lib/x.jar</systemPath></dependency>")
    ws = _tree(tmp_path, {"pom.xml": pom("app", packaging="pom", modules=("web",)),
                          "web/pom.xml": pom("web", packaging="war", deps=(sys_dep,)),
                          "web/lib/x.jar": jar_bytes(("com/acme/x/X.class",)),
                          "web/" + SEED_JAR: jar_bytes()})
    poms, _ = parse_poms(ws)
    assert declared_system_jars(ws, poms) == {"web/lib/x.jar"}
    assert [j.path for j in scan_bundled_jars(ws, cfg, poms, lookup=False)] == ["web/" + SEED_JAR]


def test_pom_properties_give_the_coordinates(tmp_path, cfg):
    ws = _tree(tmp_path, {"pom.xml": WEB_POM,
                          "WEB-INF/lib/acme-sso-2.1.jar": jar_bytes(("com/acme/sso/Sso.class",),
                                                                    pom_props=("com.example", "acme-sso", "2.1"))})
    [j] = _scan(ws, cfg)
    assert (j.group_id, j.artifact_id, j.version, j.source) == ("com.example", "acme-sso", "2.1", "pom_properties")


def test_the_jar_name_table_gives_the_coordinates(tmp_path, cfg):
    ws = _tree(tmp_path, {"pom.xml": WEB_POM,
                          "lib/commons-lang-2.6.jar": jar_bytes(("org/apache/commons/lang/StringUtils.class",))})
    [j] = _scan(ws, cfg)
    assert (j.group_id, j.artifact_id, j.version, j.source) == ("commons-lang", "commons-lang", "2.6", "name_table")


def test_a_duplicate_sha1_is_kept_with_a_note(tmp_path, cfg):
    data = jar_bytes()
    ws = _tree(tmp_path, {"pom.xml": WEB_POM, "a/lib/seed.jar": data, "b/lib/seed.jar": data})
    a, b = _scan(ws, cfg)
    assert (a.path, b.path) == ("a/lib/seed.jar", "b/lib/seed.jar")
    assert "same jar as" not in a.note and "same jar as a/lib/seed.jar" in b.note


def test_used_is_set_from_the_java_sources(tmp_path, cfg):
    ws = _tree(tmp_path, {"pom.xml": WEB_POM, SEED_JAR: jar_bytes(),
                          "lib/other-1.0.jar": jar_bytes(("com/acme/other/O.class",)),
                          "src/main/java/com/example/web/Login.java":
                              "package com.example.web;\nimport com.acme.seed.Seed;\npublic class Login {}\n"})
    used = {j.path: j.used for j in _scan(ws, cfg)}
    assert used == {SEED_JAR: True, "lib/other-1.0.jar": False}


def test_the_switch_off_gives_nothing(tmp_path, cfg):
    ws = _tree(tmp_path, {"pom.xml": WEB_POM, SEED_JAR: jar_bytes()})
    cfg.intake.bundled_jars = False
    assert _scan(ws, cfg) == []


def test_an_unreadable_jar_never_raises(tmp_path, cfg):
    ws = _tree(tmp_path, {"pom.xml": WEB_POM, "lib/broken.jar": b"not a zip"})
    [j] = _scan(ws, cfg)
    assert j.path == "lib/broken.jar" and j.provides == []


def test_a_tree_without_jars_gives_nothing(tmp_path, cfg):
    ws = _tree(tmp_path, {"pom.xml": pom("web", deps=(dep("com.acme", "x", "1"),))})
    assert _scan(ws, cfg) == []


# ---------------------------------------------------------------------------------------------------- providers
def _jc(path: str, *provides: str) -> JarCoordinate:
    return JarCoordinate(path=path, provides=list(provides))


def test_providers_exact_match_first_then_sub_packages():
    jars = [_jc("z/exact.jar", "com.acme.seed"), _jc("a/sub.jar", "com.acme.seed.util"),
            _jc("b/exact.jar", "com.acme.seed", "com.acme.sso"), _jc("c/none.jar", "com.acme.seedling")]
    assert [j.path for j in providers(jars, "com.acme.seed")] == ["b/exact.jar", "z/exact.jar", "a/sub.jar"]


def test_providers_sub_package_only():
    assert [j.path for j in providers([_jc("a.jar", "com.acme.crypto.seed")], "com.acme.crypto")] == ["a.jar"]


def test_providers_none():
    assert providers([_jc("a.jar", "com.acme.seed")], "org.example") == []
    assert providers([], "com.acme.seed") == [] and providers([_jc("a.jar", "x")], "") == []


# ---------------------------------------------------------------------------------------------------- analyze
def test_analyze_project_fills_the_profile(tmp_path, cfg):
    ws = _tree(tmp_path, {"pom.xml": WEB_POM, SEED_JAR: jar_bytes(),
                          "src/main/java/com/example/web/Login.java":
                              "package com.example.web;\nimport com.acme.seed.Seed;\npublic class Login {}\n"})
    profile = analyze_project(ws, cfg)
    assert [j.path for j in profile.bundled_jars] == [SEED_JAR]
    assert profile.bundled_jars[0].provides == ["com.acme.seed"]


def test_analyze_project_with_the_switch_off(tmp_path, cfg):
    ws = _tree(tmp_path, {"pom.xml": WEB_POM, SEED_JAR: jar_bytes()})
    cfg.intake.bundled_jars = False
    assert analyze_project(ws, cfg).bundled_jars == []
