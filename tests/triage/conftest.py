from __future__ import annotations

from pathlib import Path

import pytest

from sbmig.core.models import CaseType, PomInfo, ProjectProfile
from sbmig.core.workspace import Workspace
from sbmig.triage import HandlerRegistry

from .fakes import FakePomEditor, pom_xml

try:
    from sbmig.restructure import PomEditor as RealPomEditor  # type: ignore[attr-defined]
except Exception:  # pragma: no cover
    try:
        from sbmig.restructure.pom_editor import PomEditor as RealPomEditor  # type: ignore[no-redef]
    except Exception:
        RealPomEditor = None


class PomKit:
    """Create poms (fake store and/or real xml) and read them back through the same protocol."""

    def __init__(self, mode: str, root: Path):
        self.mode = mode
        self.root = root
        self.store: dict = {}
        self.ws = Workspace(root, root.parent / "run", "t")
        self.registry = HandlerRegistry.default(None, pom_editor_factory=self.factory)

    def factory(self, path: Path):
        if self.mode == "fake":
            return FakePomEditor(path, self.store)
        return RealPomEditor(path)

    def pom(self, rel: str = "pom.xml", deps=(), props=None, plugins=(), managed=(), extra_build="",
            packaging="jar", artifact="demo") -> str:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(pom_xml(deps, props, plugins, managed, extra_build, packaging, artifact), encoding="utf-8")
        if self.mode == "fake":
            ed = FakePomEditor(p, self.store)
            ed.d.deps = [d.model_copy() for d in deps]
            ed.d.managed = [d.model_copy() for d in managed]
            ed.d.props = dict(props or {})
            ed.d.plugins = [x.model_copy() for x in plugins]
            ed.d.packaging = packaging
        return rel

    def read(self, rel: str = "pom.xml"):
        return self.factory(self.root / rel)

    def dep(self, key: str, rel: str = "pom.xml", managed: bool = False):
        ed = self.read(rel)
        lst = ed.managed_dependencies() if managed else ed.dependencies()
        return next((d for d in lst if d.key == key), None)

    def text(self, rel: str = "pom.xml") -> str:
        return (self.root / rel).read_text(encoding="utf-8")

    def handler(self, hid: str):
        h = self.registry.get(hid)
        assert h is not None, hid
        return h


MODES = ["fake"] + (["real"] if RealPomEditor is not None else [])


@pytest.fixture(params=MODES)
def kit(request, tmp_path: Path) -> PomKit:
    root = tmp_path / "project"
    root.mkdir()
    return PomKit(request.param, root)


@pytest.fixture()
def fake_kit(tmp_path: Path) -> PomKit:
    root = tmp_path / "project"
    root.mkdir()
    return PomKit("fake", root)


def make_profile(poms=None, libs=(), boot="2.7.18", case=CaseType.SPRING_BOOT, root="/x") -> ProjectProfile:
    from sbmig.core.models import LibraryUsage
    poms = poms or [PomInfo(path="pom.xml", artifact_id="demo", is_root=True)]
    return ProjectProfile(root=root, name="demo", case_type=case, poms=poms, root_pom=poms[0],
                          libraries=[LibraryUsage(name=n, version=v) for n, v in libs], spring_boot_version=boot)


@pytest.fixture()
def profile() -> ProjectProfile:
    return make_profile()
