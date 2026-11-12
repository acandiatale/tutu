"""Tests for sbmig.remediations.intake: ZIP validation, layout, hashes, copy, delivery freeze, boundary checks."""
from __future__ import annotations

import json
import os
import stat
import struct
import types
import warnings
import zipfile
from pathlib import Path

import pytest

from sbmig.core.config import RemediationConfig
from sbmig.remediations import intake
from sbmig.remediations.interfaces import IntakeFns
from sbmig.remediations.models import IntakeLimits, IntakeRejected, RemediationError, RunPaths, StateConflict

POM = b"<project><modelVersion>4.0.0</modelVersion><groupId>example</groupId><artifactId>lib</artifactId></project>"


# ---------------------------------------------------------------------------- helpers
def make_zip(path: Path, entries: list, *, warn_ok: bool = False) -> Path:
    """entries: (name, data) or (ZipInfo, data)."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for name, data in entries:
                zf.writestr(name, data)
    return path


def patch_names(path: Path, old: bytes, new: bytes) -> None:
    raw = path.read_bytes()
    assert len(old) == len(new) and old in raw
    path.write_bytes(raw.replace(old, new))


def patch_header_field(path: Path, *, local_off: int, central_off: int, fmt: str, value: int) -> None:
    raw = bytearray(path.read_bytes())
    size = struct.calcsize(fmt)
    i = raw.find(b"PK\x03\x04")
    raw[i + local_off:i + local_off + size] = struct.pack(fmt, value)
    j = raw.find(b"PK\x01\x02")
    raw[j + central_off:j + central_off + size] = struct.pack(fmt, value)
    path.write_bytes(bytes(raw))


def info(name: str, mode: int) -> zipfile.ZipInfo:
    zi = zipfile.ZipInfo(name)
    zi.create_system = 3
    zi.external_attr = mode << 16
    zi.compress_type = zipfile.ZIP_DEFLATED
    return zi


def cfg(**kw):
    return types.SimpleNamespace(remediation=RemediationConfig(**kw))


def reject_code(tmp_path: Path, zip_file: Path, limits: IntakeLimits | None = None) -> str:
    run = tmp_path / "run"
    with pytest.raises(IntakeRejected) as ei:
        intake.accept_zip(zip_file, run, limits or IntakeLimits())
    assert not (run / "accepted").exists()
    assert not (run / ".accepted.partial").exists()
    assert not (run / "original" / "manifest.json").exists()
    assert not (run / "original" / "source.zip").exists()
    return ei.value.code


# ---------------------------------------------------------------------------- config / protocol
def test_module_satisfies_intake_protocol():
    for name in ("manifest_paths", "tree_hash", "check_boundary", "copy_tree", "freeze_delivery"):
        assert callable(getattr(intake, name))
    _: IntakeFns = intake  # type: ignore[assignment]


def test_limits_from_config_converts_mib():
    lim = intake.limits_from_config(cfg(max_upload_mb=2, max_entries=7, max_unpacked_mb=3, max_entry_mb=1,
                                        max_compression_ratio=50))
    assert lim == IntakeLimits(max_zip_bytes=2 * 1024 * 1024, max_entries=7, max_unpacked_bytes=3 * 1024 * 1024,
                               max_entry_bytes=1024 * 1024, max_compression_ratio=50)


# ---------------------------------------------------------------------------- accepted layouts
def test_accept_root_pom(tmp_path):
    z = make_zip(tmp_path / "up.zip", [("pom.xml", POM), ("src/main/java/A.java", b"class A {}"), ("src/empty/", b"")])
    run = tmp_path / "run"
    res = intake.accept_zip(z, run, IntakeLimits())
    rp = RunPaths(run)
    assert res.project_root == "."
    assert res.original_zip == "original/source.zip" and res.manifest == "original/manifest.json"
    assert res.accepted_dir == "accepted"
    assert res.files == 2 and res.unpacked_bytes == len(POM) + len(b"class A {}")
    assert (rp.accepted / "pom.xml").read_bytes() == POM
    assert (rp.accepted / "src/empty").is_dir()
    # original byte-for-byte and read-only
    assert rp.original_zip.read_bytes() == z.read_bytes()
    assert not (rp.original_zip.stat().st_mode & 0o222)
    assert z.exists()  # the upload itself is left to the caller
    # manifest
    m = json.loads(rp.manifest.read_text())
    assert m["schema"] == 1 and m["zip_sha256"] == res.zip_sha256
    assert [e["path"] for e in m["entries"]] == ["pom.xml", "src/main/java/A.java"]
    import hashlib
    assert m["entries"][0] == {"path": "pom.xml", "size": len(POM), "sha256": hashlib.sha256(POM).hexdigest()}
    assert res.zip_sha256 == hashlib.sha256(z.read_bytes()).hexdigest()
    assert intake.manifest_paths(rp.manifest) == ["pom.xml", "src/main/java/A.java"]
    assert res.tree_hash == intake.tree_hash(rp.accepted, intake.manifest_paths(rp.manifest))


def test_accept_single_wrapped_directory_with_macos_noise(tmp_path):
    z = make_zip(tmp_path / "up.zip", [("app/pom.xml", POM), ("app/src/A.java", b"x"),
                                       ("__MACOSX/app/._pom.xml", b"meta"), (".DS_Store", b"ds")])
    res = intake.accept_zip(z, tmp_path / "run", IntakeLimits())
    assert res.project_root == "app"
    paths = intake.manifest_paths(RunPaths(tmp_path / "run").manifest)
    assert "__MACOSX/app/._pom.xml" in paths and ".DS_Store" in paths  # noise kept consistently
    assert res.notes


def test_accept_in_place_original(tmp_path):
    run = tmp_path / "run"
    rp = RunPaths(run)
    rp.original_zip.parent.mkdir(parents=True)
    make_zip(rp.original_zip, [("pom.xml", POM)])
    res = intake.accept_zip(rp.original_zip, run, IntakeLimits())
    assert res.files == 1 and rp.original_zip.exists()


def test_accept_refuses_existing_accepted(tmp_path):
    z = make_zip(tmp_path / "up.zip", [("pom.xml", POM)])
    (tmp_path / "run" / "accepted").mkdir(parents=True)
    with pytest.raises(StateConflict):
        intake.accept_zip(z, tmp_path / "run", IntakeLimits())


def test_safe_modes_exec_bit_only(tmp_path):
    z = make_zip(tmp_path / "up.zip", [("pom.xml", POM), (info("run.sh", stat.S_IFREG | 0o4777), b"#!/bin/sh"),
                                       (info("data.txt", stat.S_IFREG | 0o666), b"d")])
    intake.accept_zip(z, tmp_path / "run", IntakeLimits())
    acc = tmp_path / "run" / "accepted"
    assert stat.S_IMODE((acc / "run.sh").stat().st_mode) == 0o755
    assert stat.S_IMODE((acc / "data.txt").stat().st_mode) == 0o644


# ---------------------------------------------------------------------------- rejections
def test_reject_not_zip(tmp_path):
    p = tmp_path / "up.zip"
    p.write_bytes(b"this is not a zip")
    assert reject_code(tmp_path, p) == "not_zip"


def test_reject_empty(tmp_path):
    assert reject_code(tmp_path, make_zip(tmp_path / "e.zip", [])) == "empty"


def test_reject_only_directories_is_empty(tmp_path):
    assert reject_code(tmp_path, make_zip(tmp_path / "e.zip", [("a/", b"")])) == "empty"


def test_reject_encrypted(tmp_path):
    z = make_zip(tmp_path / "up.zip", [("pom.xml", POM)])
    patch_header_field(z, local_off=6, central_off=8, fmt="<H", value=0x1)
    assert reject_code(tmp_path, z) == "encrypted"


@pytest.mark.parametrize("name,code", [
    ("/etc/passwd", "absolute_path"),
    ("C:/x/pom.xml", "absolute_path"),
    ("\\evil", "absolute_path"),
    ("a/../../evil", "path_traversal"),
    ("../evil", "path_traversal"),
    ("a\\..\\evil", "path_traversal"),
    ("a//b", "path_traversal"),
    ("./a", "path_traversal"),
    ("a/.../b", "path_traversal"),
    ("a/b\x01c", "path_traversal"),
])
def test_reject_bad_names(tmp_path, name, code):
    z = make_zip(tmp_path / "up.zip", [("pom.xml", POM), (name, b"x")])
    assert reject_code(tmp_path, z) == code


def test_reject_nul_in_name(tmp_path):
    z = make_zip(tmp_path / "up.zip", [("pom.xml", POM), ("aXb.txt", b"x")])
    patch_names(z, b"aXb.txt", b"a\x00b.txt")
    assert reject_code(tmp_path, z) == "path_traversal"


def test_reject_symlink(tmp_path):
    z = make_zip(tmp_path / "up.zip", [("pom.xml", POM), (info("link", stat.S_IFLNK | 0o777), b"/etc/passwd")])
    assert reject_code(tmp_path, z) == "symlink"


@pytest.mark.parametrize("ftype", [stat.S_IFIFO, stat.S_IFCHR, stat.S_IFBLK, stat.S_IFSOCK])
def test_reject_special_file(tmp_path, ftype):
    z = make_zip(tmp_path / "up.zip", [("pom.xml", POM), (info("dev", ftype | 0o644), b"")])
    assert reject_code(tmp_path, z) == "special_file"


def test_reject_duplicate_entry(tmp_path):
    z = make_zip(tmp_path / "up.zip", [("pom.xml", POM), ("a.txt", b"1"), ("a.txt", b"2")])
    assert reject_code(tmp_path, z) == "duplicate_entry"


@pytest.mark.parametrize("names", [
    ["Readme.txt", "README.txt"],
    ["caf\u00e9.txt", "cafe\u0301.txt"],
    ["a.", "a"],
    ["a ", "a"],
    ["x", "x/y"],
    ["Src/A.java", "src/B.java"],
])
def test_reject_normalized_collision(tmp_path, names):
    z = make_zip(tmp_path / "up.zip", [("pom.xml", POM)] + [(n, b"x") for n in names])
    assert reject_code(tmp_path, z) == "normalized_collision"


def test_reject_file_and_directory_same_name(tmp_path):
    z = make_zip(tmp_path / "up.zip", [("pom.xml", POM), ("x", b"1"), ("x/", b"")])
    assert reject_code(tmp_path, z) == "normalized_collision"


def test_reject_too_many_entries(tmp_path):
    z = make_zip(tmp_path / "up.zip", [("pom.xml", POM), ("a", b"1"), ("b", b"2"), ("c", b"3")])
    assert reject_code(tmp_path, z, IntakeLimits(max_entries=3)) == "too_many_entries"


def test_reject_zip_too_large(tmp_path):
    z = make_zip(tmp_path / "up.zip", [("pom.xml", POM)])
    assert reject_code(tmp_path, z, IntakeLimits(max_zip_bytes=10)) == "too_large"


def test_reject_entry_too_large(tmp_path):
    z = make_zip(tmp_path / "up.zip", [("pom.xml", POM), ("big.bin", os.urandom(5000))])
    assert reject_code(tmp_path, z, IntakeLimits(max_entry_bytes=4000)) == "too_large"


def test_reject_total_too_large(tmp_path):
    z = make_zip(tmp_path / "up.zip", [("pom.xml", POM), ("a.bin", os.urandom(3000)), ("b.bin", os.urandom(3000))])
    assert reject_code(tmp_path, z, IntakeLimits(max_unpacked_bytes=5000)) == "too_large"


def test_reject_compression_ratio(tmp_path):
    z = make_zip(tmp_path / "up.zip", [("pom.xml", POM), ("zeros.bin", b"\0" * (2 * 1024 * 1024))])
    assert reject_code(tmp_path, z) == "compression_ratio"


def test_small_entries_ignore_ratio(tmp_path):
    # 512 KiB of zeros: ratio is huge but under RATIO_MIN_BYTES
    z = make_zip(tmp_path / "up.zip", [("pom.xml", POM), ("zeros.bin", b"\0" * (512 * 1024))])
    assert intake.accept_zip(z, tmp_path / "run", IntakeLimits()).files == 2


def test_reject_lying_declared_size(tmp_path):
    # the central directory claims 10 bytes; the stream holds much more → CRC / size mismatch while extracting
    z = make_zip(tmp_path / "up.zip", [("a.bin", os.urandom(4000)), ("pom.xml", POM)])
    patch_header_field(z, local_off=22, central_off=24, fmt="<I", value=10)
    assert reject_code(tmp_path, z) in ("not_zip", "too_large")


def test_extract_enforces_actual_bytes(tmp_path):
    z = make_zip(tmp_path / "up.zip", [("pom.xml", POM), ("a.bin", os.urandom(3000))])
    with zipfile.ZipFile(z) as zf:
        files = [(i, i.filename) for i in zf.infolist()]
        with pytest.raises(IntakeRejected) as ei:
            intake._extract(zf, files, [], tmp_path / "dest", IntakeLimits(max_unpacked_bytes=1000))
    assert ei.value.code == "too_large"


@pytest.mark.parametrize("entries", [
    [("src/A.java", b"x")],
    [("a/pom.xml", POM), ("b/pom.xml", POM)],
    [("app/src/A.java", b"x")],
    [("app/sub/pom.xml", POM)],
    [("Pom.xml", POM)],
])
def test_reject_no_pom(tmp_path, entries):
    assert reject_code(tmp_path, make_zip(tmp_path / "up.zip", entries)) == "no_pom"


# ---------------------------------------------------------------------------- tree hash / copy
def _accepted(tmp_path) -> tuple[Path, list[str]]:
    z = make_zip(tmp_path / "up.zip", [("pom.xml", POM), ("src/A.java", b"a"), ("mvnw", b"#!")])
    intake.accept_zip(z, tmp_path / "run", IntakeLimits())
    rp = RunPaths(tmp_path / "run")
    return rp.accepted, intake.manifest_paths(rp.manifest)


def test_tree_hash_stable_and_ignores_extra_files(tmp_path):
    acc, paths = _accepted(tmp_path)
    h1 = intake.tree_hash(acc, paths)
    assert h1 == intake.tree_hash(acc, list(reversed(paths)))
    (acc / "target").mkdir()
    (acc / "target" / "app.jar").write_bytes(b"built")
    assert intake.tree_hash(acc, paths) == h1
    (acc / "src/A.java").write_bytes(b"changed")
    assert intake.tree_hash(acc, paths) != h1


def test_tree_hash_missing_file(tmp_path):
    acc, paths = _accepted(tmp_path)
    h1 = intake.tree_hash(acc, paths)
    os.remove(acc / "src/A.java")
    h2 = intake.tree_hash(acc, paths)
    assert h2 != h1
    (acc / "src/A.java").write_bytes(b"")
    assert intake.tree_hash(acc, paths) not in (h1, h2)


def test_copy_tree_copies_only_manifest_paths(tmp_path):
    acc, paths = _accepted(tmp_path)
    (acc / "target").mkdir()
    (acc / "target" / "x.class").write_bytes(b"c")
    dst = tmp_path / "cand"
    intake.copy_tree(acc, dst, paths)
    got = sorted(p.relative_to(dst).as_posix() for p in dst.rglob("*") if p.is_file())
    assert got == paths
    assert intake.tree_hash(dst, paths) == intake.tree_hash(acc, paths)


def test_copy_tree_requires_fresh_destination(tmp_path):
    acc, paths = _accepted(tmp_path)
    dst = tmp_path / "cand"
    dst.mkdir()
    (dst / "junk").write_text("x")
    with pytest.raises(StateConflict):
        intake.copy_tree(acc, dst, paths)


def test_copy_tree_refuses_symlink(tmp_path):
    acc, paths = _accepted(tmp_path)
    os.remove(acc / "src/A.java")
    os.symlink(tmp_path / "up.zip", acc / "src/A.java")
    with pytest.raises(RemediationError) as ei:
        intake.copy_tree(acc, tmp_path / "cand", paths)
    assert ei.value.code == "symlink"


def test_copy_tree_refuses_symlinked_directory(tmp_path):
    acc, paths = _accepted(tmp_path)
    real = tmp_path / "elsewhere"
    (acc / "src").rename(real)
    os.symlink(real, acc / "src")
    with pytest.raises(RemediationError) as ei:
        intake.copy_tree(acc, tmp_path / "cand", paths)
    assert ei.value.code == "symlink"


# ---------------------------------------------------------------------------- delivery
def test_freeze_delivery_deterministic(tmp_path):
    acc, paths = _accepted(tmp_path)
    rp = RunPaths(tmp_path / "run")
    (acc / "target").mkdir()
    (acc / "target" / "app.jar").write_bytes(b"built")
    d1 = intake.freeze_delivery(acc, rp.delivery_zip, 1, paths)
    b1 = rp.delivery_zip.read_bytes()
    os.utime(acc / "pom.xml", (1_700_000_000, 1_700_000_000))
    d2 = intake.freeze_delivery(acc, rp.delivery_zip, 1, paths)
    assert d1.sha256 == d2.sha256 and rp.delivery_zip.read_bytes() == b1
    assert d1.path == "delivery/result.zip" and d1.files == len(paths) and d1.round == 1
    assert d1.size == len(b1)
    assert d1.tree_hash == intake.tree_hash(acc, paths)
    assert intake.verify_zip_sha256(rp.delivery_zip, d1.sha256)
    assert intake.verify_zip_sha256(rp.delivery_zip, d1.sha256.upper())
    assert not intake.verify_zip_sha256(rp.delivery_zip, "0" * 64)
    assert not intake.verify_zip_sha256(tmp_path / "nope.zip", d1.sha256)
    with zipfile.ZipFile(rp.delivery_zip) as zf:
        assert zf.namelist() == sorted(paths)
        assert all(i.date_time == (1980, 1, 1, 0, 0, 0) for i in zf.infolist())
        assert zf.read("pom.xml") == POM
    # a copy of the tree gives the same bytes
    other = tmp_path / "copy"
    intake.copy_tree(acc, other, paths)
    d3 = intake.freeze_delivery(other, tmp_path / "x" / "result.zip", 1, paths)
    assert d3.sha256 == d1.sha256


@pytest.fixture
def windows_fsync(monkeypatch):
    """Windows' rule for `os.fsync` (`_commit`): a descriptor opened read-only is refused with EBADF. POSIX accepts
    it, so the delivery freeze passed here and failed on the company PC (2026-11-12: `OSError: [Errno 9] Bad file
    descriptor`, `freeze_delivery` failed)."""
    import errno
    import fcntl

    real = os.fsync

    def fsync(fd):
        if fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY:
            raise OSError(errno.EBADF, "Bad file descriptor")
        return real(fd)

    monkeypatch.setattr(os, "fsync", fsync)


def test_freeze_delivery_syncs_through_a_writable_descriptor(tmp_path, windows_fsync):
    acc, paths = _accepted(tmp_path)
    rp = RunPaths(tmp_path / "run")
    d = intake.freeze_delivery(acc, rp.delivery_zip, 1, paths)
    assert intake.verify_zip_sha256(rp.delivery_zip, d.sha256)
    assert not [n for n in os.listdir(rp.delivery_zip.parent) if n.startswith(".")]      # no temp file left


def test_accept_zip_and_state_writes_sync_through_writable_descriptors(tmp_path, windows_fsync):
    from sbmig.remediations.models import write_json_atomic

    z = make_zip(tmp_path / "up.zip", [("app/pom.xml", POM), ("app/src/A.java", b"a")])
    intake.accept_zip(z, tmp_path / "run", IntakeLimits())
    write_json_atomic(tmp_path / "run" / "state.json", {"a": 1})
    assert json.loads((tmp_path / "run" / "state.json").read_text(encoding="utf-8")) == {"a": 1}


def test_freeze_delivery_file_set_equals_original(tmp_path):
    z = make_zip(tmp_path / "up.zip", [("app/pom.xml", POM), ("app/src/A.java", b"a"), ("app/web/x.jsp", b"j")])
    intake.accept_zip(z, tmp_path / "run", IntakeLimits())
    rp = RunPaths(tmp_path / "run")
    paths = intake.manifest_paths(rp.manifest)
    intake.freeze_delivery(rp.accepted, rp.delivery_zip, 0, paths)
    with zipfile.ZipFile(z) as a, zipfile.ZipFile(rp.delivery_zip) as b:
        assert sorted(n for n in a.namelist() if not n.endswith("/")) == sorted(b.namelist())


def test_freeze_delivery_refuses_symlink(tmp_path):
    acc, paths = _accepted(tmp_path)
    os.remove(acc / "mvnw")
    os.symlink(acc / "pom.xml", acc / "mvnw")
    out = tmp_path / "d" / "result.zip"
    with pytest.raises(RemediationError):
        intake.freeze_delivery(acc, out, 0, paths)
    assert not out.exists() and not any(out.parent.iterdir())


# ---------------------------------------------------------------------------- boundary
def _project(tmp_path: Path, files: dict[str, str | bytes]) -> Path:
    root = tmp_path / "proj"
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data if isinstance(data, bytes) else data.encode())
    return root


def _codes(findings) -> dict[tuple[str, str], str]:
    return {(f.code, f.path): f.action for f in findings}


def pom_with(repos: str, props: str = "", ns: bool = True) -> str:
    xmlns = ' xmlns="http://maven.apache.org/POM/4.0.0"' if ns else ""
    return f"<project{xmlns}><modelVersion>4.0.0</modelVersion><properties>{props}</properties>{repos}</project>"


def test_boundary_clean_project(tmp_path):
    proj = _project(tmp_path, {"pom.xml": pom_with(
        "<repositories><repository><id>n</id><url>https://nexus.invalid/repo</url></repository></repositories>"
        "<distributionManagement><repository><id>d</id><url>https://other.invalid/r</url></repository>"
        "</distributionManagement>")})
    assert intake.check_boundary(proj, cfg(), ["nexus.invalid"]) == []


@pytest.mark.parametrize("action", ["refuse", "ignore"])
def test_boundary_project_maven_config(tmp_path, action):
    proj = _project(tmp_path, {"pom.xml": POM, ".mvn/maven.config": "-s x", ".mvn/jvm.config": "-Xmx1g",
                               ".mvn/extensions.xml": "<extensions/>"})
    got = _codes(intake.check_boundary(proj, cfg(project_maven_config=action), []))
    assert got == {("project_maven_config", ".mvn/maven.config"): action,
                   ("project_maven_config", ".mvn/jvm.config"): action,
                   ("maven_extensions", ".mvn/extensions.xml"): action}


@pytest.mark.parametrize("action", ["refuse", "ignore"])
def test_boundary_maven_wrapper(tmp_path, action):
    proj = _project(tmp_path, {"pom.xml": POM, "mvnw": "#!", "mvnw.cmd": "@echo",
                               ".mvn/wrapper/maven-wrapper.properties": "distributionUrl=x"})
    got = _codes(intake.check_boundary(proj, cfg(maven_wrapper=action), []))
    assert got == {("maven_wrapper", "mvnw"): action, ("maven_wrapper", "mvnw.cmd"): action,
                   ("maven_wrapper", ".mvn/wrapper/maven-wrapper.properties"): action}


def test_boundary_symlink_always_refused(tmp_path):
    proj = _project(tmp_path, {"pom.xml": POM, "src/A.java": "x"})
    os.symlink(proj / "src/A.java", proj / "src/B.java")
    os.symlink(tmp_path, proj / "linkdir")
    got = _codes(intake.check_boundary(proj, cfg(project_maven_config="ignore", maven_wrapper="ignore",
                                                 foreign_repository="ignore"), []))
    assert got == {("symlink", "src/B.java"): "refuse", ("symlink", "linkdir"): "refuse"}


def test_boundary_settings_in_project_ignored(tmp_path):
    proj = _project(tmp_path, {"pom.xml": POM, "settings.xml": "<settings/>", "conf/settings-security.xml": "<s/>"})
    got = _codes(intake.check_boundary(proj, cfg(), []))
    assert got == {("settings_in_project", "settings.xml"): "ignore",
                   ("settings_in_project", "conf/settings-security.xml"): "ignore"}


@pytest.mark.parametrize("action", ["refuse", "ignore"])
def test_boundary_foreign_repositories(tmp_path, action):
    proj = _project(tmp_path, {
        "pom.xml": pom_with("<repositories><repository><id>ext</id><url>http://user:secret@evil.invalid/r?token=t"
                            "</url></repository></repositories>"),
        "mod/pom.xml": pom_with("<profiles><profile><pluginRepositories><pluginRepository><id>p</id>"
                                "<url>https://plugins.invalid/r</url></pluginRepository></pluginRepositories>"
                                "</profile></profiles>", ns=False)})
    fs = intake.check_boundary(proj, cfg(foreign_repository=action), ["nexus.invalid"])
    assert _codes(fs) == {("foreign_repository", "pom.xml"): action,
                          ("foreign_plugin_repository", "mod/pom.xml"): action}
    detail = next(f.detail for f in fs if f.path == "pom.xml")
    assert "non-https" in detail and "evil.invalid" in detail
    assert "secret" not in detail and "token" not in detail and "user" not in detail


def test_boundary_allowed_host_matching(tmp_path):
    proj = _project(tmp_path, {"pom.xml": pom_with(
        "<repositories><repository><id>n</id><url>https://NEXUS.invalid:8443/repo</url></repository></repositories>")})
    assert intake.check_boundary(proj, cfg(), ["https://nexus.invalid/"]) == []
    assert intake.check_boundary(proj, cfg(), ["nexus.invalid:8443"]) == []
    assert len(intake.check_boundary(proj, cfg(), ["other.invalid"])) == 1


def test_boundary_property_urls(tmp_path):
    proj = _project(tmp_path, {
        "pom.xml": pom_with("<repositories><repository><id>a</id><url>${repo.url}/x</url></repository>"
                            "<repository><id>b</id><url>${missing.url}</url></repository></repositories>",
                            props="<repo.url>https://${repo.host}</repo.url><repo.host>nexus.invalid</repo.host>")})
    fs = intake.check_boundary(proj, cfg(), ["nexus.invalid"])
    assert len(fs) == 1 and fs[0].code == "foreign_repository" and "unresolved property" in fs[0].detail
    assert fs[0].action == "refuse"


def test_boundary_unparsable_pom(tmp_path):
    proj = _project(tmp_path, {"pom.xml": POM, "bad/pom.xml": "<project><unclosed></project>"})
    got = _codes(intake.check_boundary(proj, cfg(foreign_repository="ignore"), []))
    assert got == {("unparsable_pom", "bad/pom.xml"): "refuse"}


def test_boundary_does_not_resolve_external_entities(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("evil.invalid")
    xml = (f'<!DOCTYPE project [<!ENTITY h SYSTEM "file://{secret}">]>'
           "<project><repositories><repository><id>x</id><url>https://&h;/r</url></repository></repositories></project>")
    proj = _project(tmp_path, {"pom.xml": xml})
    fs = intake.check_boundary(proj, cfg(), ["evil.invalid"])
    # the entity is not expanded, so the host is never "evil.invalid" (approved) — it stays a finding
    assert all("evil.invalid" not in f.detail or f.code == "unparsable_pom" for f in fs)
    assert fs
