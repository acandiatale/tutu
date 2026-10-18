from __future__ import annotations

import pytest

from sbmig.fleet import ManifestError, load_manifest

from .conftest import make_project, make_zip


def test_directory_manifest(tmp_path):
    d = tmp_path / "p"
    make_project(d, "b-svc")
    make_project(d, "a-svc")
    make_zip(d, "c-zip")
    (d / "not-a-project").mkdir()
    (d / "notes.txt").write_text("x")
    ps = load_manifest(d)
    assert [p.name for p in ps] == ["a-svc", "b-svc", "c-zip"]
    assert all(p.ok for p in ps) and ps[2].input.endswith("c-zip.zip")


def test_txt_manifest_relative_paths_comments_and_missing(tmp_path):
    make_project(tmp_path / "src", "one")
    m = tmp_path / "list.txt"
    m.write_text("# fleet\nsrc/one   # first\n\nsrc/missing\n" + str(tmp_path / "src" / "one") + "\n")
    ps = load_manifest(m)
    assert [p.name for p in ps] == ["one", "missing", "one-2"]
    assert ps[0].ok and ps[2].ok and ps[0].input == ps[2].input
    assert not ps[1].ok and "input not found" in ps[1].error


def test_yml_manifest(tmp_path):
    make_project(tmp_path / "src", "svc")
    (tmp_path / "extra.yml").write_text("loop: {max_llm_calls: 3}\n")
    (tmp_path / "src" / "nopom").mkdir()
    m = tmp_path / "fleet.yml"
    m.write_text("""projects:
  - name: Svc One
    input: src/svc
    configs: [extra.yml]
    overrides: {kb: {use_hints: false}}
    tags: [boot, team-a]
  - input: src/nopom
  - src/svc
  - name: badcfg
    input: src/svc
    configs: missing.yml
""")
    ps = load_manifest(m)
    assert [p.name for p in ps] == ["Svc-One", "nopom", "svc", "badcfg"]
    assert ps[0].configs == [str((tmp_path / "extra.yml").resolve())]
    assert ps[0].overrides == {"kb": {"use_hints": False}} and ps[0].tags == ["boot", "team-a"]
    assert "no pom.xml" in ps[1].error and ps[2].ok
    assert "config not found" in ps[3].error
    bad = tmp_path / "bad.yml"
    bad.write_text("projects: [{name: x}]\n")
    with pytest.raises(ManifestError):
        load_manifest(bad)
    bad.write_text("foo: bar\n")
    with pytest.raises(ManifestError):
        load_manifest(bad)


def test_csv_manifest(tmp_path):
    make_project(tmp_path, "a")
    make_zip(tmp_path, "b")
    m = tmp_path / "fleet.csv"
    m.write_text("name,input,tags\nfirst,a,boot;team-x\n,b.zip,\n,,\n")
    ps = load_manifest(m)
    assert [(p.name, p.tags) for p in ps] == [("first", ["boot", "team-x"]), ("b", [])]
    assert all(p.ok for p in ps)


def test_unsupported_and_missing_manifest(tmp_path):
    with pytest.raises(ManifestError):
        load_manifest(tmp_path / "nope.yml")
    f = tmp_path / "x.json"
    f.write_text("{}")
    with pytest.raises(ManifestError):
        load_manifest(f)
    t = tmp_path / "l.txt"
    t.write_text(str(f) + "\n")
    assert "directory or a .zip" in load_manifest(t)[0].error
