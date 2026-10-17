"""Formatting/comment-preserving pom.xml editor (lxml based). Implements ``PomEditorLike``.

Design notes
------------
* The document is parsed with lxml keeping comments, CDATA and whitespace. On output we keep the original
  prolog (XML declaration, comments/doctype before the root) and epilog byte-for-byte, and we restore the
  original root start tag (lxml would normalise its line breaks between attributes).
* Inserted elements are indented with the indent unit detected from the file (default 4 spaces, tabs supported);
  new top-level sections are placed following the conventional Maven element order.
* Every mutator returns True iff the serialised document changed, so calling it twice returns False the 2nd time.
* Works with poms in the default POM namespace and with poms without a namespace.
"""
from __future__ import annotations

import copy
import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from lxml import etree

from sbmig.core.models import GAV, PluginInfo

log = logging.getLogger("sbmig.restructure.pom")

POM_NS = "http://maven.apache.org/POM/4.0.0"
DEFAULT_PLUGIN_GROUP = "org.apache.maven.plugins"

PROJECT_ORDER = [
    "modelVersion", "parent", "groupId", "artifactId", "version", "packaging", "name", "description", "url",
    "inceptionYear", "organization", "licenses", "developers", "contributors", "mailingLists", "prerequisites",
    "modules", "scm", "issueManagement", "ciManagement", "distributionManagement", "properties",
    "dependencyManagement", "dependencies", "repositories", "pluginRepositories", "build", "reporting", "profiles",
]
PARENT_ORDER = ["groupId", "artifactId", "version", "relativePath"]
DEP_ORDER = ["groupId", "artifactId", "version", "type", "classifier", "scope", "systemPath", "exclusions", "optional"]
PLUGIN_ORDER = ["groupId", "artifactId", "version", "extensions", "executions", "dependencies", "goals",
                "inherited", "configuration"]
BUILD_ORDER = ["sourceDirectory", "scriptSourceDirectory", "testSourceDirectory", "outputDirectory",
               "testOutputDirectory", "extensions", "defaultGoal", "resources", "testResources", "directory",
               "finalName", "filters", "pluginManagement", "plugins"]
COORD_ORDER = ["groupId", "artifactId", "version"]

_ORDERS: dict[str, list[str]] = {
    "project": PROJECT_ORDER, "parent": PARENT_ORDER, "dependency": DEP_ORDER, "plugin": PLUGIN_ORDER,
    "build": BUILD_ORDER, "path": COORD_ORDER, "dependencyManagement": ["dependencies"],
    "pluginManagement": ["plugins"],
}

_PROLOG_RE = re.compile(r"\A(?:\s+|<\?.*?\?>|<!--.*?-->|<!DOCTYPE[^>\[]*(?:\[.*?\])?\s*>)*", re.DOTALL)
_START_TAG_RE = re.compile(r"<([^\s/>]+)(?:\s+[^\s=/>]+\s*=\s*(?:\"[^\"]*\"|'[^']*'))*\s*(/?)>", re.DOTALL)
_ENC_RE = re.compile(rb"^\s*<\?xml[^>]*encoding\s*=\s*[\"']([A-Za-z0-9._-]+)[\"']")
_PROP_REF_RE = re.compile(r"^\$\{([^}]+)\}$")

T = TypeVar("T")


class PomEditError(Exception):
    """Raised for malformed / unsupported pom files."""


@dataclass
class ArtifactRef:
    """An element carrying groupId/artifactId/version somewhere in the pom (for version bumps)."""
    section: str                 # dependencies | dependencyManagement | plugin-dependency | annotationProcessorPath
    element: Any                 # lxml element
    group_id: str
    artifact_id: str
    version: str | None

    @property
    def key(self) -> str:
        return f"{self.group_id}:{self.artifact_id}"


def property_ref(value: str | None) -> str | None:
    """'${foo.version}' -> 'foo.version', else None."""
    if not value:
        return None
    m = _PROP_REF_RE.match(value.strip())
    return m.group(1) if m else None


def _is_el(node: Any) -> bool:
    return isinstance(node.tag, str)


def _local(node: Any) -> str:
    return etree.QName(node).localname if _is_el(node) else ""


def _mutator(fn: Callable[..., Any]) -> Callable[..., bool]:
    """Wrap a mutating method: returns True iff the serialised document changed."""
    def wrapper(self: PomEditor, *args: Any, **kwargs: Any) -> bool:
        before = etree.tostring(self._root)
        fn(self, *args, **kwargs)
        return etree.tostring(self._root) != before
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


class PomEditor:
    """Edit one pom.xml in place. ``PomEditor(path)`` reads the file; ``save()`` writes it back."""

    def __init__(self, path: str | Path, text: str | None = None):
        self.path = Path(path)
        if text is not None:
            m = _ENC_RE.match(text.encode("ascii", "replace"))
            raw = text.encode(m.group(1).decode() if m else "utf-8", "xmlcharrefreplace")
        else:
            try:
                raw = self.path.read_bytes()
            except OSError as e:
                raise PomEditError(f"cannot read {self.path}: {e}") from e
        self._load(raw)

    @classmethod
    def from_string(cls, text: str, path: str | Path = "pom.xml") -> PomEditor:
        return cls(path, text=text)

    # ------------------------------------------------------------------ load / serialise
    def _load(self, raw: bytes) -> None:
        self._bom = raw.startswith(b"\xef\xbb\xbf")
        if self._bom:
            raw = raw[3:]
        m = _ENC_RE.match(raw)
        self.encoding = m.group(1).decode() if m else "utf-8"
        try:
            text = raw.decode(self.encoding)
        except (LookupError, UnicodeDecodeError) as e:
            raise PomEditError(f"{self.path}: cannot decode as {self.encoding}: {e}") from e
        self.newline = "\r\n" if "\r\n" in text else "\n"
        text = text.replace("\r\n", "\n")
        parser = etree.XMLParser(remove_blank_text=False, strip_cdata=False, resolve_entities=False,
                                 remove_comments=False, remove_pis=False, huge_tree=True, no_network=True)
        try:
            root = etree.fromstring(text.encode(self.encoding, "xmlcharrefreplace"), parser)
        except etree.XMLSyntaxError as e:
            raise PomEditError(f"{self.path}: malformed XML: {e}") from e
        if root is None or _local(root) != "project":
            raise PomEditError(f"{self.path}: root element is not <project>")
        self._root = root
        self._ns = etree.QName(root).namespace
        pm = _PROLOG_RE.match(text)
        pos = pm.end() if pm else 0
        sm = _START_TAG_RE.match(text, pos)
        self._prolog = text[:pos]
        self._start_tag = sm.group(0) if sm else None
        self._self_closing = bool(sm and sm.group(2))
        if sm and not self._self_closing:
            close = text.rfind(f"</{sm.group(1)}")
            end = text.find(">", close)
            self._epilog = text[end + 1:] if close >= 0 and end >= 0 else "\n"
        else:
            self._epilog = text[sm.end():] if sm else "\n"
        self.indent_unit = self._detect_indent()
        self._original = self.to_string()

    def _detect_indent(self) -> str:
        """Indent unit from the whitespace before the first child of <project> (fallback: deeper levels)."""
        candidates: list[str] = []
        for node in self._root.iter():
            if not _is_el(node) or len(node) == 0:
                continue
            ws = node.text or ""
            if "\n" in ws and not ws.strip():
                depth = self._depth(node) + 1
                ind = ws.rsplit("\n", 1)[1]
                if ind and depth:
                    if ind.startswith("\t"):
                        return "\t"
                    if len(ind) % depth == 0:
                        candidates.append(" " * (len(ind) // depth))
            if len(candidates) >= 3:
                break
        return candidates[0] if candidates else "    "

    def to_string(self) -> str:
        body = etree.tostring(self._root, encoding="unicode", with_tail=False)
        if self._start_tag and not self._self_closing:
            gt = body.find(">")
            if gt > 0 and not body[:gt + 1].endswith("/>"):
                body = self._start_tag + body[gt + 1:]
        out = self._prolog + body + self._epilog
        if self.newline != "\n":
            out = out.replace("\n", self.newline)
        return out

    def save(self, path: str | Path | None = None) -> None:
        target = Path(path) if path else self.path
        target.parent.mkdir(parents=True, exist_ok=True)
        data = self.to_string().encode(self.encoding, "xmlcharrefreplace")
        target.write_bytes((b"\xef\xbb\xbf" if self._bom else b"") + data)
        if path is None or target == self.path:
            self._original = self.to_string()

    @property
    def changed(self) -> bool:
        """True if the document differs from what was loaded / last saved."""
        return self.to_string() != self._original

    def clone_to(self, path: str | Path) -> PomEditor:
        """Write the current content to `path` and return an editor for the copy."""
        self.save(path)
        return PomEditor(path)

    # ------------------------------------------------------------------ low level helpers
    @property
    def root(self) -> Any:
        return self._root

    @property
    def namespace(self) -> str | None:
        return self._ns

    def q(self, name: str) -> str:
        return f"{{{self._ns}}}{name}" if self._ns else name

    def _depth(self, el: Any) -> int:
        d = 0
        p = el.getparent()
        while p is not None:
            d += 1
            p = p.getparent()
        return d

    def _ind(self, depth: int) -> str:
        return "\n" + self.indent_unit * depth

    @staticmethod
    def child(el: Any, name: str) -> Any:
        if el is None:
            return None
        for c in el:
            if _is_el(c) and _local(c) == name:
                return c
        return None

    @staticmethod
    def children(el: Any, name: str | None = None) -> list[Any]:
        if el is None:
            return []
        return [c for c in el if _is_el(c) and (name is None or _local(c) == name)]

    @staticmethod
    def text_of(el: Any, name: str | None = None) -> str | None:
        target = PomEditor.child(el, name) if name else el
        if target is None:
            return None
        return (target.text or "").strip()

    @staticmethod
    def local_name(el: Any) -> str:
        return _local(el)

    def find(self, path: str) -> Any:
        """Element at a slash path of local names below <project>, e.g. 'build/pluginManagement/plugins'."""
        el = self._root
        for part in [p for p in path.split("/") if p]:
            el = self.child(el, part)
            if el is None:
                return None
        return el

    def ensure_path(self, path: str) -> Any:
        """Like find() but creates missing elements (placed in conventional Maven order)."""
        el = self._root
        for part in [p for p in path.split("/") if p]:
            nxt = self.child(el, part)
            if nxt is None:
                nxt = self._insert_ordered(el, part)
            el = nxt
        return el

    def _order_for(self, parent: Any) -> list[str]:
        return _ORDERS.get(_local(parent), [])

    def _sep_like(self, ref: Any, depth: int) -> str:
        """Separator (newlines + indent) to use after `ref` for an element at `depth`."""
        tail = ref.tail or ""
        n = tail.count("\n") if not tail.strip() else 1
        n = max(1, min(2, n))
        if ref.getnext() is None:      # ref is last: its tail is the closing indent → look at previous separator
            prev = ref.getprevious()
            pt = (prev.tail or "") if prev is not None else ""
            n = max(1, min(2, pt.count("\n"))) if prev is not None and not pt.strip() else 1
        return "\n" * n + self.indent_unit * depth

    def _insert_after(self, ref: Any, new: Any) -> None:
        """Insert detached `new` right after `ref`, giving it ref's separator style."""
        parent = ref.getparent()
        depth = self._depth(ref)
        sep = self._sep_like(ref, depth)
        if ref.getnext() is None:
            new.tail = ref.tail if ref.tail is not None and not ref.tail.strip() else self._ind(depth - 1)
        else:
            new.tail = ref.tail
        ref.tail = sep
        parent.insert(parent.index(ref) + 1, new)

    def _insert_first(self, parent: Any, new: Any) -> None:
        depth = self._depth(parent) + 1
        if len(parent) == 0:
            new.tail = self._ind(depth - 1)
        else:
            t = parent.text or ""
            new.tail = t if not t.strip() and "\n" in t else self._ind(depth)
        parent.text = self._ind(depth)
        parent.insert(0, new)

    def _append(self, parent: Any, new: Any) -> None:
        nodes = list(parent)
        if nodes:
            self._insert_after(nodes[-1], new)
        else:
            self._insert_first(parent, new)

    def _position_new(self, parent: Any, el: Any, order: list[str] | None = None) -> None:
        """`el` (created with SubElement, or detached) goes to its conventional position; whitespace is fixed.
        Known names go after the last sibling that precedes them in `order`; unknown names go last."""
        if el.getparent() is parent:
            parent.remove(el)
        order = order if order is not None else self._order_for(parent)
        name = _local(el)
        if name not in order:
            self._append(parent, el)
            return
        idx = order.index(name)
        ref = None
        for c in parent:
            if _is_el(c) and _local(c) in order and order.index(_local(c)) <= idx:
                ref = c
        if ref is not None:
            self._insert_after(ref, el)
        elif len(parent) == 0:
            self._append(parent, el)
        else:
            self._insert_first(parent, el)

    def _insert_ordered(self, parent: Any, name: str, text: str | None = None) -> Any:
        el = etree.SubElement(parent, self.q(name))
        if text is not None:
            el.text = text
        self._position_new(parent, el)
        return el

    def _append_new(self, parent: Any, name: str) -> Any:
        el = etree.SubElement(parent, self.q(name))
        parent.remove(el)
        self._append(parent, el)
        return el

    def reindent(self, el: Any) -> None:
        """Normalise whitespace inside `el` (an element we created or copied) to the file's indentation."""
        depth = self._depth(el)
        kids = list(el)
        if not kids:
            return
        if el.text is None or not el.text.strip():
            el.text = self._ind(depth + 1)
        for i, c in enumerate(kids):
            last = i == len(kids) - 1
            if c.tail is None or not c.tail.strip():
                blank = c.tail is not None and c.tail.count("\n") >= 2 and not last
                c.tail = ("\n" if blank else "") + self._ind(depth if last else depth + 1)
            if _is_el(c):
                self.reindent(c)

    def remove_node(self, el: Any) -> None:
        """Remove an element (or comment) and keep the surrounding indentation tidy."""
        parent = el.getparent()
        if parent is None:
            return
        prev = el.getprevious()
        if el.getnext() is None:
            if prev is not None:
                prev.tail = el.tail
            else:
                parent.text = el.tail if (el.tail or "").strip() == "" else parent.text
        parent.remove(el)
        if len(parent) == 0 and parent.text is not None and not parent.text.strip():
            parent.text = None

    def _remove_if_empty(self, el: Any) -> None:
        if el is not None and el.getparent() is not None and not self.children(el) and not (el.text or "").strip() \
                and not any(not _is_el(c) for c in el):
            self.remove_node(el)

    def _set_child_text(self, parent: Any, name: str, value: str | None) -> None:
        c = self.child(parent, name)
        if value is None:
            if c is not None:
                self.remove_node(c)
            return
        if c is None:
            self._insert_ordered(parent, name, value)
        elif (c.text or "").strip() != value or len(c):
            for k in list(c):
                c.remove(k)
            c.text = value

    def set_child_text(self, parent: Any, name: str, value: str | None) -> None:
        """Public low-level helper: set/remove the text child `name` of an arbitrary element (ordered insert)."""
        self._set_child_text(parent, name, value)

    def remove_if_empty(self, el: Any) -> None:
        self._remove_if_empty(el)

    def _build_value(self, parent: Any, name: str, value: Any) -> Any:
        """Append <name> with a value built from str/bool/int/dict/list to `parent` (no positioning)."""
        el = etree.SubElement(parent, self.q(name))
        self._fill(el, name, value)
        return el

    def _fill(self, el: Any, name: str, value: Any) -> None:
        if value is None:
            return
        if isinstance(value, bool):
            el.text = "true" if value else "false"
        elif isinstance(value, dict):
            for k, v in value.items():
                self._build_value(el, str(k), v)
        elif isinstance(value, (list, tuple)):
            item_name = _singular(name)
            for item in value:
                if isinstance(item, dict) and len(item) == 1:
                    (k, v), = item.items()
                    self._build_value(el, str(k), v)
                else:
                    self._build_value(el, item_name, item)
        else:
            el.text = str(value)

    def copy_into(self, parent: Any, src: Any, *, ordered: bool = False) -> Any:
        """Deep-copy element/comment `src` (possibly from another pom, other namespace) as last child of `parent`
        (or at its conventional position when `ordered`)."""
        new = self._copy_node(parent, src)
        if ordered and _is_el(new):
            self._position_new(parent, new)
        else:
            parent.remove(new)
            self._append(parent, new)
        if _is_el(new):
            self.reindent(new)
        return new

    def _copy_node(self, parent: Any, src: Any) -> Any:
        if not _is_el(src):
            node = copy.copy(src)
            node.tail = None
            parent.append(node)
            return node
        el = etree.SubElement(parent, self.q(_local(src)))
        for k, v in src.attrib.items():
            el.set(k, v)
        el.text = src.text
        for c in src:
            n = self._copy_node(el, c)
            n.tail = c.tail
        return el

    # ------------------------------------------------------------------ coordinates
    def artifact_id(self) -> str | None:
        return self.text_of(self._root, "artifactId")

    def group_id(self) -> str | None:
        return self.text_of(self._root, "groupId")

    def version(self) -> str | None:
        return self.text_of(self._root, "version")

    def effective_group_id(self) -> str | None:
        g = self.group_id()
        p = self.get_parent()
        return g or (p.group_id if p else None)

    def effective_version(self) -> str | None:
        v = self.version()
        p = self.get_parent()
        return v or (p.version if p else None)

    @_mutator
    def set_coordinate(self, name: str, value: str | None) -> None:
        """Set/remove the project's own groupId / artifactId / version / name / description."""
        self._set_child_text(self._root, name, value)

    def packaging(self) -> str:
        return self.text_of(self._root, "packaging") or "jar"

    # ------------------------------------------------------------------ parent
    def get_parent(self) -> GAV | None:
        p = self.child(self._root, "parent")
        if p is None:
            return None
        return GAV(group_id=self.text_of(p, "groupId") or "", artifact_id=self.text_of(p, "artifactId") or "",
                   version=self.text_of(p, "version") or None)

    def parent_relative_path(self) -> str | None:
        """None = element absent (Maven default ../pom.xml); '' = <relativePath/>."""
        p = self.child(self._root, "parent")
        return self.text_of(p, "relativePath") if p is not None else None

    @_mutator
    def set_parent(self, gav: GAV, relative_path: str | None = None) -> None:
        p = self.child(self._root, "parent")
        if p is None:
            p = self._insert_ordered(self._root, "parent")
        self._set_child_text(p, "groupId", gav.group_id)
        self._set_child_text(p, "artifactId", gav.artifact_id)
        self._set_child_text(p, "version", gav.version)
        rp = self.child(p, "relativePath")
        if relative_path is None or relative_path == "":
            if rp is None:
                self._insert_ordered(p, "relativePath")
            elif (rp.text or "").strip() or len(rp):
                for k in list(rp):
                    rp.remove(k)
                rp.text = None
        else:
            self._set_child_text(p, "relativePath", relative_path)

    @_mutator
    def set_parent_relative_path(self, relative_path: str | None) -> None:
        """Only change <relativePath> of an existing parent ('' → <relativePath/>, None → remove element)."""
        p = self.child(self._root, "parent")
        if p is None:
            return
        if relative_path is None:
            self._set_child_text(p, "relativePath", None)
        elif relative_path == "":
            rp = self.child(p, "relativePath")
            if rp is None:
                self._insert_ordered(p, "relativePath")
            elif (rp.text or "").strip():
                rp.text = None
        else:
            self._set_child_text(p, "relativePath", relative_path)

    @_mutator
    def remove_parent(self) -> None:
        p = self.child(self._root, "parent")
        if p is not None:
            self.remove_node(p)

    @_mutator
    def set_packaging(self, packaging: str) -> None:
        cur = self.child(self._root, "packaging")
        if cur is None and packaging == "jar":
            return
        self._set_child_text(self._root, "packaging", packaging)

    # ------------------------------------------------------------------ modules
    def modules(self) -> list[str]:
        return [(m.text or "").strip() for m in self.children(self.child(self._root, "modules"), "module")]

    @_mutator
    def set_modules(self, modules: list[str]) -> None:
        if self.modules() == list(modules):
            return
        mods = self.child(self._root, "modules")
        if not modules:
            if mods is not None:
                self.remove_node(mods)
            return
        if mods is None:
            mods = self._insert_ordered(self._root, "modules")
        for c in list(mods):
            mods.remove(c)
        mods.text = None
        for m in modules:
            el = self._append_new(mods, "module")
            el.text = m

    @_mutator
    def add_module(self, module: str) -> None:
        if module in self.modules():
            return
        mods = self.child(self._root, "modules")
        if mods is None:
            mods = self._insert_ordered(self._root, "modules")
        self._append_new(mods, "module").text = module

    # ------------------------------------------------------------------ properties
    def properties(self) -> dict[str, str]:
        return {_local(c): (c.text or "").strip() for c in self.children(self.child(self._root, "properties"))}

    def get_property(self, key: str) -> str | None:
        return self.properties().get(key)

    def has_property(self, key: str) -> bool:
        return key in self.properties()

    @_mutator
    def set_property(self, key: str, value: str) -> None:
        props = self.child(self._root, "properties")
        if props is None:
            props = self._insert_ordered(self._root, "properties")
        for c in self.children(props):
            if _local(c) == key:
                if (c.text or "").strip() != value:
                    c.text = value
                return
        self._append_new(props, key).text = value

    @_mutator
    def remove_property(self, key: str) -> None:
        props = self.child(self._root, "properties")
        for c in self.children(props):
            if _local(c) == key:
                self.remove_node(c)

    def resolve(self, value: str | None) -> str | None:
        """Resolve ${prop} references against this pom's properties (single level, best effort)."""
        if value is None:
            return None
        props = self.properties()
        builtins = {"project.version": self.effective_version() or "",
                    "project.groupId": self.effective_group_id() or "",
                    "project.artifactId": self.artifact_id() or ""}
        def repl(m: re.Match[str]) -> str:
            k = m.group(1)
            return props.get(k, builtins.get(k, m.group(0)))
        return re.sub(r"\$\{([^}]+)\}", repl, value)

    # ------------------------------------------------------------------ dependencies
    def _deps_container(self, managed: bool, create: bool = False) -> Any:
        if managed:
            dm = self.find("dependencyManagement/dependencies")
            if dm is None and create:
                dm = self.ensure_path("dependencyManagement/dependencies")
            return dm
        d = self.child(self._root, "dependencies")
        if d is None and create:
            d = self._insert_ordered(self._root, "dependencies")
        return d

    def dependency_elements(self, managed: bool = False) -> list[Any]:
        return self.children(self._deps_container(managed), "dependency")

    def _gav_of(self, el: Any) -> GAV:
        opt = (self.text_of(el, "optional") or "").lower() == "true"
        return GAV(group_id=self.text_of(el, "groupId") or "", artifact_id=self.text_of(el, "artifactId") or "",
                   version=self.text_of(el, "version") or None, scope=self.text_of(el, "scope") or None,
                   type=self.text_of(el, "type") or None, classifier=self.text_of(el, "classifier") or None,
                   optional=opt)

    def gav_of(self, el: Any) -> GAV:
        return self._gav_of(el)

    def _find_deps(self, group_id: str, artifact_id: str, managed: bool) -> list[Any]:
        return [e for e in self.dependency_elements(managed)
                if self.text_of(e, "groupId") == group_id and self.text_of(e, "artifactId") == artifact_id]

    def dependencies(self) -> list[GAV]:
        return [self._gav_of(e) for e in self.dependency_elements(False)]

    def managed_dependencies(self) -> list[GAV]:
        return [self._gav_of(e) for e in self.dependency_elements(True)]

    def has_dependency(self, group_id: str, artifact_id: str) -> bool:
        return bool(self._find_deps(group_id, artifact_id, False))

    def has_managed_dependency(self, group_id: str, artifact_id: str) -> bool:
        return bool(self._find_deps(group_id, artifact_id, True))

    def _write_dep(self, el: Any, gav: GAV) -> None:
        self._set_child_text(el, "groupId", gav.group_id)
        self._set_child_text(el, "artifactId", gav.artifact_id)
        if gav.version:
            self._set_child_text(el, "version", gav.version)
        if gav.type:
            self._set_child_text(el, "type", gav.type)
        if gav.classifier:
            self._set_child_text(el, "classifier", gav.classifier)
        if gav.scope:
            self._set_child_text(el, "scope", gav.scope)
        if gav.optional:
            self._set_child_text(el, "optional", "true")

    def _add_dep(self, gav: GAV, managed: bool, if_absent: bool, first: bool = False) -> None:
        existing = self._find_deps(gav.group_id, gav.artifact_id, managed)
        if existing:
            if if_absent:
                return
            self._write_dep(existing[0], gav)
            return
        cont = self._deps_container(managed, create=True)
        el = etree.SubElement(cont, self.q("dependency"))
        cont.remove(el)
        if first and self.children(cont):
            first_el = self.children(cont)[0]
            prev = first_el.getprevious()
            if prev is None:
                self._insert_first(cont, el)
            else:
                self._insert_after(prev, el)
        else:
            self._append(cont, el)
        for name, val in (("groupId", gav.group_id), ("artifactId", gav.artifact_id), ("version", gav.version),
                          ("type", gav.type), ("classifier", gav.classifier), ("scope", gav.scope),
                          ("optional", "true" if gav.optional else None)):
            if val:
                etree.SubElement(el, self.q(name)).text = val
        self.reindent(el)

    @_mutator
    def add_dependency(self, gav: GAV, if_absent: bool = True) -> None:
        self._add_dep(gav, managed=False, if_absent=if_absent)

    @_mutator
    def add_managed_dependency(self, gav: GAV, if_absent: bool = True, first: bool = False) -> None:
        """Add to <dependencyManagement>; BOM imports: GAV(type='pom', scope='import'). `first` puts it on top."""
        self._add_dep(gav, managed=True, if_absent=if_absent, first=first)

    @_mutator
    def remove_dependency(self, group_id: str, artifact_id: str) -> None:
        cont = self._deps_container(False)
        for e in self._find_deps(group_id, artifact_id, False):
            self.remove_node(e)
        self._remove_if_empty(cont)

    @_mutator
    def remove_all_dependencies(self) -> None:
        cont = self._deps_container(False)
        if cont is not None:
            self.remove_node(cont)

    @_mutator
    def remove_managed_dependency(self, group_id: str, artifact_id: str) -> None:
        cont = self._deps_container(True)
        for e in self._find_deps(group_id, artifact_id, True):
            self.remove_node(e)
        self._remove_if_empty(cont)
        dm = self.child(self._root, "dependencyManagement")
        self._remove_if_empty(dm)

    def _set_version_el(self, el: Any, version: str | None) -> None:
        """Set the version of a dependency-like element, going through a locally defined ${prop}."""
        cur = self.text_of(el, "version")
        if version is None:
            self._set_child_text(el, "version", None)
            return
        prop = property_ref(cur)
        if prop and self.has_property(prop):
            self.set_property(prop, version)
            return
        self._set_child_text(el, "version", version)

    @_mutator
    def set_dependency_version(self, group_id: str, artifact_id: str, version: str | None) -> None:
        """Direct + managed. version=None removes <version> from direct deps and drops the local managed entry
        (the version is then managed by the parent)."""
        for e in self._find_deps(group_id, artifact_id, False):
            self._set_version_el(e, version)
        if version is None:
            for e in self._find_deps(group_id, artifact_id, True):
                self.remove_node(e)
            self._remove_if_empty(self._deps_container(True))
            self._remove_if_empty(self.child(self._root, "dependencyManagement"))
        else:
            for e in self._find_deps(group_id, artifact_id, True):
                self._set_version_el(e, version)

    @_mutator
    def replace_dependency(self, old_group: str, old_artifact: str, new: GAV) -> None:
        """Rename g:a in place (direct + managed); keeps scope/exclusions; sets version/scope/type when given."""
        for managed in (False, True):
            olds = self._find_deps(old_group, old_artifact, managed)
            if not olds:
                continue
            if (old_group, old_artifact) != (new.group_id, new.artifact_id) and \
                    self._find_deps(new.group_id, new.artifact_id, managed):
                for e in olds:                 # target already declared → drop the old one
                    self.remove_node(e)
                continue
            for e in olds:
                self._set_child_text(e, "groupId", new.group_id)
                self._set_child_text(e, "artifactId", new.artifact_id)
                if new.version:
                    self._set_child_text(e, "version", new.version)
                if new.scope:
                    self._set_child_text(e, "scope", new.scope)
                if new.type:
                    self._set_child_text(e, "type", new.type)
                if new.classifier:
                    self._set_child_text(e, "classifier", new.classifier)

    # ------------------------------------------------------------------ generic artifact references
    def artifact_refs(self) -> list[ArtifactRef]:
        """All groupId/artifactId/version carriers: deps, managed deps, plugin deps, annotationProcessorPaths."""
        out: list[ArtifactRef] = []

        def add(section: str, el: Any) -> None:
            out.append(ArtifactRef(section, el, self.text_of(el, "groupId") or "",
                                   self.text_of(el, "artifactId") or "", self.text_of(el, "version") or None))

        for e in self.dependency_elements(False):
            add("dependencies", e)
        for e in self.dependency_elements(True):
            add("dependencyManagement", e)
        for p in self.plugin_elements("all"):
            for d in self.children(self.child(p, "dependencies"), "dependency"):
                add("plugin-dependency", d)
            for path in self.children(self.find_in(p, "configuration/annotationProcessorPaths")):
                add("annotationProcessorPath", path)
        return out

    def find_in(self, el: Any, path: str) -> Any:
        for part in [p for p in path.split("/") if p]:
            el = self.child(el, part)
            if el is None:
                return None
        return el

    @_mutator
    def set_ref_version(self, el: Any, version: str | None) -> None:
        """Literal version on a dependency-like element (None removes <version>)."""
        self._set_child_text(el, "version", version)

    @_mutator
    def rename_ref(self, el: Any, group_id: str, artifact_id: str) -> None:
        self._set_child_text(el, "groupId", group_id)
        self._set_child_text(el, "artifactId", artifact_id)

    # ------------------------------------------------------------------ plugins
    def plugin_elements(self, where: str = "build") -> list[Any]:
        """where: build | management | all"""
        out: list[Any] = []
        if where in ("build", "all"):
            out += self.children(self.find("build/plugins"), "plugin")
        if where in ("management", "all"):
            out += self.children(self.find("build/pluginManagement/plugins"), "plugin")
        return out

    def plugin_key(self, el: Any) -> str:
        return f"{self.text_of(el, 'groupId') or DEFAULT_PLUGIN_GROUP}:{self.text_of(el, 'artifactId') or ''}"

    def _find_plugins(self, group_id: str, artifact_id: str, where: str) -> list[Any]:
        key = f"{group_id or DEFAULT_PLUGIN_GROUP}:{artifact_id}"
        return [p for p in self.plugin_elements(where) if self.plugin_key(p) == key]

    def find_plugins(self, group_id: str, artifact_id: str, where: str = "all") -> list[Any]:
        return self._find_plugins(group_id, artifact_id, where)

    def _plugin_info(self, el: Any) -> PluginInfo:
        conf = self.child(el, "configuration")
        return PluginInfo(group_id=self.text_of(el, "groupId") or DEFAULT_PLUGIN_GROUP,
                          artifact_id=self.text_of(el, "artifactId") or "",
                          version=self.text_of(el, "version") or None,
                          configuration=_to_value(conf) if conf is not None and len(conf) else {})

    def plugins(self) -> list[PluginInfo]:
        return [self._plugin_info(p) for p in self.plugin_elements("build")]

    def managed_plugins(self) -> list[PluginInfo]:
        return [self._plugin_info(p) for p in self.plugin_elements("management")]

    def has_plugin(self, group_id: str, artifact_id: str, where: str = "build") -> bool:
        return bool(self._find_plugins(group_id, artifact_id, where))

    @_mutator
    def add_plugin(self, plugin: PluginInfo, if_absent: bool = True,
                   executions: list[dict[str, Any]] | None = None) -> None:
        """Add to <build><plugins>. `configuration` dict → nested elements; `executions` e.g.
        [{"goals": ["repackage"]}] → <executions><execution><goals><goal>repackage</goal>…"""
        existing = self._find_plugins(plugin.group_id, plugin.artifact_id, "build")
        if existing:
            if if_absent:
                return
            el = existing[0]
            if plugin.version:
                self._set_child_text(el, "version", plugin.version)
            if plugin.configuration:
                old = self.child(el, "configuration")
                if old is None or _to_value(old) != _stringify(plugin.configuration):
                    if old is not None:
                        self.remove_node(old)
                    conf = self._build_value(el, "configuration", plugin.configuration)
                    self._position_new(el, conf)
                    self.reindent(conf)
            return
        plugins = self.ensure_path("build/plugins")
        el = etree.SubElement(plugins, self.q("plugin"))
        plugins.remove(el)
        self._append(plugins, el)
        etree.SubElement(el, self.q("groupId")).text = plugin.group_id or DEFAULT_PLUGIN_GROUP
        etree.SubElement(el, self.q("artifactId")).text = plugin.artifact_id
        if plugin.version:
            etree.SubElement(el, self.q("version")).text = plugin.version
        if executions:
            ex = etree.SubElement(el, self.q("executions"))
            for e in executions:
                self._build_value(ex, "execution", e)
        if plugin.configuration:
            self._build_value(el, "configuration", plugin.configuration)
        self.reindent(el)

    @_mutator
    def set_plugin_version(self, group_id: str, artifact_id: str, version: str) -> None:
        for p in self._find_plugins(group_id, artifact_id, "all"):
            self._set_version_el(p, version)

    @_mutator
    def remove_plugin(self, group_id: str, artifact_id: str) -> None:
        for p in self._find_plugins(group_id, artifact_id, "all"):
            parent = p.getparent()
            self.remove_node(p)
            self._remove_if_empty(parent)
        self._remove_if_empty(self.find("build/pluginManagement"))
        self._remove_if_empty(self.child(self._root, "build"))

    @_mutator
    def remove_build_plugins_except(self, keys: Iterable[str]) -> None:
        """Remove every <build><plugins> plugin whose g:a is not in `keys` (pluginManagement untouched)."""
        keep = set(keys)
        for p in self.plugin_elements("build"):
            if self.plugin_key(p) not in keep:
                self.remove_node(p)
        plugins = self.find("build/plugins")
        self._remove_if_empty(plugins)
        self._remove_if_empty(self.child(self._root, "build"))

    @_mutator
    def remove_element(self, path: str) -> None:
        """Remove the element at slash path below <project> (e.g. 'build/finalName'); drops an emptied parent."""
        el = self.find(path)
        if el is not None:
            parent = el.getparent()
            self.remove_node(el)
            if parent is not self._root:
                self._remove_if_empty(parent)


# ---------------------------------------------------------------------- module helpers
def _singular(name: str) -> str:
    if name.endswith("ies"):
        return name[:-3] + "y"
    if name.endswith(("ses", "xes")):
        return name[:-2]
    if name.endswith("s") and len(name) > 1:
        return name[:-1]
    return "item"


def _to_value(el: Any) -> Any:
    kids = [c for c in el if _is_el(c)]
    if not kids:
        return (el.text or "").strip()
    names = [_local(c) for c in kids]
    if len(names) > 1 and len(set(names)) == 1:          # <goals><goal>a</goal><goal>b</goal></goals>
        return [_to_value(c) for c in kids]
    out: dict[str, Any] = {}
    for c in kids:
        k = _local(c)
        v = _to_value(c)
        if k in out:                                       # mixed repeated names → keep a list
            out[k] = (out[k] if isinstance(out[k], list) else [out[k]]) + [v]
        else:
            out[k] = v
    return out


def _stringify(value: Any) -> Any:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict):
        return {str(k): _stringify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_stringify(v) for v in value]
    return "" if value is None else str(value)
