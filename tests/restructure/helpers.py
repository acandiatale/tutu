"""Builders for tiny as-is projects used by the restructure tests."""
from __future__ import annotations

from pathlib import Path

from lxml import etree

from sbmig.core.models import CaseType, PomInfo, ProjectProfile, ServiceInfo, SourceStats
from sbmig.core.workspace import Workspace

NS = "http://maven.apache.org/POM/4.0.0"
HEAD = ('<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<project xmlns="{NS}"\n'
        '         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"\n'
        f'         xsi:schemaLocation="{NS} https://maven.apache.org/xsd/maven-4.0.0.xsd">\n'
        '    <modelVersion>4.0.0</modelVersion>\n')


def dep(g: str, a: str, v: str | None = None, scope: str | None = None, extra: str = "") -> str:
    out = f"        <dependency>\n            <groupId>{g}</groupId>\n            <artifactId>{a}</artifactId>\n"
    if v:
        out += f"            <version>{v}</version>\n"
    if scope:
        out += f"            <scope>{scope}</scope>\n"
    return out + extra + "        </dependency>\n"


def write_tree(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


def java(pkg: str, name: str, annotations: str = "", body: str = "", imports: str = "") -> str:
    return f"package {pkg};\n\n{imports}\n{annotations}\npublic class {name} {{\n{body}\n}}\n"


def make_ws(tmp_path: Path, files: dict[str, str], name: str = "proj") -> Workspace:
    src = write_tree(tmp_path / "src-in" / name, files)
    ws = Workspace.from_input(src, tmp_path / "out", run_id=f"t-{name}")
    ws.git_init()
    return ws


def profile(ws: Workspace, case: CaseType, *, artifact_id: str = "shop", group_id: str = "com.acme",
            version: str = "1.0.0-SNAPSHOT", packaging: str = "jar", **source) -> ProjectProfile:
    return ProjectProfile(
        root=str(ws.root), name=artifact_id, case_type=case, packaging=packaging,
        root_pom=PomInfo(path="pom.xml", artifact_id=artifact_id, group_id=group_id, version=version,
                         packaging=packaging, is_root=True),
        source=SourceStats(**source),
    )


def xml(path: Path):
    """Parse a pom strictly with lxml and return (root, find-helper)."""
    root = etree.parse(str(path)).getroot()
    assert etree.QName(root).namespace == NS
    assert root.find(f"{{{NS}}}modelVersion").text == "4.0.0"
    return root


def t(el, path: str) -> str | None:
    """Text at a slash path of local names (namespaced pom)."""
    q = "/".join(f"{{{NS}}}{p}" for p in path.split("/"))
    found = el.find(q)
    return None if found is None else (found.text or "").strip()


def all_(el, path: str):
    q = "/".join(f"{{{NS}}}{p}" for p in path.split("/"))
    return el.findall(q)


def dep_keys(el, path: str = "dependencies/dependency") -> list[str]:
    return [f"{t(d, 'groupId')}:{t(d, 'artifactId')}" for d in all_(el, path)]


__all__ = [
    "HEAD",
    "NS",
    "CaseType",
    "PomInfo",
    "ServiceInfo",
    "all_",
    "dep",
    "dep_keys",
    "java",
    "make_ws",
    "profile",
    "t",
    "write_tree",
    "xml",
]
