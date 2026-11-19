"""Poms saved in MS949 / EUC-KR through every remediation step that reads or writes a pom.

What "Maven accepts" means here was measured with the real Maven 3.9.10 on JDK 21 (`mvn -B -ntp -o validate` and
`package`), on a one-module jar project whose pom holds Korean in a comment and in `<name>`, saved as MS949:

| pom header                                   | validate / package | what Maven prints                         |
|----------------------------------------------|--------------------|-------------------------------------------|
| (a) `encoding="UTF-8"`                       | pass / pass        | no warning; `Building ��� ...` (mojibake) |
| (b) no XML declaration                       | pass / pass        | no warning; mojibake name                 |
| (c) `encoding="EUC-KR"`                      | pass / pass        | no warning; Korean name correct           |
| (d) `encoding="MS949"`                       | pass / pass        | no warning; Korean name correct           |
| (e) `encoding="x-windows-949"`               | pass / pass        | no warning; Korean name correct           |
| `encoding="CP949"`                           | pass / pass        | no warning; Korean name correct           |
| `encoding="EUC-KR"`, an MS949-only syllable  | pass / pass        | no warning; that syllable replaced        |
| `encoding="ISO-8859-1"`, MS949 bytes         | pass / pass        | no warning; Latin-1 mojibake              |
| `encoding="UTF-8"`, bytes valid in no codec  | pass / pass        | no warning                                |
| module pom (a) inside a UTF-8 reactor        | pass / pass        | no warning; mojibake module name          |
| `encoding="no-such-enc"`                     | FAIL               | `Non-readable POM …: NO-SUCH-ENC`         |
| truncated XML                                | FAIL               | `Non-readable POM …: no more data …`      |

So Maven never refuses a pom for its bytes: it decodes leniently and replaces what it cannot decode. It refuses
malformed XML and an encoding name Java does not know. The remediation therefore reads a pom with the migration's
rule (`sbmig.analyze.pom_parser.pom_encoding`: BOM, strict UTF-8, the declared encoding with the Korean family read
as cp949, then utf-8 / cp949 / euc-kr / shift_jis / latin-1) and refuses only malformed XML, a file it cannot open,
or bytes that do not decode in the encoding chosen (a broken UTF-16 file), saying which in words.
"""
from __future__ import annotations

import os
import types
import zipfile
from pathlib import Path

import pytest

from sbmig.core.config import RemediationConfig
from sbmig.remediations import apply as A
from sbmig.remediations import i18n, intake, planner
from sbmig.remediations.inventory import build_inventory, parse_pom_file
from sbmig.remediations.models import K_DIRECT_VERSION, O_DIRECT_LITERAL, PomLocation, Proposal, TextEdit

from .test_inventory import finding, model, rdep

KOREAN = "결제 모듈"
DECLS = {"utf8": 'encoding="UTF-8"', "none": None, "euckr": 'encoding="EUC-KR"', "ms949": 'encoding="MS949"',
         "xwin949": 'encoding="x-windows-949"', "cp949": 'encoding="CP949"'}


def korean_pom(decl: str | None = 'encoding="UTF-8"', *, repos: str = "", version: str = "1.2.3") -> bytes:
    """A pom with Korean in a comment and in <name>, saved as MS949 (cp949); the declaration is ASCII."""
    head = f'<?xml version="1.0" {decl}?>\n' if decl else ""
    body = (
        f"<!-- 한글 주석: {KOREAN} -->\n"
        '<project xmlns="http://maven.apache.org/POM/4.0.0">\n'
        "  <modelVersion>4.0.0</modelVersion>\n"
        "  <groupId>example</groupId>\n"
        "  <artifactId>app</artifactId>\n"
        "  <version>1.0.0</version>\n"
        "  <name>한글 이름 똠방각하</name>\n"
        f"{repos}"
        "  <dependencies>\n"
        "    <dependency>\n"
        "      <groupId>example</groupId>\n"
        "      <artifactId>lib</artifactId>\n"
        f"      <version>{version}</version>\n"
        "    </dependency>\n"
        "  </dependencies>\n"
        "</project>\n")
    return head.encode("ascii") + body.encode("cp949")


VERSION_LINE = 13          # "<version>1.2.3</version>" of example:lib with a declaration line


def cfg(**kw):
    return types.SimpleNamespace(remediation=RemediationConfig(**kw))


def project(tmp_path: Path, files: dict[str, bytes]) -> Path:
    root = tmp_path / "proj"
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return root


def test_fixture_is_really_ms949_under_a_utf8_header():
    data = korean_pom()
    with pytest.raises(UnicodeDecodeError):
        data.decode("utf-8")
    assert KOREAN.encode("cp949") in data and data.decode("cp949").splitlines()[VERSION_LINE - 1].strip() == \
        "<version>1.2.3</version>"


# ---------------------------------------------------------------------------- boundary check (intake._check_pom)
@pytest.mark.parametrize("key", sorted(DECLS))
def test_boundary_accepts_every_header_maven_accepts(tmp_path, key):
    proj = project(tmp_path, {"pom.xml": korean_pom(DECLS[key])})
    assert intake.check_boundary(proj, cfg(), []) == []


def test_boundary_accepts_a_module_pom_in_a_reactor(tmp_path):
    root = (b'<?xml version="1.0" encoding="UTF-8"?>\n<project><modelVersion>4.0.0</modelVersion>'
            b"<groupId>example</groupId><artifactId>root</artifactId><version>1</version><packaging>pom</packaging>"
            b"<modules><module>core</module></modules></project>\n")
    proj = project(tmp_path, {"pom.xml": root, "core/pom.xml": korean_pom()})
    assert intake.check_boundary(proj, cfg(), []) == []


def test_boundary_still_inspects_repositories_of_a_decoded_ms949_pom(tmp_path):
    repos = ("  <repositories><repository><id>사내</id><url>https://other.invalid/repo</url></repository>"
             "</repositories>\n  <pluginRepositories><pluginRepository><id>p</id>"
             "<url>https://nexus.invalid/p</url></pluginRepository></pluginRepositories>\n")
    proj = project(tmp_path, {"pom.xml": korean_pom(repos=repos)})
    fs = intake.check_boundary(proj, cfg(), ["nexus.invalid"])
    assert [(f.code, f.path) for f in fs] == [("foreign_repository", "pom.xml")]
    assert "id=사내" in fs[0].detail and "host other.invalid not approved" in fs[0].detail


def test_boundary_refuses_truncated_xml_and_says_where(tmp_path):
    data = korean_pom()
    proj = project(tmp_path, {"pom.xml": data[: data.index(b"<dependencies>")]})
    fs = intake.check_boundary(proj, cfg(), [])
    assert [(f.code, f.action) for f in fs] == [("unparsable_pom", "refuse")]
    assert fs[0].detail.startswith("not well-formed XML (line ") and "XMLSyntaxError" not in fs[0].detail
    assert i18n.ko_text(fs[0].detail).startswith("XML 문법 오류(줄 ")


@pytest.mark.skipif(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0), reason="needs POSIX non-root")
def test_boundary_refuses_a_pom_it_may_not_open(tmp_path):
    proj = project(tmp_path, {"pom.xml": korean_pom()})
    (proj / "pom.xml").chmod(0)
    try:
        fs = intake.check_boundary(proj, cfg(), [])
    finally:
        (proj / "pom.xml").chmod(0o644)
    assert [(f.code, f.detail) for f in fs] == [("unparsable_pom", "cannot be opened (permission denied)")]
    assert i18n.ko_text(fs[0].detail) == "파일을 열 수 없습니다(권한)."


def test_boundary_refuses_bytes_no_decoding_explains(tmp_path):
    # a UTF-16 BOM followed by an odd number of bytes decodes in no encoding the file can be
    proj = project(tmp_path, {"pom.xml": b"\xff\xfe<\x00p\x00r"})
    fs = intake.check_boundary(proj, cfg(), [])
    assert [f.code for f in fs] == ["unparsable_pom"] and fs[0].detail.startswith("character encoding unknown")
    assert i18n.ko_text(fs[0].detail) == "글자 인코딩을 알 수 없습니다."


def test_refusal_message_and_report_say_what_is_wrong(tmp_path):
    from sbmig.remediations import graph
    from sbmig.remediations.models import FAILED, BoundaryFinding

    from .fakes import FakeIntakeFns, Recorder, make_cfg, make_components, make_run

    state, paths = make_run(tmp_path)
    b = [BoundaryFinding(code="unparsable_pom", path="core/pom.xml", action="refuse",
                         detail="not well-formed XML (line 12)")]
    rec = Recorder()
    out = graph.run_remediation(state, paths, make_cfg(), make_components(intake=FakeIntakeFns(boundary=b)),
                                save=rec.save, journal=rec.journal, stop=rec.stop)
    assert out.status == FAILED
    md = paths.report_md.read_text(encoding="utf-8")
    head = md.split("## 요약")[0]
    assert "pom 파일을 읽지 못해 작업을 중단했습니다" in head and "core/pom.xml: XML 문법 오류(줄 12)" in head
    assert "Maven 실행 설정을 바꿀 수 있는 파일" not in head
    assert "**pom 파일 고치기**" in md and "제거한 뒤" not in md.split("## 해야 할 일")[1].split("##")[0]
    assert "- `core/pom.xml` — 읽을 수 없는 pom: 작업 중단 — XML 문법 오류(줄 12)" in md
    ko, raw = i18n.split_known(out.message)
    assert raw == [] and " ".join(ko) == "pom 파일을 읽을 수 없습니다. core/pom.xml: XML 문법 오류(줄 12)."


# ---------------------------------------------------------------------------- inventory and planner
def test_inventory_reads_an_ms949_pom_and_locates_the_version(tmp_path):
    proj = project(tmp_path, {"pom.xml": korean_pom()})
    pf = parse_pom_file(proj, ".")
    assert pf.ok, pf.error
    assert KOREAN in pf.lines[1] and pf.lines[VERSION_LINE - 1].strip() == "<version>1.2.3</version>"
    inv = build_inventory(proj, [finding("CVE-1")], model({".": [rdep("example:lib", "1.2.3")]}))
    assert inv.complete, inv.notes
    item = inv.items[0]
    assert item.origin == O_DIRECT_LITERAL and (item.location.pom, item.location.line) == ("pom.xml", VERSION_LINE)


def test_inventory_says_why_a_module_pom_cannot_be_read(tmp_path):
    proj = project(tmp_path, {"pom.xml": b"<project><unclosed>"})
    pf = parse_pom_file(proj, ".")
    assert not pf.ok and pf.error.startswith("cannot read pom.xml: not well-formed XML (line 1")


def test_planner_location_of_an_ms949_pom_is_not_stale(tmp_path):
    proj = project(tmp_path, {"pom.xml": korean_pom()})
    assert planner._stale(proj, PomLocation(pom="pom.xml", line=VERSION_LINE, text="1.2.3")) == ""


# ---------------------------------------------------------------------------- apply, verification, delivery
def _bump(line: int = VERSION_LINE) -> Proposal:
    return Proposal(id="p-1-1", round=1, ga="example:lib", from_version="1.2.3", to_version="1.2.4",
                    kind=K_DIRECT_VERSION, edits=[TextEdit("pom.xml", line, "1.2.3", "1.2.4")])


@pytest.mark.parametrize("key", sorted(DECLS))
def test_apply_changes_only_the_version_bytes(tmp_path, key):
    before = korean_pom(DECLS[key])
    orig = project(tmp_path / "o", {"pom.xml": before})
    cand = project(tmp_path / "c", {"pom.xml": before})
    line = VERSION_LINE if DECLS[key] else VERSION_LINE - 1
    res = A.apply_proposals(cand, [_bump(line)])
    assert res.applied == ["p-1-1"], res.skipped
    after = (cand / "pom.xml").read_bytes()
    # byte for byte: the one version text differs, the declaration and every Korean byte are as they were
    assert after == before.replace(b"<version>1.2.3</version>", b"<version>1.2.4</version>")
    assert len(after) == len(before) and [i for i, (x, y) in enumerate(zip(before, after)) if x != y] == \
        [before.index(b"1.2.3") + 4]
    assert A.verify_text_only(orig, cand, [_bump(line)], ["pom.xml"]) == []


def test_override_entry_into_an_ms949_pom_keeps_the_other_bytes(tmp_path):
    before = korean_pom()
    orig = project(tmp_path / "o", {"pom.xml": before})
    cand = project(tmp_path / "c", {"pom.xml": before})
    edit = A.locate_override(cand, "example", "dep", "2.0.1")
    p = Proposal(id="p-1-2", round=1, ga="example:dep", from_version="2.0.0", to_version="2.0.1",
                 kind="managed_override", edits=[edit])
    res = A.apply_proposals(cand, [p])
    assert res.applied == ["p-1-2"], res.skipped
    after = (cand / "pom.xml").read_bytes()
    assert after.startswith(before[: before.index(b"  <dependencies>")]) and after.endswith(
        before[before.index(b"  <dependencies>"):])
    assert A.verify_text_only(orig, cand, [p], ["pom.xml"]) == []


def test_delivery_zip_holds_the_edited_bytes(tmp_path):
    before = korean_pom()
    cand = project(tmp_path, {"pom.xml": before})
    A.apply_proposals(cand, [_bump()])
    intake.freeze_delivery(cand, tmp_path / "delivery" / "result.zip", 1, ["pom.xml"])
    with zipfile.ZipFile(tmp_path / "delivery" / "result.zip") as zf:
        assert zf.read("pom.xml") == before.replace(b"1.2.3", b"1.2.4")


def test_refusal_head_says_the_pom_once(tmp_path):
    from sbmig.remediations import graph
    from sbmig.remediations.models import BoundaryFinding

    from .fakes import FakeIntakeFns, Recorder, make_cfg, make_components, make_run

    state, paths = make_run(tmp_path)
    b = [BoundaryFinding(code="unparsable_pom", path="core/pom.xml", action="refuse",
                         detail="cannot be opened (permission denied)")]
    rec = Recorder()
    graph.run_remediation(state, paths, make_cfg(), make_components(intake=FakeIntakeFns(boundary=b)),
                          save=rec.save, journal=rec.journal, stop=rec.stop)
    head = paths.report_md.read_text(encoding="utf-8").split("## 요약")[0]
    assert head.count("core/pom.xml") == 1 and "상세:" not in head
    assert "> pom 파일을 읽지 못해 작업을 중단했습니다. core/pom.xml: 파일을 열 수 없습니다(권한)." in head
