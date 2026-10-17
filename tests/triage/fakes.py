"""Test helpers for triage: a dict-based FakePomEditor and a pom kit that works with fake or real editors."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from sbmig.core.models import GAV, BuildError, ErrorKind, PluginInfo


@dataclass
class FakePom:
    deps: list[GAV] = field(default_factory=list)
    managed: list[GAV] = field(default_factory=list)
    props: dict[str, str] = field(default_factory=dict)
    plugins: list[PluginInfo] = field(default_factory=list)
    parent: Optional[GAV] = None
    packaging: str = "jar"
    modules: list[str] = field(default_factory=list)
    saves: int = 0


class FakePomEditor:
    """Implements PomEditorLike on top of FakePom stored in a shared dict keyed by absolute path."""

    def __init__(self, path: Path, store: dict[str, FakePom]):
        self.path = Path(path)
        self.d = store.setdefault(str(Path(path).resolve()), FakePom())

    # parent / structure
    def set_parent(self, gav, relative_path=None):
        ch = self.d.parent != gav
        self.d.parent = gav
        return ch

    def remove_parent(self):
        ch = self.d.parent is not None
        self.d.parent = None
        return ch

    def set_packaging(self, packaging):
        ch = self.d.packaging != packaging
        self.d.packaging = packaging
        return ch

    def set_modules(self, modules):
        ch = self.d.modules != modules
        self.d.modules = list(modules)
        return ch

    def add_module(self, module):
        if module in self.d.modules:
            return False
        self.d.modules.append(module)
        return True

    # properties
    def get_property(self, key):
        return self.d.props.get(key)

    def set_property(self, key, value):
        ch = self.d.props.get(key) != value
        self.d.props[key] = value
        return ch

    def remove_property(self, key):
        return self.d.props.pop(key, None) is not None

    def resolve(self, value):
        if value and value.startswith("${") and value.endswith("}"):
            return self.d.props.get(value[2:-1], value)
        return value

    # dependencies
    def _find(self, lst, g, a):
        return [x for x in lst if x.group_id == g and x.artifact_id == a]

    def has_dependency(self, g, a):
        return bool(self._find(self.d.deps, g, a))

    def add_dependency(self, gav, if_absent=True):
        if self.has_dependency(gav.group_id, gav.artifact_id):
            return False
        self.d.deps.append(gav)
        return True

    def remove_dependency(self, g, a):
        before = len(self.d.deps)
        self.d.deps = [x for x in self.d.deps if not (x.group_id == g and x.artifact_id == a)]
        return len(self.d.deps) != before

    def set_dependency_version(self, g, a, version):
        ch = False
        for lst in (self.d.deps, self.d.managed):
            for x in self._find(lst, g, a):
                if x.version != version:
                    x.version = version
                    ch = True
        return ch

    def replace_dependency(self, og, oa, new):
        ch = False
        for lst in (self.d.deps, self.d.managed):
            for i, x in enumerate(lst):
                if x.group_id == og and x.artifact_id == oa:
                    lst[i] = GAV(group_id=new.group_id, artifact_id=new.artifact_id,
                                 version=new.version or x.version, scope=new.scope or x.scope,
                                 classifier=new.classifier or x.classifier, type=new.type or x.type)
                    ch = ch or lst[i] != x
        return ch

    def add_managed_dependency(self, gav, if_absent=True):
        if self._find(self.d.managed, gav.group_id, gav.artifact_id):
            return False
        self.d.managed.append(gav)
        return True

    def remove_managed_dependency(self, g, a):
        before = len(self.d.managed)
        self.d.managed = [x for x in self.d.managed if not (x.group_id == g and x.artifact_id == a)]
        return before != len(self.d.managed)

    def dependencies(self):
        return [x.model_copy() for x in self.d.deps]

    def managed_dependencies(self):
        return [x.model_copy() for x in self.d.managed]

    # plugins
    def add_plugin(self, plugin, if_absent=True):
        if any(p.key == plugin.key for p in self.d.plugins):
            return False
        self.d.plugins.append(plugin)
        return True

    def set_plugin_version(self, g, a, version):
        ch = False
        for p in self.d.plugins:
            if p.key == f"{g}:{a}" and p.version != version:
                p.version = version
                ch = True
        return ch

    def remove_plugin(self, g, a):
        before = len(self.d.plugins)
        self.d.plugins = [p for p in self.d.plugins if p.key != f"{g}:{a}"]
        return before != len(self.d.plugins)

    def plugins(self):
        return [p.model_copy() for p in self.d.plugins]

    def save(self):
        self.d.saves += 1

    def to_string(self):
        return repr(self.d)


# ---------------------------------------------------------------------------
def pom_xml(deps=(), props=None, plugins=(), managed=(), extra_build="", packaging="jar", artifact="demo") -> str:
    def dep(g: GAV) -> str:
        parts = [f"<groupId>{g.group_id}</groupId>", f"<artifactId>{g.artifact_id}</artifactId>"]
        if g.version:
            parts.append(f"<version>{g.version}</version>")
        if g.classifier:
            parts.append(f"<classifier>{g.classifier}</classifier>")
        if g.scope:
            parts.append(f"<scope>{g.scope}</scope>")
        return "      <dependency>\n        " + "\n        ".join(parts) + "\n      </dependency>"

    def plug(p: PluginInfo) -> str:
        parts = [f"<groupId>{p.group_id}</groupId>", f"<artifactId>{p.artifact_id}</artifactId>"]
        if p.version:
            parts.append(f"<version>{p.version}</version>")
        if p.configuration:
            conf = "".join(f"<{k}>{v}</{k}>" for k, v in p.configuration.items())
            parts.append(f"<configuration>{conf}</configuration>")
        return "        <plugin>\n          " + "\n          ".join(parts) + "\n        </plugin>"

    props_xml = ""
    if props:
        props_xml = "  <properties>\n" + "".join(f"    <{k}>{v}</{k}>\n" for k, v in props.items()) + "  </properties>\n"
    managed_xml = ""
    if managed:
        managed_xml = ("  <dependencyManagement>\n    <dependencies>\n" + "\n".join(dep(g) for g in managed)
                       + "\n    </dependencies>\n  </dependencyManagement>\n")
    deps_xml = ("  <dependencies>\n" + "\n".join(dep(g).replace("      ", "    ", 1) for g in deps)
                + "\n  </dependencies>\n") if deps else ""
    build = ""
    if plugins or extra_build:
        build = ("  <build>\n    <plugins>\n" + "\n".join(plug(p) for p in plugins) + extra_build
                 + "\n    </plugins>\n  </build>\n")
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<project xmlns="http://maven.apache.org/POM/4.0.0">\n'
            f"  <modelVersion>4.0.0</modelVersion>\n  <groupId>com.example</groupId>\n"
            f"  <artifactId>{artifact}</artifactId>\n  <version>1.0.0</version>\n  <packaging>{packaging}</packaging>\n"
            f"{props_xml}{managed_xml}{deps_xml}{build}</project>\n")


def err(kind: ErrorKind = ErrorKind.COMPILE, message: str = "cannot find symbol", file: Optional[str] = None,
        sig: Optional[str] = None, module: Optional[str] = None, artifact: Optional[str] = None,
        symbol: Optional[str] = None, line: Optional[int] = None, raw: str = "") -> BuildError:
    return BuildError(kind=kind, message=message, file=file, module=module, symbol=symbol, line=line, raw=raw,
                      artifact=GAV.parse(artifact) if artifact else None,
                      signature=sig or f"{kind.value}|{message}|{symbol or ''}",
                      signature_generic=f"{kind.value}|{message.split()[0] if message else ''}")
